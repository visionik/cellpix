"""The widget's lifecycles, made explicit: sending a picture, its two buffers, animation."""

import io

from PIL import Image as PILImage
from textual.app import App, ComposeResult

import pixcell.textual as ct
from pixcell import kitty
from pixcell.live import LiveImage
from pixcell.probe import Capabilities


def _gif(count=3, gap=40, size=(40, 30)):
    pictures = [PILImage.new("RGB", size, (60 * index, 100, 255 - 60 * index)) for index in range(count)]
    buffer = io.BytesIO()
    pictures[0].save(buffer, format="GIF", save_all=True, append_images=pictures[1:], duration=gap, loop=0)
    buffer.seek(0)
    return PILImage.open(buffer)


class _App(App):
    CSS = "Image { width: 10; height: 4; } LiveImage { width: 20; height: 5; }"

    def __init__(self, widget, written):
        super().__init__()
        self.widget, self.written = widget, written

    def compose(self) -> ComposeResult:
        yield self.widget

    def on_mount(self):
        original = self._driver.write
        self._driver.write = lambda data: (self.written.append(data), original(data))[1]


async def _until(pilot, done, tries=40):
    for _ in range(tries):
        await pilot.pause(0.05)
        if done():
            return


async def test_a_picture_that_cannot_be_encoded_is_not_tried_again_and_again(monkeypatch):
    """The bug: a failed encode cleared the 'sending' flag, so every redraw started
    another encode in a worker, for ever. Failed now holds until the size changes."""
    attempts = []

    def broken(picture):
        attempts.append(1)
        raise OSError("can't encode")

    monkeypatch.setattr(ct, "_png", broken)
    ct.configure(Capabilities("kitty", (8, 16)))
    widget = ct.Image(PILImage.new("RGB", (400, 300)))
    try:
        async with _App(widget, []).run_test() as pilot:
            await pilot.pause(0.2)
            for _ in range(5):
                widget.refresh()
                await pilot.pause(0.05)
            assert widget.upload == "failed"
            assert len(attempts) == 1
    finally:
        ct.configure(Capabilities("halfblock"))


async def test_stepped_kitty_counts_and_frees_both_buffers():
    """The bug: after a step the spare id still held the last frame, but the budget
    counted one frame, and eviction or an unmount after it left the spare's data in the
    terminal for good."""
    ct.configure(Capabilities("kitty", (8, 16), terminal="ghostty", version="1.3.1"))
    widget = ct.Image(_gif())
    written = []
    try:
        async with _App(widget, written).run_test() as pilot:
            await _until(pilot, lambda: "".join(written).count("a=t,") >= 3)
            frame_bytes = widget._sent_box[0] * widget._sent_box[1] * 4
            assert ct._registry.used == 2 * frame_bytes  # both buffers hold a frame
            ids = {widget._image_id, widget._spare_id}
            written.clear()
            evicted = widget._evicted()
            assert set(evicted) == ids  # the spare goes with it
            assert widget.upload == "unsent"
            written.clear()
            await widget.remove()
        assert ct._registry.used == 0
    finally:
        ct.configure(Capabilities("halfblock"))


async def test_registry_deletes_a_stepped_widgets_spare_too():
    ct.configure(Capabilities("kitty", (8, 16), terminal="ghostty", version="1.3.1"))
    widget = ct.Image(_gif())
    written = []
    budget = ct._registry.budget
    try:
        async with _App(widget, written).run_test() as pilot:
            await _until(pilot, lambda: "".join(written).count("a=t,") >= 3)
            ids = {widget._image_id, widget._spare_id}
            for image_id in ids:
                entry = ct._registry._sent[image_id]
                ct._registry._sent[image_id] = (entry[0], 0.0, entry[2])  # drawn long ago
            ct._registry.budget = 1
            assert set(ct._registry.make_room(10, keep=-1)) == ids
            assert widget.upload == "unsent"
    finally:
        ct._registry.budget = budget
        ct.configure(Capabilities("halfblock"))


async def test_live_image_frees_its_spare_after_eviction():
    """LiveImage swaps whole frames between two ids where edits don't redraw: the same
    two buffers as stepped animation, now one implementation."""
    ct.configure(Capabilities("kitty", (8, 16), terminal="ghostty", version="1.3.1"))
    live = LiveImage((160, 80), fps=50)
    written = []
    try:
        async with _App(live, written).run_test() as pilot:
            await _until(pilot, lambda: live.upload == "shown")
            for shade in (10, 200):
                live.push(PILImage.new("RGB", (160, 80), (shade, 0, 0)))
                await pilot.pause(0.1)
            spare = live._spare_id
            assert spare in ct._registry._sent  # it holds the frame before
            live._evicted()
            written.clear()
            await live.remove()
            assert kitty.delete(spare) not in "".join(written)  # already deleted on eviction
        assert ct._registry.used == 0
    finally:
        ct.configure(Capabilities("halfblock"))


async def test_unmounting_while_sending_does_not_leave_it_sending():
    ct.configure(Capabilities("kitty", (8, 16)))
    widget = ct.Image(PILImage.new("RGB", (400, 300)))
    try:
        async with _App(widget, []).run_test() as pilot:
            widget._upload = "sending"
            await widget.remove()
            await pilot.pause()
            assert widget.upload == "unsent"
    finally:
        ct.configure(Capabilities("halfblock"))


async def test_still_reason_says_why_a_picture_does_not_move(monkeypatch):
    ct.configure(Capabilities("kitty", (8, 16), terminal="kitty", version="0.50.0"))
    try:
        assert ct.Image(_gif()).still_reason == ""
        assert ct.Image(PILImage.new("RGB", (4, 4))).still_reason == "not an animation"
        assert ct.Image(_gif(), animate=False).still_reason == "animations are off"
        assert ct.Image(_gif(), protocol="off").still_reason == "off can't animate here"
        ct.configure(Capabilities("kitty-direct", (8, 16), terminal="wezterm"))
        assert ct.Image(_gif(), protocol="kitty-direct").still_reason == "kitty-direct can't animate here"
        monkeypatch.setenv("PIXCELL_ANIMATION", "0")
        assert ct.Image(_gif()).still_reason == "animations are off"
    finally:
        ct.configure(Capabilities("halfblock"))
