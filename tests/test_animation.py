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
    assert Capabilities("kitty", terminal="ghostty", version="1.3.1").animation == "stepped"  # no a=f yet
    assert Capabilities("kitty", terminal="rex").animation == "native"
    assert Capabilities("kitty", terminal="iterm2", version="3.7.7").animation == "stepped"
    assert Capabilities("kitty-direct", terminal="wezterm").animation == ""  # would flicker
    assert Capabilities("iterm2", terminal="iterm2", version="3.7.7").animation == "native"
    assert Capabilities("iterm2", terminal="wezterm").animation == ""  # shows the first frame only
    assert Capabilities("sixel", terminal="foot").animation == "stepped"  # repainted per frame
    assert Capabilities("halfblock").animation == "stepped"
    assert Capabilities("off").animation == ""
    monkeypatch.setenv("CELLPIX_ANIMATION", "native")
    assert Capabilities("kitty", terminal="ghostty", version="1.3.1").animation == "native"
    assert Capabilities("sixel", terminal="foot").animation == "stepped"  # never native
    monkeypatch.setenv("CELLPIX_ANIMATION", "0")
    assert Capabilities("kitty", terminal="kitty").animation == ""
    assert Capabilities("sixel", terminal="foot").animation == ""
    assert Capabilities("halfblock").animation == ""


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
    widget, sent = await _run(Capabilities("kitty-direct", (8, 16), terminal="wezterm"), protocol="kitty-direct")
    assert not widget.animated and "a=f," not in sent
    widget, sent = await _run(Capabilities("kitty", (8, 16), terminal="kitty", version="0.50.0"), animate=False)
    assert not widget.animated and "a=f," not in sent


async def test_iterm2_sends_a_gif_when_fully_visible():
    import base64

    widget, sent = await _run(Capabilities("iterm2", (8, 16), terminal="iterm2"))
    payload = re.search(r"1337;File=[^:]*:([^\x07]*)\x07", sent).group(1)
    assert base64.b64decode(payload)[:6] in (b"GIF89a", b"GIF87a")
    assert widget.animated


async def test_placeholder_terminals_without_playback_step_through_the_frames():
    ct.configure(Capabilities("kitty", (8, 16), terminal="ghostty", version="1.3.1"))
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


async def test_too_big_to_play_natively_steps_instead():
    """Played natively the terminal keeps every frame; when they don't fit its budget the
    animation is stepped (two frames in the terminal at a time) rather than shown still."""
    ct.configure(Capabilities("kitty", (8, 16), terminal="rex", image_budget=64 * 1024))
    widget = ct.Image(_gif(gaps=(40, 40, 40), size=(200, 100)))
    written = []
    async with _App(widget, written).run_test() as pilot:
        await pilot.pause(0.5)
    ct.configure(Capabilities("halfblock"))
    sent = "".join(written)
    assert widget.animated and widget._animation == "stepped" and "a=a," not in sent
    assert len(re.findall(r"a=t,f=100", sent)) >= 3


def test_encoded_frames_keep_only_the_encoded_copy():
    image = _gif(count=3, gaps=(80, 0, 200), size=(40, 30))
    encoded, size, decoded = frames.encoded_frames(image, lambda picture: picture, lambda picture: b"png" * 10)
    assert [gap for _, gap in encoded] == [80, frames.DEFAULT_GAP, 200] and size == (40, 30)
    assert decoded == 3 * 40 * 30 * 4  # counted, though none is kept
    assert all(data == b"png" * 10 for data, _ in encoded)
    assert image.tell() == 0
    assert frames.encoded_frames(_gif(), lambda p: p, lambda p: b"x" * 100, max_encoded=150) is None  # too big
    assert frames.encoded_frames(PILImage.new("RGB", (4, 4)), lambda p: p, lambda p: b"") is None  # still


