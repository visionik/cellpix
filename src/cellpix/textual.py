"""An image widget for Textual: Kitty graphics where the terminal has them, half blocks
elsewhere.

Probe the terminal before the app starts (the probe needs the tty to itself)::

    import cellpix.textual
    cellpix.textual.configure()          # runs cellpix.probe.detect()
    MyApp().run()

then use :class:`Image` like any widget. With Kitty graphics the widget draws Unicode
placeholders: coloured text that the terminal shows as the picture, so it scrolls and
clips with everything else. The picture itself is sent once, scaled to the size it's
shown at, and deleted again when the widget goes away. A budget keeps the terminal's
image memory in check: pictures not drawn for a while are deleted first, and sent again
if they come back into view.
"""

from __future__ import annotations

import io
import math
import time
from typing import TYPE_CHECKING

from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget

from cellpix import halfblock, kitty
from cellpix.probe import Capabilities, detect

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

_capabilities = Capabilities("halfblock", reason="cellpix.textual.configure() wasn't called")


def configure(capabilities: Capabilities | None = None) -> Capabilities:
    """Set what the terminal can draw (probing it if not given). Call before ``App.run``."""
    global _capabilities
    _capabilities = capabilities if capabilities is not None else detect()
    _registry.budget = _capabilities.image_budget
    return _capabilities


def capabilities() -> Capabilities:
    return _capabilities


def cell_size() -> tuple[int, int]:
    """A cell's pixel size: the terminal's answer, else a common 10x20."""
    return _capabilities.cell_size or (10, 20)


class _Registry:
    """The pictures sent to the terminal, for its memory budget."""

    KEEP_SECONDS = 2.0
    """A picture drawn this recently is never evicted (it's on screen)."""

    def __init__(self) -> None:
        self.budget = 64 * 1024 * 1024
        self._sent: dict[int, tuple[int, float, Image]] = {}  # id -> (bytes, last drawn, widget)

    @property
    def used(self) -> int:
        return sum(size for size, _, _ in self._sent.values())

    def touch(self, image_id: int) -> None:
        if (entry := self._sent.get(image_id)) is not None:
            self._sent[image_id] = (entry[0], time.monotonic(), entry[2])

    def make_room(self, size: int, keep: int) -> list[int]:
        """Evict the least recently drawn pictures until `size` fits; their ids."""
        evicted = []
        now = time.monotonic()
        for image_id, (bytes_used, drawn, widget) in sorted(self._sent.items(), key=lambda item: item[1][1]):
            if self.used + size <= self.budget:
                break
            if image_id == keep or now - drawn < self.KEEP_SECONDS:
                continue
            del self._sent[image_id]
            widget._evicted()
            evicted.append(image_id)
        return evicted

    def add(self, image_id: int, size: int, widget: Image) -> None:
        self._sent[image_id] = (size, time.monotonic(), widget)

    def remove(self, image_id: int) -> None:
        self._sent.pop(image_id, None)


_registry = _Registry()


