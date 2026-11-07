"""Animated pictures (GIF, WebP, APNG): played by the terminal where it can."""

import io
import re

from PIL import Image as PILImage
from textual.app import App, ComposeResult

import cellpix.textual as ct
from cellpix import frames, kitty
from cellpix.probe import Capabilities


def _gif(count=3, gaps=(80, 0, 200), size=(40, 30)):
    pictures = [PILImage.new("RGB", size, (60 * index, 100, 255 - 60 * index)) for index in range(count)]
    buffer = io.BytesIO()
    pictures[0].save(buffer, format="GIF", save_all=True, append_images=pictures[1:], duration=list(gaps[:count]), loop=0)
    buffer.seek(0)
    return PILImage.open(buffer)


def test_kitty_frame_and_animate_codes():
    assert kitty.frame(42, b"png", 80) == "\x1b_Ga=f,i=42,f=100,X=1,z=80,q=2;cG5n\x1b\\"
    assert kitty.animate(42, frame=1, gap=50) == "\x1b_Ga=a,i=42,r=1,z=50,q=2\x1b\\"
    assert kitty.animate(42, run=True) == "\x1b_Ga=a,i=42,s=3,v=1,q=2\x1b\\"
    assert kitty.animate(42, run=False) == "\x1b_Ga=a,i=42,s=1,q=2\x1b\\"
    chunked = kitty.frame(42, bytes(range(256)) * 20, 80)
    keys = re.findall(r"\x1b_G([^;]*);", chunked)
    assert keys[0].startswith("a=f,i=42") and all(key.startswith("a=f") for key in keys)  # a=f on every chunk


def test_frames_are_whole_pictures_with_gaps():
    image = _gif()
    moving = frames.frames(image, lambda picture: picture)
    assert [gap for _, gap in moving] == [80, frames.DEFAULT_GAP, 200]  # 0 ms means "unset"
    assert all(picture.size == (40, 30) and picture.mode == "RGBA" for picture, _ in moving)
    assert image.tell() == 0  # left where it was
    assert frames.frames(PILImage.new("RGB", (4, 4)), lambda picture: picture) is None  # still
    assert frames.frames(_gif(), lambda picture: picture, max_bytes=100) is None  # too big


def test_which_terminals_play_animations(monkeypatch):
    monkeypatch.delenv("CELLPIX_ANIMATION", raising=False)
    assert Capabilities("kitty", terminal="kitty", version="0.50.0").animation == "native"
    assert Capabilities("kitty", terminal="ghostty", version="1.4.0").animation == "native"
    assert Capabilities("kitty", terminal="ghostty", version="1.3.1").animation == ""  # no a=f yet
    assert Capabilities("kitty", terminal="rex").animation == "stepped"  # we step the frames
    assert Capabilities("kitty", terminal="iterm2", version="3.7.7").animation == ""
    assert Capabilities("iterm2", terminal="wezterm").animation == "native"
    assert Capabilities("sixel", terminal="foot").animation == ""
    monkeypatch.setenv("CELLPIX_ANIMATION", "native")
    assert Capabilities("kitty", terminal="rex").animation == "native"
    assert Capabilities("sixel", terminal="foot").animation == ""  # Sixel can't, whatever
    monkeypatch.setenv("CELLPIX_ANIMATION", "0")
    assert Capabilities("kitty", terminal="kitty").animation == ""


class _App(App):
    CSS = "Image { width: 10; height: 4; }"

    def __init__(self, widget, written):
        super().__init__()
        self.widget, self.written = widget, written

    def compose(self) -> ComposeResult:
        yield self.widget

    def on_mount(self):
        original = self._driver.write
        self._driver.write = lambda data: (self.written.append(data), original(data))[1]


async def _run(capabilities, **options):
    ct.configure(capabilities)
    widget = ct.Image(_gif(), **options)
    written = []
    async with _App(widget, written).run_test() as pilot:
        for _ in range(40):
            await pilot.pause(0.05)
            if "".join(written).count("\x1b_G") or "1337;File" in "".join(written):
                break
        await pilot.pause(0.1)
    ct.configure(Capabilities("halfblock"))
    return widget, "".join(written)


async def test_kitty_plays_the_animation():
    widget, sent = await _run(Capabilities("kitty", (8, 16), terminal="kitty", version="0.50.0"))
    image_id = widget._image_id
    assert widget.animated
    assert sent.count(f"a=f,i={image_id}") == 2  # frames 2 and 3
    assert f"a=a,i={image_id},r=1,z=80" in sent and f"a=a,i={image_id},s=3,v=1" in sent


async def test_still_where_the_terminal_does_not_animate_or_when_asked():
    widget, sent = await _run(Capabilities("kitty", (8, 16), terminal="ghostty", version="1.3.1"))
    assert not widget.animated and "a=f," not in sent
    widget, sent = await _run(Capabilities("kitty", (8, 16), terminal="kitty", version="0.50.0"), animate=False)
    assert not widget.animated and "a=f," not in sent


async def test_iterm2_sends_a_gif_when_fully_visible():
    import base64

    widget, sent = await _run(Capabilities("iterm2", (8, 16), terminal="wezterm"))
    payload = re.search(r"1337;File=[^:]*:([^\x07]*)\x07", sent).group(1)
    assert base64.b64decode(payload)[:6] in (b"GIF89a", b"GIF87a")
    assert widget.animated


async def test_rex_steps_through_the_frames_itself():
    ct.configure(Capabilities("kitty", (8, 16), terminal="rex"))
    widget = ct.Image(_gif(gaps=(40, 40, 40)))
    written = []
    async with _App(widget, written).run_test() as pilot:
        await pilot.pause(0.6)
        sent = "".join(written)
    ct.configure(Capabilities("halfblock"))
    ids = {widget._image_id, widget._spare_id}
    assert widget.animated and "a=a," not in sent and "a=f," not in sent  # we do the playing
    transmits = re.findall(r"a=t,f=100,t=d,i=(\d+)", sent)
    assert len(transmits) >= 4 and {int(found) for found in transmits} == ids  # both buffers, in turn
    assert all(first != second for first, second in zip(transmits[1:], transmits[2:]))


async def test_iterm2_uses_its_own_gifs_for_moving_pictures_only():
    supported = frozenset({"kitty", "kitty-direct", "iterm2", "sixel"})
    ct.configure(Capabilities("kitty", (8, 16), terminal="iterm2", version="3.7.7", supported=supported))
    try:
        assert ct.Image(_gif()).protocol == "iterm2"
        assert ct.Image(PILImage.new("RGB", (8, 8))).protocol == "kitty"  # still: placeholders
        assert ct.Image(_gif(), animate=False).protocol == "kitty"
    finally:
        ct.configure(Capabilities("halfblock"))


async def test_stepped_animation_is_not_limited_by_the_terminal_budget():
    """Stepped, the terminal holds two frames at a time: a long animation still moves."""
    ct.configure(Capabilities("kitty", (8, 16), terminal="rex", image_budget=64 * 1024))
    widget = ct.Image(_gif(gaps=(40, 40, 40), size=(200, 100)))
    written = []
    async with _App(widget, written).run_test() as pilot:
        await pilot.pause(0.5)
    ct.configure(Capabilities("halfblock"))
    assert widget.animated and len(re.findall(r"a=t,f=100", "".join(written))) >= 3