def _long_gif(count, size):
    """A GIF with ``count`` frames that all differ (Pillow merges identical neighbours)."""
    pictures = []
    for index in range(count):
        picture = PILImage.new("RGB", size, (20, 20, 20))
        picture.putpixel((index % size[0], (index // size[0]) % size[1]), (255, 255, 255))
        pictures.append(picture)
    buffer = io.BytesIO()
    pictures[0].save(buffer, format="GIF", save_all=True, append_images=pictures[1:], duration=80, loop=0)
    buffer.seek(0)
    return PILImage.open(buffer)


def test_a_long_animation_is_encoded_not_dropped():
    """228 frames: over MAX_FRAMES (200), which only limits frames kept decoded."""
    gif = _long_gif(228, (60, 40))
    assert gif.n_frames == 228
    assert frames.frames(gif, lambda picture: picture) is None
    encoded, _, _ = frames.encoded_frames(gif, lambda picture: picture, lambda picture: b"p")
    assert len(encoded) == 228


async def test_a_long_gif_over_the_terminals_budget_is_stepped_and_reserves_two_frames():
    """The bug: a 228-frame 350x233 GIF in Rex (8 MB budget) was shown still. Now it's
    stepped, and only two frames count against the terminal's memory. (Shrunk to a small
    box, the same GIF fits the budget, and the terminal plays it itself.)"""
    ct.configure(Capabilities("kitty", (8, 16), terminal="rex", image_budget=8 * 1024 * 1024))
    widget = ct.Image(_long_gif(228, (350, 233)))
    widget.styles.width, widget.styles.height = 44, 15  # 352 x 240 px: the GIF at its own size
    written = []
    async with _App(widget, written).run_test(size=(80, 30)) as pilot:
        for _ in range(60):
            await pilot.pause(0.05)
            if widget._steps:
                break
        reserved = ct._registry.used
    ct.configure(Capabilities("halfblock"))
    assert widget.animated and widget._animation == "stepped" and len(widget._steps) == 228
    assert 0 < reserved <= 2 * 350 * 233 * 4  # two frames at most, not 228 (74 MB)


async def _stepped_output(capabilities, seconds=0.8):
    ct.configure(capabilities)
    widget = ct.Image(_gif(gaps=(40, 40, 40)))
    written = []
    async with _App(widget, written).run_test() as pilot:
        await pilot.pause(seconds)
    ct.configure(Capabilities("halfblock"))
    return widget, "".join(written)


async def test_half_blocks_step_through_the_frames(monkeypatch):
    monkeypatch.delenv("CELLPIX_ANIMATION", raising=False)
    ct.configure(Capabilities("halfblock", (8, 16)))
    widget = ct.Image(_gif(gaps=(40, 40, 40)))
    seen = set()
    async with _App(widget, []).run_test() as pilot:
        for _ in range(40):
            await pilot.pause(0.02)
            seen |= {segment.style.color.triplet for segment in widget.render_line(1) if segment.style.color}
    ct.configure(Capabilities("halfblock"))
    assert widget.animated and widget._animation == "stepped" and len(widget._steps) == 3
    assert {(0, 100, 255), (60, 100, 195), (120, 100, 135)} <= {tuple(colour) for colour in seen}


async def test_sixel_steps_through_the_frames_painting_each_whole(monkeypatch):
    monkeypatch.delenv("CELLPIX_ANIMATION", raising=False)
    widget, sent = await _stepped_output(Capabilities("sixel", (8, 16), terminal="foot"))
    assert widget.animated and len(widget._steps) == 3
    codes = re.findall(r"\x1bP0;1;0q.*?\x1b\\", sent, re.S)
    palettes = [re.findall(r"#\d+;2;([\d;]+)", code) for code in codes[-3:]]
    for red, blue in ((0, 255), (60, 195), (120, 135)):  # every frame, in Sixel's percentages
        assert any(f"{red * 100 // 255};39;{blue * 100 // 255}" in palette for palette in palettes)
    # Opaque: the letterbox is painted in the background colour, so no old frame shows.
    assert all(len(palette) == 2 for palette in palettes) and len({palette[1] for palette in palettes}) == 1


async def test_stepping_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("CELLPIX_ANIMATION", "0")
    widget, sent = await _stepped_output(Capabilities("halfblock", (8, 16)), seconds=0.3)
    assert not widget.animated and not widget._steps


def test_two_threads_can_go_through_one_animated_picture():
    """The bug: the demo shows each picture twice, and both widgets' workers seeked the
    same image at once: frames came out of order, or Pillow broke and both stayed still.
    Each encode here pauses, so without the lock the two threads interleave."""
    import threading
    import time

    image = _long_gif(40, (60, 40))
    results = {}

    def encode(picture):
        time.sleep(0.001)
        return picture.tobytes()

    def work(name):
        try:
            results[name] = frames.encoded_frames(image, lambda picture: picture, encode)
        except Exception as error:
            results[name] = error

    threads = [threading.Thread(target=work, args=(name,)) for name in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    alone = frames.encoded_frames(image, lambda picture: picture, lambda picture: picture.tobytes())
    assert results["a"] == results["b"] == alone


async def test_wezterm_moves_animations_as_sixel():
    """WezTerm draws still pictures as iTerm2 images but doesn't play their GIFs: moving
    ones go to Sixel, stepped. A protocol forced with CELLPIX_PROTOCOL is kept."""
    supported = frozenset({"kitty-direct", "iterm2", "sixel"})
    try:
        ct.configure(Capabilities("iterm2", (7, 16), terminal="wezterm", supported=supported))
        assert (ct.Image(_gif()).protocol, ct.Image(_gif())._animation) == ("sixel", "stepped")
        assert ct.Image(PILImage.new("RGB", (8, 8))).protocol == "iterm2"  # still
        ct.configure(Capabilities("sixel", (7, 16), terminal="wezterm", reason="CELLPIX_PROTOCOL", supported=supported))
        assert (ct.Image(_gif()).protocol, ct.Image(_gif())._animation) == ("sixel", "stepped")
        ct.configure(Capabilities("iterm2", (7, 16), terminal="wezterm", reason="CELLPIX_PROTOCOL", supported=supported))
        assert ct.Image(_gif()).protocol == "iterm2" and not ct.Image(_gif()).animated
    finally:
        ct.configure(Capabilities("halfblock"))