class Image(Widget):
    """A picture, drawn with the best protocol the terminal has.

    Size it with CSS (``width``/``height`` in cells); its natural size is its pixel size
    in cells. The picture keeps its aspect ratio within that box.
    """

    DEFAULT_CSS = """
    Image {
        width: auto;
        height: auto;
    }
    """

    RESEND_GROWTH = 1.25
    """Send a sharper copy when the box grows by more than this."""

    def __init__(
        self,
        image: "PILImage",
        *,
        protocol: str | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.image = image
        wanted = protocol or _capabilities.protocol
        # Overlay protocols (iterm2, sixel, kitty-direct) aren't drawn yet: half blocks.
        self.protocol = wanted if wanted in ("kitty", "halfblock", "off") else "halfblock"
        self.image_width, self.image_height = image.size
        self._image_id = kitty.new_image_id()
        self._sent_box: tuple[int, int] | None = None  # pixel box the sent copy was made for
        self._placed: tuple[int, int] | None = None  # cells of the current placement
        self._sending = False
        self._halfblock: tuple[tuple[int, int], list[Strip]] | None = None

    # Size ------------------------------------------------------------------------------

    def get_content_width(self, container, viewport) -> int:
        return max(1, math.ceil(self.image_width / cell_size()[0]))

    def get_content_height(self, container, viewport, width: int) -> int:
        cell_width, cell_height = cell_size()
        columns = min(width, self.get_content_width(container, viewport)) or 1
        return max(1, round(columns * cell_width * self.image_height / self.image_width / cell_height))

    # Drawing ---------------------------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        columns, rows = self.content_size
        if columns <= 0 or rows <= 0 or y >= rows:
            return Strip.blank(columns)
        if self.protocol == "kitty":
            return self._kitty_line(y, columns, rows)
        if self.protocol == "halfblock":
            return self._halfblock_lines(columns, rows)[y]
        return Strip.blank(columns)

    def _kitty_line(self, y: int, columns: int, rows: int) -> Strip:
        columns, rows = min(columns, kitty.MAX_CELLS), min(rows, kitty.MAX_CELLS)
        if self._placed != (columns, rows) or self._sent_box is None:
            if not self._sending:
                self._sending = True
                self.call_later(self._send, columns, rows)
        else:
            _registry.touch(self._image_id)
        style = Style(color=Color.from_rgb(*kitty.id_colour(self._image_id)))
        return Strip([Segment(kitty.placeholder_row(self._image_id, y, columns), style)], columns)

    def _halfblock_lines(self, columns: int, rows: int) -> list[Strip]:
        if self._halfblock is None or self._halfblock[0] != (columns, rows):
            strips = []
            for line in halfblock.cells(self.image, columns, rows):
                segments = [
                    Segment(
                        halfblock.UPPER_HALF,
                        Style(
                            color=Color.from_rgb(*upper) if upper else None,
                            bgcolor=Color.from_rgb(*lower) if lower else None,
                        ),
                    )
                    for upper, lower in line
                ]
                strips.append(Strip(Segment.simplify(segments), columns))
            self._halfblock = ((columns, rows), strips)
        return self._halfblock[1]

    def on_resize(self) -> None:
        self._halfblock = None
        self.refresh()

    # Kitty: sending --------------------------------------------------------------------

    def _send(self, columns: int, rows: int) -> None:
        """Place the picture at this size, sending (a sharper copy of) it if needed."""
        cell_width, cell_height = cell_size()
        box = (columns * cell_width, rows * cell_height)
        sent = self._sent_box
        if sent is not None and box[0] <= sent[0] * self.RESEND_GROWTH and box[1] <= sent[1] * self.RESEND_GROWTH:
            self._write(kitty.place(self._image_id, columns, rows))
            self._placed = (columns, rows)
            self._sending = False
            self.refresh()
            return
        self.run_worker(lambda: self._encode(box, columns, rows), thread=True, exit_on_error=False)

    def _encode(self, box: tuple[int, int], columns: int, rows: int) -> None:
        try:
            self._encode_png(box, columns, rows)
        except Exception:
            self.app.call_from_thread(setattr, self, "_sending", False)
            raise

    def _encode_png(self, box: tuple[int, int], columns: int, rows: int) -> None:
        picture = self.image.copy()
        picture.thumbnail(box)  # only ever shrinks; the terminal fits it to the cells
        if picture.mode not in ("RGB", "RGBA"):
            picture = picture.convert("RGBA")
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG", compress_level=1)
        self.app.call_from_thread(self._sent, buffer.getvalue(), picture.size, box, columns, rows)

    def _sent(self, png: bytes, size: tuple[int, int], box: tuple[int, int], columns: int, rows: int) -> None:
        if not self.is_attached:
            return
        decoded = size[0] * size[1] * 4  # what the terminal keeps
        codes = [kitty.delete(old) for old in _registry.make_room(decoded, keep=self._image_id)]
        if self._sent_box is not None:
            codes.append(kitty.delete(self._image_id))  # replacing its data needs a delete
        codes.append(kitty.transmit(self._image_id, png))
        codes.append(kitty.place(self._image_id, columns, rows))
        self._write("".join(codes))
        _registry.add(self._image_id, decoded, self)
        self._sent_box, self._placed, self._sending = box, (columns, rows), False
        self.refresh()

    def _evicted(self) -> None:
        """The registry deleted this picture from the terminal: send it again when drawn."""
        self._sent_box = self._placed = None

    def on_unmount(self) -> None:
        if self._sent_box is not None:
            self._write(kitty.delete(self._image_id))
        _registry.remove(self._image_id)
        self._sent_box = self._placed = None

    def _write(self, codes: str) -> None:
        driver = getattr(self.app, "_driver", None)
        if driver is not None:
            if _capabilities.tmux:
                codes = _through_tmux(codes)
            driver.write(codes)
            driver.flush()


def _through_tmux(codes: str) -> str:
    """Each graphics escape code wrapped for tmux's passthrough (one per code: tmux
    limits how much one passthrough sequence may carry)."""
    from cellpix.probe import _wrap

    return "".join(_wrap(code + "\x1b\\", tmux=True) for code in codes.split("\x1b\\") if code)
