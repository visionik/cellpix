"""The host protocol: the app's host draws the pictures, and pixcell writes nothing."""

import pytest
from PIL import Image as PILImage
from textual.app import App, ComposeResult

import pixcell.textual as ct
from pixcell.live import LiveImage
from pixcell.probe import Capabilities, detect


@pytest.fixture
def host():
    before = ct.capabilities()
    ct.configure(Capabilities("host", cell_size=(8, 20)))
    yield
    ct.configure(before)


class Writes:
    def __init__(self, driver):
        self.codes = []
        self._write = driver.write
        driver.write = self.record

    def record(self, data):
        self.codes.append(data)
        self._write(data)


class Pictures(App[None]):
    def __init__(self, widget):
        super().__init__()
        self.widget = widget

    def compose(self) -> ComposeResult:
        yield self.widget


def test_forcing_it_never_asks_a_terminal(monkeypatch):
    monkeypatch.setenv("PIXCELL_PROTOCOL", "host")

    def fail(*args):
        raise AssertionError("asked the terminal")

    monkeypatch.setattr("pixcell.probe._ask", fail)
    found = detect()
    assert found.protocol == "host"
    assert found.reason == "PIXCELL_PROTOCOL"


def test_detection_never_picks_it():
    assert Capabilities("host").animation == "native"
    assert "host" not in Capabilities("kitty").supported


def test_animation_can_still_be_turned_off(monkeypatch):
    monkeypatch.setenv("PIXCELL_ANIMATION", "0")
    assert Capabilities("host").animation == ""


async def test_a_picture_writes_nothing_and_draws_blank_cells(host):
    widget = ct.Image(PILImage.new("RGB", (80, 40), "red"), alt="A red box")
    app = Pictures(widget)
    async with app.run_test(size=(40, 10)) as pilot:
        writes = Writes(app._driver)
        await pilot.pause()
        widget.refresh()
        await pilot.pause()
        assert widget.protocol == "host"
        assert widget.alt == "A red box"
        assert widget.live is False
        assert not any("\x1b_G" in code for code in writes.codes)
        assert widget.render_line(0).text.strip() == ""
    assert (widget.get_content_width(None, None)) == 10  # 80 pixels, 8 to a cell


async def test_an_animated_picture_is_left_for_the_host_to_play(host):
    frames = [PILImage.new("RGB", (8, 8), colour) for colour in ("red", "blue")]
    import io

    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=50)
    buffer.seek(0)
    widget = ct.Image(PILImage.open(buffer))
    assert widget.protocol == "host"
    assert widget.animated is True
    assert widget._steps == []


async def test_a_new_live_frame_replaces_image_and_asks_for_a_refresh(host):
    widget = LiveImage((64, 40), fps=100, alt="A camera")
    assert widget.alt == "A camera"
    app = Pictures(widget)
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.pause()
        writes = Writes(app._driver)
        frame = PILImage.new("RGB", (64, 40), "green")
        widget.push(frame)
        await pilot.pause(0.1)
        assert widget.live is True
        assert widget.image is frame
        assert widget.frames_shown == 1
        assert not any("\x1b_G" in code for code in writes.codes)


async def test_clicks_still_map_to_frame_pixels(host):
    widget = LiveImage((160, 100), fps=100)
    widget.styles.width = 20
    widget.styles.height = 5
    app = Pictures(widget)
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.pause()
        assert widget.to_pixels(10, 2.5) == (80, 50)


def test_the_cell_size_can_be_said_after_starting(host):
    assert ct.set_cell_size(9, 18) is True
    assert ct.cell_size() == (9, 18)
    assert ct.set_cell_size(9, 18) is False
