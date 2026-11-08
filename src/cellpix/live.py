"""Live pictures: a widget you keep pushing frames into (a browser view, a camera, a game).

    live = LiveImage((640, 400))
    live.push(frame)            # from any thread; the newest frame wins

Frames are shown at most ``fps`` times a second, and not at all while the widget is out
of view. Each frame is fitted into the widget's exact pixel box (centred, letterboxed),
so cells map to known pixels: clicks and scrolls come back as
:class:`LiveImage.Clicked` / :class:`LiveImage.Scrolled` in the frame's own pixels.

With Kitty placeholders, only what changed is sent: the bounding box of the difference
from the last frame shown, as an in-place edit of the image (``a=f`` with ``r=1``),
in the format the terminal takes in fastest (raw pixels + zlib; PNG for iTerm2). Where
edits aren't known to redraw, whole frames are swapped between two image ids instead.
Overlay protocols (Sixel, iTerm2, Kitty placements) and half blocks repaint the widget.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from textual import events
from textual.message import Message

from cellpix import kitty
from cellpix.textual import OVERLAY, Image, _png, _registry, capabilities, cell_size

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


class LiveImage(Image):
    """A picture that keeps changing. Size it with CSS like :class:`cellpix.textual.Image`."""

    class Clicked(Message):
        """The picture was clicked, at (x, y) in the frame's own pixels."""

        def __init__(self, live: "LiveImage", x: int, y: int, button: int) -> None:
            super().__init__()
            self.live, self.x, self.y, self.button = live, x, y, button

    class Scrolled(Message):
        """The mouse wheel turned over the picture: ``delta`` is -1 (up) or 1 (down)."""

        def __init__(self, live: "LiveImage", x: int, y: int, delta: int) -> None:
            super().__init__()
            self.live, self.x, self.y, self.delta = live, x, y, delta

    def __init__(
        self,
        size: tuple[int, int] = (640, 400),
        *,
        fps: float = 15.0,
        protocol: str | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        from PIL import Image as Pillow

        super().__init__(Pillow.new("RGB", size), protocol=protocol, animate=False, name=name, id=id, classes=classes)
        self.fps = fps
        self._lock = threading.Lock()
        self._pending: PILImage | None = None
        self._shown: PILImage | None = None  # the canvas the terminal has (Kitty placeholders)
        self._geometry: tuple[float, int, int] = (1.0, 0, 0)  # scale, x and y offset of the frame
        self._swapping_id = kitty.new_image_id()  # where edits aren't known to redraw
        self.frames_shown = 0
        self.bytes_sent = 0
        self.busy_seconds = 0.0  # main-thread time spent showing frames

    # Frames in ------------------------------------------------------------------------

    def push(self, frame: "PILImage") -> None:
        """Show this frame next. Safe from any thread; frames pushed faster than ``fps``
        are dropped (the newest wins)."""
        with self._lock:
            self._pending = frame

    def on_mount(self) -> None:  # (Image.on_mount runs too: Textual calls each class's)
        self.set_interval(1 / self.fps, self._flush)

    def _flush(self) -> None:
        with self._lock:
            frame, self._pending = self._pending, None
        if frame is None:
            return
        if self.screen is not self.app.screen or self._visible(self.screen) is None:
            with self._lock:  # out of view: keep it for when it's back
                if self._pending is None:
                    self._pending = frame
            return
        import time

        started = time.perf_counter()
        self.image = frame
        self.frames_shown += 1
        try:
            self._show_frame()
        finally:
            self.busy_seconds += time.perf_counter() - started

    def _show_frame(self) -> None:
        if self.protocol == "kitty":
            self._kitty_update()
        elif self.protocol in OVERLAY:
            self._canvas = self._crop_cache = None
            if self.protocol == "kitty-direct" and self._sent_box is not None:
                self._write(kitty.delete(self._image_id))  # new data under the same id
                _registry.remove(self._image_id)
                self._sent_box = None
            self.bytes_sent += self._repaint()  # painted straight over the old frame
        else:
            self._halfblock = None
            self._repaint()

    # Kitty placeholders ----------------------------------------------------------------

    def _fit(self, frame: "PILImage") -> "PILImage":
        """The frame fitted into the widget's pixel box, centred on black (RGB)."""
        from PIL import Image as Pillow

        columns, rows = self.content_size
        cell_width, cell_height = cell_size()
        box = (max(1, columns * cell_width), max(1, rows * cell_height))
        scale = min(box[0] / frame.width, box[1] / frame.height)
        size = (max(1, round(frame.width * scale)), max(1, round(frame.height * scale)))
        offset = ((box[0] - size[0]) // 2, (box[1] - size[1]) // 2)
        self._geometry = (scale, *offset)
        canvas = Pillow.new("RGB", box)
        picture = frame if frame.size == size else frame.resize(size, Pillow.Resampling.BILINEAR)
        if picture.mode in ("RGBA", "LA", "P"):
            picture = picture.convert("RGBA")
            canvas.paste(picture, offset, picture)
        else:
            canvas.paste(picture.convert("RGB"), offset)
        return canvas

    def _send(self, columns: int, rows: int) -> None:
        """Send the whole current frame and place it (first time, or the size changed)."""
        canvas = self._fit(self.image)
        codes = ""
        if self._sent_box is not None:
            codes += kitty.delete(self._image_id)
        codes += self._whole(self._image_id, canvas) + kitty.place(self._image_id, columns, rows)
        self._write_counted(codes)
        _registry.add(self._image_id, canvas.width * canvas.height * 4, self)
        self._shown, self._sent_box, self._placed, self._sending = canvas, canvas.size, (columns, rows), False
        self.refresh()

    def _kitty_update(self) -> None:
        from PIL import ImageChops

        if self._sent_box is None or self._placed is None:
            return  # the first render sends it whole
        canvas = self._fit(self.image)
        if self._shown is None or canvas.size != self._shown.size:
            self._send(*self._placed)
            return
        changed = ImageChops.difference(self._shown, canvas).getbbox()
        if changed is None:
            return
        if capabilities().edits:
            left, top, right, bottom = changed
            piece = canvas.crop(changed)
            if capabilities().frame_format == "png":
                code = kitty.edit(self._image_id, _png(piece), left, top)
            else:
                code = kitty.edit_pixels(self._image_id, piece, left, top)
            self._write_counted(code)
        else:
            # Whole frames, double-buffered: the spare id gets the frame and a placement,
            # then the placeholder text switches to it.
            columns, rows = self._placed
            spare = self._swapping_id
            self._write_counted(kitty.delete(spare) + self._whole(spare, canvas) + kitty.place(spare, columns, rows))
            _registry.remove(self._image_id)
            _registry.add(spare, canvas.width * canvas.height * 4, self)
            self._image_id, self._swapping_id = spare, self._image_id
            self.refresh()
        self._shown = canvas

    def _whole(self, image_id: int, canvas: "PILImage") -> str:
        if capabilities().frame_format == "png":
            return kitty.transmit(image_id, _png(canvas))
        return kitty.transmit_pixels(image_id, canvas)

    def _write_counted(self, codes: str) -> None:
        self.bytes_sent += len(codes)
        self._write(codes)

    def on_unmount(self) -> None:  # (Image.on_unmount deletes the shown id)
        if self.protocol == "kitty" and self._sent_box is not None:
            self._write(kitty.delete(self._swapping_id))

    # Mouse -------------------------------------------------------------------------------

    def to_pixels(self, pointer_x: float, pointer_y: float) -> tuple[int, int] | None:
        """A pointer position (in cells, relative to the widget, fractions allowed) as a
        pixel of the frame, or None outside the frame (in the letterbox)."""
        gutter = self.gutter
        cell_width, cell_height = cell_size()
        if self.protocol != "kitty":
            self._fit(self.image)  # the same fitting: keeps the geometry current
        scale, offset_x, offset_y = self._geometry
        x = ((pointer_x - gutter.left) * cell_width - offset_x) / scale
        y = ((pointer_y - gutter.top) * cell_height - offset_y) / scale
        if 0 <= x < self.image.width and 0 <= y < self.image.height:
            return int(x), int(y)
        return None

    def on_click(self, event: events.Click) -> None:
        if (where := self.to_pixels(event.pointer_x, event.pointer_y)) is not None:
            event.stop()
            self.post_message(self.Clicked(self, *where, event.button))

    def _scrolled(self, event: events.MouseEvent, delta: int) -> None:
        if (where := self.to_pixels(event.pointer_x, event.pointer_y)) is not None:
            event.stop()
            self.post_message(self.Scrolled(self, *where, delta))

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._scrolled(event, -1)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._scrolled(event, 1)
