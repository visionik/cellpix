"""LiveImage: frames pushed in, only changes sent, clicks mapped back to pixels."""

import re

from PIL import Image as PILImage
from PIL import ImageDraw
from textual.app import App, ComposeResult

import cellpix.textual as ct
from cellpix import kitty
from cellpix.live import LiveImage
from cellpix.probe import Capabilities


def test_raw_pixel_commands():
    picture = PILImage.new("RGB", (4, 2), (1, 2, 3))
    assert kitty.transmit_pixels(7, picture).startswith("\x1b_Ga=t,t=d,i=7,f=24,s=4,v=2,o=z,q=2;")
    assert kitty.edit_pixels(7, picture, 8, 4).startswith("\x1b_Ga=f,i=7,r=1,x=8,y=4,f=24,s=4,v=2,o=z,X=1,q=2;")
    assert kitty.pixels(PILImage.new("RGBA", (2, 2)))[0]["f"] == 32
    big = PILImage.effect_noise((200, 200), 64).convert("RGB")
    keys = re.findall(r"\x1b_G([^;]*);", kitty.edit_pixels(7, big, 0, 0))
    assert keys[0].startswith("a=f,") and all(key.startswith("a=f") for key in keys[1:])  # a=f on every chunk


def test_per_terminal_format_and_edits():
    assert Capabilities("kitty", terminal="rex").frame_format == "pixels"
    assert Capabilities("kitty", terminal="iterm2", version="3.7.7").frame_format == "png"
    assert Capabilities("kitty", terminal="rex").edits
    assert not Capabilities("kitty", terminal="ghostty", version="1.3.1").edits


class _App(App):
    CSS = "LiveImage { width: 20; height: 5; }"

    def __init__(self, live, written):
        super().__init__()
        self.live, self.written, self.clicks = live, written, []

    def compose(self) -> ComposeResult:
        yield self.live

    def on_mount(self):
        original = self._driver.write
        self._driver.write = lambda data: (self.written.append(data), original(data))[1]

    def on_live_image_clicked(self, message: LiveImage.Clicked) -> None:
        self.clicks.append((message.x, message.y))


def _frame(x):
    picture = PILImage.new("RGB", (160, 80), (20, 40, 80))
    ImageDraw.Draw(picture).rectangle((x, 30, x + 10, 40), fill=(255, 0, 0))
    return picture


async def _run(capabilities, steps=3):
    ct.configure(capabilities)
    live = LiveImage((160, 80), fps=50)
    written = []
    app = _App(live, written)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        first = "".join(written)
        written.clear()
        for step in range(steps):
            live.push(_frame(20 + 10 * step))
            await pilot.pause(0.1)
        updates = "".join(written)
        written.clear()
        live.push(_frame(20 + 10 * (steps - 1)))  # unchanged: nothing to send
        await pilot.pause(0.1)
        unchanged = "".join(written)
        await pilot.click(LiveImage, offset=(5, 2))
        await pilot.pause(0.1)
    ct.configure(Capabilities("halfblock"))
    return live, first, updates, unchanged, app.clicks


async def test_only_the_changed_box_is_sent_as_an_edit():
    live, first, updates, unchanged, _ = await _run(Capabilities("kitty", (8, 16), terminal="rex"))
    assert "a=t,t=d,i=" in first and ",f=24," in first and ",o=z," in first  # whole, raw + zlib
    edits = re.findall(r"a=f,i=\d+,r=1,x=(\d+),y=(\d+),f=24,s=(\d+),v=(\d+)", updates)
    assert len(edits) == 3 and "a=t," not in updates  # edits only, no resend
    widths = [int(width) for _, _, width, _ in edits]
    assert widths[0] == 160  # from the blank start: everything changed
    assert all(width <= 30 for width in widths[1:])  # then just where the square moved
    assert "\x1b_G" not in unchanged
    assert live.frames_shown == 4


async def test_png_where_the_terminal_takes_it_faster():
    _, first, updates, _, _ = await _run(Capabilities("kitty", (8, 16), terminal="iterm2", version="3.7.7"))
    assert "f=100" in first and "f=24" not in first


async def test_whole_frames_swap_ids_where_edits_are_unknown():
    live, _, updates, _, _ = await _run(Capabilities("kitty", (8, 16), terminal="ghostty", version="1.3.1"))
    assert "a=f," not in updates
    ids = re.findall(r"a=t,t=d,i=(\d+)", updates)
    assert len(ids) == 3 and ids[0] != ids[1] and ids[0] == ids[2]  # two buffers in turn


async def test_clicks_come_back_in_frame_pixels():
    # 20x5 cells of 8x16 = 160x80: the frame fits exactly, so cell (5, 2) is pixel (40, 32)
    # plus however far into the cell the click landed.
    live, *_, clicks = await _run(Capabilities("kitty", (8, 16), terminal="rex"))
    (x, y), = clicks
    assert 40 <= x < 48 and 32 <= y < 48
    assert live.to_pixels(-1, 0) is None


async def test_overlay_frames_are_painted_over_without_a_redraw():
    """iTerm2-protocol frames go straight over the old picture (no blank cells between)."""
    live, first, updates, _, _ = await _run(Capabilities("iterm2", (8, 16), terminal="wezterm"))
    paints = updates.count("\x1b]1337;File=")
    assert paints >= 3  # each new frame painted
    assert live.bytes_sent > 0  # and counted


async def test_overlay_frames_fill_the_box_whatever_size_it_started_at():
    """The bug: overlay protocols fitted each frame by the size the LiveImage was made
    with (1280x800 here), not the frame's own: a frame made for the widget's box was
    squeezed to 1.6:1 and letterboxed (too narrow in a wide pane, too short in a tall
    one). Console's browser pushes frames the box's size; in WezTerm (iTerm2 images)
    the page came out narrower than its pane."""
    import base64
    import io

    for protocol in ("iterm2", "sixel", "kitty-direct"):
        ct.configure(Capabilities(protocol, (8, 16), terminal="wezterm"))
        live = LiveImage((1280, 800), fps=50)  # 20x5 cells of 8x16: a 160x80 box (2:1)
        written = []
        async with _App(live, written).run_test() as pilot:
            await pilot.pause(0.2)
            live.push(PILImage.new("RGB", (160, 80), (200, 30, 30)))
            await pilot.pause(0.2)
            canvas = live._canvas[1]
        ct.configure(Capabilities("halfblock"))
        assert canvas.size == (160, 80)
        assert canvas.getchannel("A").getextrema() == (255, 255), protocol  # no letterbox
        if protocol == "iterm2":
            payload = re.findall(r"1337;File=[^:]*:([^\x07]*)\x07", "".join(written))[-1]
            painted = PILImage.open(io.BytesIO(base64.b64decode(payload))).convert("RGBA")
            assert painted.getchannel("A").getextrema() == (255, 255)
