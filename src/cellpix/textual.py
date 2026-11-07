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

from cellpix import frames, halfblock, iterm2, kitty, sixel
from cellpix.probe import Capabilities, detect

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

_capabilities = Capabilities("halfblock", reason="cellpix.textual.configure() wasn't called")


_animate = True


def configure(capabilities: Capabilities | None = None, *, animate: bool = True) -> Capabilities:
    """Set what the terminal can draw (probing it if not given). Call before ``App.run``.

    ``animate=False`` keeps animated pictures (GIF, WebP, APNG) still, on their first frame.
    """
    global _capabilities, _animate
    _animate = animate
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
        animate: bool | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.image = image
        wanted = protocol or _capabilities.protocol
        self.protocol = wanted if wanted in ("kitty", "halfblock", "off", *OVERLAY) else "halfblock"
        self.image_width, self.image_height = image.size
        self._image_id = kitty.new_image_id()
        self._sent_box: tuple[int, int] | None = None  # pixel box the sent copy was made for
        self._placed: tuple[int, int] | None = None  # cells of the current placement
        self._sending = False
        self._halfblock: tuple[tuple[int, int], list[Strip]] | None = None
        # Overlay protocols: what was painted where, and the picture fitted to its box.
        self._painted: tuple[int, int, int, int] | None = None  # visible region, screen cells
        self._canvas: tuple[tuple[int, int, int, int], PILImage] | None = None
        self._crop_cache: tuple[tuple, str] | None = None
        # Animation: played by the terminal where it can, else stepped through by us.
        wants = (_animate if animate is None else animate) and frames.is_animated(image)
        mode = _animation_mode(self.protocol) if wants else ""
        if wants and mode != "native" and protocol is None and _better_for_animation():
            # e.g. iTerm2: placeholders for still pictures, its own GIFs for moving ones.
            self.protocol, mode = "iterm2", "native"
        if mode == "stepped" and self.protocol != "kitty":
            mode = ""  # stepping is for placeholder images only
        self._animation = mode
        self._animating = bool(mode)
        self._frames: tuple[tuple, list] | None = None  # (key, [(canvas frame, gap ms)])
        self._paused = False
        self._steps: list[tuple[bytes, int]] = []  # stepped animation: encoded frames
        self._step_index = 0
        self._spare_id = kitty.new_image_id()  # stepped: the other buffer

    @property
    def animated(self) -> bool:
        """Is this picture moving (an animation the terminal plays)? False for a still
        picture, and for an animation shown still (not supported here, or too long)."""
        return self._animating

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
        return Strip.blank(columns, self.rich_style)  # overlay: painted after the frame

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

    def on_mount(self) -> None:
        if self.protocol in OVERLAY:
            _Painter.for_app(self.app).widgets.add(self)
        if self._animating and self.protocol == "kitty":
            self.set_interval(0.5, self._pause_when_hidden)

    def _pause_when_hidden(self) -> None:
        """Stop an animation nobody can see; start it again on return."""
        if self._sent_box is None:
            return
        hidden = self._visible(self.screen) is None if self.is_attached else True
        if hidden != self._paused:
            self._paused = hidden
            if self._animation == "native":
                self._write(kitty.animate(self._image_id, run=not hidden))

    def _step(self) -> None:
        """Stepped animation, double-buffered: send the next frame as a whole image under
        the spare id (with its own placement), then switch the placeholder text to it.
        Terminals that don't redraw in-place edits (Rex) still show every frame."""
        if not self.is_attached or not self._steps:
            return
        gap = self._steps[self._step_index][1]
        if self._sent_box is not None and self._placed is not None and not self._paused:
            self._step_index = (self._step_index + 1) % len(self._steps)
            png, gap = self._steps[self._step_index]
            columns, rows = self._placed
            shown, spare = self._image_id, self._spare_id
            self._write(kitty.delete(spare) + kitty.transmit(spare, png) + kitty.place(spare, columns, rows))
            _registry.remove(shown)
            _registry.add(spare, self._sent_box[0] * self._sent_box[1] * 4, self)
            self._image_id, self._spare_id = spare, shown
            self.refresh()  # the placeholder text now names the new image
        self.set_timer(max(gap, frames.MIN_GAP) / 1000, self._step)

    # Overlay protocols: painted at the cursor after each frame ---------------------------

    def _overlay_paint(self, screen, update) -> str:
        """Codes to (re)paint the visible part, if it moved or its cells were redrawn."""
        visible = self._visible(screen)
        if visible is None:
            hide = self._overlay_hide()
            self._painted = None
            return hide
        if visible == self._painted and not _touched(update, visible):
            return ""
        self._painted = visible
        try:
            return self._overlay_codes(visible)
        except Exception:
            return ""  # never break the app's drawing over a picture

    def _visible(self, screen) -> tuple[int, int, int, int] | None:
        if not self.is_attached or self.screen is not screen or self.app.screen is not screen:
            return None
        try:
            geometry = screen._compositor.find_widget(self)
        except Exception:
            return None
        content = self.content_region
        visible = geometry.clip.intersection(content)
        if visible.width <= 0 or visible.height <= 0:
            return None
        return visible.x, visible.y, visible.width, visible.height

    def _overlay_canvas(self) -> tuple[PILImage, tuple[int, int]]:
        """The picture fitted (aspect kept, centred) into the widget's cells, as RGBA the
        exact pixel size of those cells, so any group of cells is a clean crop."""
        from PIL import Image as Pillow

        columns, rows = self.content_size
        cell_width, cell_height = cell_size()
        key = (columns, rows, cell_width, cell_height)
        if self._canvas is None or self._canvas[0] != key:
            box = (columns * cell_width, rows * cell_height)
            scale = min(box[0] / self.image_width, box[1] / self.image_height)
            size = (max(1, round(self.image_width * scale)), max(1, round(self.image_height * scale)))
            picture = self.image.convert("RGBA").resize(size, Pillow.Resampling.LANCZOS)
            canvas = Pillow.new("RGBA", box, (0, 0, 0, 0))
            canvas.paste(picture, ((box[0] - size[0]) // 2, (box[1] - size[1]) // 2))
            self._canvas = (key, canvas)
            if self.protocol == "kitty-direct" and self._sent_box is not None:
                self._write(kitty.delete(self._image_id))  # the old size's data
                _registry.remove(self._image_id)
                self._sent_box = None
        return self._canvas[1], (cell_width, cell_height)

    def _overlay_codes(self, visible: tuple[int, int, int, int]) -> str:
        x, y, width, height = visible
        content = self.content_region
        canvas, (cell_width, cell_height) = self._overlay_canvas()
        crop = ((x - content.x) * cell_width, (y - content.y) * cell_height, width * cell_width, height * cell_height)
        move = f"\x1b[{y + 1};{x + 1}H"
        if self.protocol == "kitty-direct":
            codes = ""
            if self._sent_box is None:
                moving = self._canvas_frames(canvas) if self._animating else None
                size = canvas.width * canvas.height * 4 * (len(moving) if moving else 1)
                codes += "".join(kitty.delete(old) for old in _registry.make_room(size, keep=self._image_id))
                if moving:
                    codes += _kitty_animation(self._image_id, [(_png(picture), gap) for picture, gap in moving])
                else:
                    codes += kitty.transmit(self._image_id, _png(canvas))
                _registry.add(self._image_id, size, self)
                self._sent_box = canvas.size
            _registry.touch(self._image_id)
            return codes + move + kitty.place_here(self._image_id, width, height, crop)
        key = (self.protocol, crop, canvas.size)
        if self._crop_cache is None or self._crop_cache[0] != key:
            left, top, crop_width, crop_height = crop
            piece = canvas.crop((left, top, left + crop_width, top + crop_height))
            if self.protocol == "sixel":
                # Whole bands of six pixels only: a partial band would reach the row below
                # (and at the bottom of the screen, scroll it).
                piece = piece.crop((0, 0, crop_width, crop_height - crop_height % 6))
                code = sixel.encode(piece)
            elif self._animating and crop == (0, 0, canvas.width, canvas.height) and (
                moving := self._canvas_frames(canvas)
            ):
                # Fully visible: the terminal plays a GIF itself. Partly visible: a still crop.
                code = iterm2.inline(_gif(moving), width, height)
            else:
                code = iterm2.inline(_png(piece), width, height)
            self._crop_cache = (key, code)
        return move + self._crop_cache[1]

    def _frame_budget(self) -> int:
        """Decoded bytes an animation's frames may take. Played natively, the terminal keeps
        every frame (half its image budget at most); stepped, it only ever holds two, and
        the frames wait here."""
        if self._animation == "stepped":
            return frames.MAX_BYTES
        return min(frames.MAX_BYTES, _registry.budget // 2)

    def _canvas_frames(self, canvas) -> list | None:
        """The animation's frames, each fitted and padded like the canvas (cached)."""
        from PIL import Image as Pillow

        key = canvas.size
        if self._frames is None or self._frames[0] != key:
            box = canvas.size

            def fit(picture):
                scale = min(box[0] / picture.width, box[1] / picture.height)
                size = (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
                padded = Pillow.new("RGBA", box, (0, 0, 0, 0))
                padded.paste(picture.resize(size, Pillow.Resampling.LANCZOS), ((box[0] - size[0]) // 2, (box[1] - size[1]) // 2))
                return padded

            moving = frames.frames(self.image, fit, max_bytes=self._frame_budget())
            if moving is None:
                self._animating = False  # too long or too big: still
            self._frames = (key, moving or [])
        return self._frames[1] or None

    def _overlay_hide(self) -> str:
        if self.protocol == "kitty-direct" and self._painted is not None:
            return kitty.delete_placement(self._image_id)
        return ""  # Sixel and iTerm2 pictures live in their cells: Textual redraws over them

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
        def fit(picture):
            picture = picture.copy()
            picture.thumbnail(box)  # only ever shrinks; the terminal fits it to the cells
            return picture if picture.mode in ("RGB", "RGBA") else picture.convert("RGBA")

        moving = None
        if self._animating:
            moving = frames.frames(self.image, fit, max_bytes=self._frame_budget())
            if moving is None and self._animation == "native":
                # Too big for the terminal to keep every frame: step through them instead
                # (it then holds two at a time).
                moving = frames.frames(self.image, fit, max_bytes=frames.MAX_BYTES)
                if moving is not None:
                    self.app.call_from_thread(setattr, self, "_animation", "stepped")
            if moving is None:
                self.app.call_from_thread(setattr, self, "_animating", False)  # still, then
        pictures = moving or [(fit(self.image), 0)]
        encoded = [(_png(picture), gap) for picture, gap in pictures]
        self.app.call_from_thread(self._sent, encoded, pictures[0][0].size, box, columns, rows)

    def _sent(self, encoded: list, size: tuple[int, int], box: tuple[int, int], columns: int, rows: int) -> None:
        if not self.is_attached:
            return
        decoded = size[0] * size[1] * 4 * len(encoded)  # what the terminal keeps (every frame)
        codes = [kitty.delete(old) for old in _registry.make_room(decoded, keep=self._image_id)]
        if self._sent_box is not None:
            codes.append(kitty.delete(self._image_id))  # replacing its data needs a delete
        if len(encoded) > 1 and self._animation == "native":
            codes.append(_kitty_animation(self._image_id, encoded))
        else:
            codes.append(kitty.transmit(self._image_id, encoded[0][0]))
        codes.append(kitty.place(self._image_id, columns, rows))
        self._paused = False
        if len(encoded) > 1 and self._animation == "stepped":
            first = not self._steps
            self._steps, self._step_index = encoded, 0
            if first:
                self.set_timer(encoded[0][1] / 1000, self._step)
        self._write("".join(codes))
        _registry.add(self._image_id, decoded, self)
        self._sent_box, self._placed, self._sending = box, (columns, rows), False
        self.refresh()

    def _evicted(self) -> None:
        """The registry deleted this picture from the terminal: send it again when drawn."""
        self._sent_box = self._placed = None

    def on_unmount(self) -> None:
        if self.protocol in OVERLAY:
            _Painter.for_app(self.app).widgets.discard(self)
        if self._sent_box is not None and self.protocol in ("kitty", "kitty-direct"):
            self._write(kitty.delete(self._image_id))
            if self._steps:
                self._write(kitty.delete(self._spare_id))
        _registry.remove(self._image_id)
        self._sent_box = self._placed = None

    def _write(self, codes: str) -> None:
        driver = getattr(self.app, "_driver", None)
        if driver is not None:
            if _capabilities.tmux:
                codes = _through_tmux(codes)
            driver.write(codes)
            driver.flush()


OVERLAY = ("iterm2", "kitty-direct", "sixel")
"""Protocols that paint at the cursor, after Textual has drawn the frame."""


def _animation_mode(protocol: str) -> str:
    """How animations move in this protocol here: ``native``, ``stepped`` or ``""``."""
    return Capabilities(protocol, terminal=_capabilities.terminal, version=_capabilities.version).animation


def _better_for_animation() -> bool:
    """The terminal can't animate in its usual protocol but plays iTerm2 GIFs (iTerm2)."""
    return "iterm2" in _capabilities.supported and not _capabilities.tmux


def _kitty_animation(image_id: int, encoded: list) -> str:
    """Codes for an animation: the first frame as the image, the rest as frames, its
    first frame's gap, then play it, looping."""
    (first, first_gap), rest = encoded[0], encoded[1:]
    codes = [kitty.transmit(image_id, first)]
    codes += [kitty.frame(image_id, png, gap) for png, gap in rest]
    codes.append(kitty.animate(image_id, frame=1, gap=first_gap))
    codes.append(kitty.animate(image_id, run=True))
    return "".join(codes)


def _gif(moving: list) -> bytes:
    """An animated GIF of these (RGBA picture, gap) frames, looping forever."""
    pictures = [picture for picture, _ in moving]
    buffer = io.BytesIO()
    pictures[0].save(
        buffer, format="GIF", save_all=True, append_images=pictures[1:],
        duration=[gap for _, gap in moving], loop=0, disposal=2,
    )
    return buffer.getvalue()


def _png(picture) -> bytes:
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG", compress_level=1)
    return buffer.getvalue()


def _touched(update, visible: tuple[int, int, int, int]) -> bool:
    """Did this frame redraw any of these cells (erasing what was painted there)?"""
    from textual._compositor import ChopsUpdate, LayoutUpdate

    x, y, width, height = visible
    if isinstance(update, ChopsUpdate):
        return any(y <= line < y + height and start < x + width and end > x for line, start, end in update.spans)
    if isinstance(update, LayoutUpdate):
        region = update.region
        return region.x < x + width and region.right > x and region.y < y + height and region.bottom > y
    return True


class _Painter:
    """Paints the overlay pictures of one app after each frame it draws."""

    def __init__(self, app) -> None:
        self.app = app
        self.widgets: set[Image] = set()
        self._display = app._display
        app._display = self._display_and_paint

    @classmethod
    def for_app(cls, app) -> "_Painter":
        painter = getattr(app, "_cellpix_painter", None)
        if painter is None:
            painter = app._cellpix_painter = cls(app)
        return painter

    def _display_and_paint(self, screen, renderable) -> None:
        self._display(screen, renderable)
        if renderable is None or not self.widgets or getattr(self.app, "_batch_count", 0):
            return
        codes = "".join(widget._overlay_paint(screen, renderable) for widget in list(self.widgets))
        self._write(codes)

    def paint_now(self, widget: "Image") -> int:
        """Paint one picture over its cells at once, without Textual redrawing them first
        (a redraw blanks them for a moment: flicker). Returns the bytes written."""
        if widget not in self.widgets or getattr(self.app, "_batch_count", 0):
            return 0
        codes = widget._overlay_paint(self.app.screen, None)  # None: repaint whatever's visible
        self._write(codes)
        return len(codes)

    def _write(self, codes: str) -> None:
        driver = getattr(self.app, "_driver", None)
        if codes and driver is not None:
            # Inside a synchronized update, cursor saved and restored around the painting.
            driver.write("\x1b[?2026h\x1b7" + codes + "\x1b8\x1b[?2026l")
            driver.flush()


def _through_tmux(codes: str) -> str:
    """Each graphics escape code wrapped for tmux's passthrough (one per code: tmux
    limits how much one passthrough sequence may carry)."""
    from cellpix.probe import _wrap

    return "".join(_wrap(code + "\x1b\\", tmux=True) for code in codes.split("\x1b\\") if code)
