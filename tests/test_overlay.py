"""Overlay protocols: Sixel, iTerm2 and Kitty placed at the cursor, painted after frames."""

import re

from PIL import Image as PILImage
from PIL import ImageDraw
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

import pixcell.textual as ct
from pixcell import iterm2, kitty, sixel
from pixcell.probe import Capabilities


def test_sixel_bands_and_runs():
    image = PILImage.new("RGBA", (16, 12), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 7, 5), fill=(255, 0, 0, 255))
    draw.rectangle((8, 6, 15, 11), fill=(0, 0, 255, 255))
    code = sixel.encode(image)
    assert code.startswith('\x1bP0;1;0q"1;1;16;12') and code.endswith("\x1b\\")
    bands = code.split('"1;1;16;12', 1)[1][:-2].split("-")
    assert re.fullmatch(r"(#\d+;2;\d+;\d+;\d+)+#\d+!8~", bands[0])  # red: all six rows, 8 wide
    assert bands[1].endswith("!8?!8~")  # blue: skip 8, then 8 full columns


def test_iterm2_inline():
    code = iterm2.inline(b"\x89PNG", 10, 3)
    assert code == "\x1b]1337;File=inline=1;size=4;width=10;height=3;preserveAspectRatio=0;doNotMoveCursor=1:iVBORw==\x07"


def test_kitty_place_here_and_delete_placement():
    assert kitty.place_here(42, 10, 3, (0, 16, 80, 48)) == "\x1b_Ga=p,i=42,p=1,c=10,r=3,x=0,y=16,w=80,h=48,C=1,q=2\x1b\\"
    assert kitty.delete_placement(42) == "\x1b_Ga=d,d=i,i=42,p=1,q=2\x1b\\"


class _App(App):
    CSS = "Image { width: 10; height: 4; } Static { height: 3; }"

    def __init__(self, widget, written):
        super().__init__()
        self.widget, self.written = widget, written

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("above")
            yield self.widget
            for number in range(20):
                yield Static(f"below {number}")

    def on_mount(self):
        original = self._driver.write
        self._driver.write = lambda data: (self.written.append(data), original(data))[1]


async def _painted(protocol):
    ct.configure(Capabilities(protocol, (8, 16)))
    widget = ct.Image(PILImage.new("RGB", (160, 128), (200, 30, 30)))
    written = []
    app = _App(widget, written)
    async with app.run_test(size=(40, 12)) as pilot:
        await pilot.pause(0.1)
        first = "".join(written)
        written.clear()
        app.query_one(VerticalScroll).scroll_to(y=5, animate=False)  # its top two rows scroll off
        await pilot.pause(0.1)
        second = "".join(written)
        written.clear()
        app.query_one(VerticalScroll).scroll_to(y=20, animate=False)  # all of it
        await pilot.pause(0.1)
        third = "".join(written)
    ct.configure(Capabilities("halfblock"))
    return widget, first, second, third


async def test_sixel_is_painted_cropped_and_dropped():
    widget, first, second, third = await _painted("sixel")
    assert "\x1b[4;1H\x1bP0;1;0q" in first  # at the picture's cells (row 4: under "above")
    assert '"1;1;80;60' in first  # 10x4 cells of 8x16 = 80x64, cut to whole 6-pixel bands
    assert "\x1b[1;1H\x1bP0;1;0q" in second and '"1;1;80;30' in second  # 2 rows left: 32 -> 30
    assert "\x1bP0;1;0q" not in third  # scrolled away: nothing painted


async def test_iterm2_is_painted_at_its_cells():
    _, first, second, _ = await _painted("iterm2")
    assert "\x1b[4;1H\x1b]1337;File=inline=1;" in first and ";width=10;height=4;" in first
    assert ";width=10;height=2;" in second


async def test_kitty_direct_sends_once_then_moves_and_hides():
    widget, first, second, third = await _painted("kitty-direct")
    image_id = widget._image_id
    assert first.count(f"a=t,f=100,t=d,i={image_id}") == 1
    assert f"\x1b[4;1H\x1b_Ga=p,i={image_id},p=1,c=10,r=4,x=0,y=0,w=80,h=64,C=1" in first
    assert f"a=t,f=100" not in second  # not sent again
    assert f"\x1b[1;1H\x1b_Ga=p,i={image_id},p=1,c=10,r=2,x=0,y=32,w=80,h=32,C=1" in second
    assert kitty.delete_placement(image_id) in third
