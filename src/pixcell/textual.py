"""An image widget for Textual: Kitty graphics where the terminal has them, half blocks
elsewhere.

Probe the terminal before the app starts (the probe needs the tty to itself)::

    import pixcell.textual
    pixcell.textual.configure()          # runs pixcell.probe.detect()
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
import os
import time
from typing import TYPE_CHECKING

from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget

from pixcell import frames, halfblock, iterm2, kitty, sixel
from pixcell.probe import Capabilities, detect

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

_capabilities = Capabilities("halfblock", reason="pixcell.textual.configure() wasn't called")


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


def animations_play() -> bool:
    """Do animated pictures (GIF, WebP, APNG) move here? The same choice :class:`Image`
    makes for each one, which may take them to another protocol the terminal has (iTerm2
    GIFs in iTerm2, Sixel in WezTerm), for telling a user or an agent what to expect.
    False with ``configure(animate=False)`` or ``PIXCELL_ANIMATION=0``."""
    if not _animate:
        return False
    mode = _capabilities.animation
    return bool(mode) or _better_for_animation(mode) is not None


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
        if wants and mode != "native" and protocol is None and (better := _better_for_animation(mode)):
            # e.g. iTerm2: placeholders for still pictures, its own GIFs for moving ones;
            # WezTerm: iTerm2 images for still pictures, Sixel stepped for moving ones.
            self.protocol, mode = better
        if mode == "stepped" and self.protocol not in STEPPED:
            mode = ""  # Kitty placements at the cursor would flicker
        self._animation = mode
        self._animating = bool(mode)
        self._frames: tuple[tuple, list] | None = None  # (key, [(canvas frame, gap ms)])
        self._paused = False
        # Stepped animation: each frame encoded for the protocol (Kitty: PNG; Sixel: the
        # escape code; half blocks: RGBA pixels), made for the size in _steps_key.
        self._steps: list[tuple[bytes, int]] = []
        self._steps_key: tuple | None = None
        self._steps_complete = False  # every frame is in _steps (else more are coming)
        self._step_index = 0
        self._stepping = False  # a step timer is pending
        self._preparing = False  # a worker is encoding frames (Sixel, half blocks)
        self._step_strips: dict[int, list[Strip]] = {}  # half blocks: frames drawn as strips
        self._spare_id = kitty.new_image_id()  # stepped Kitty: the other buffer
        # The first frame, for drawing still on the main thread while a worker steps
        # through the animation's frames (seeking the image as it goes).
        self._first = image.copy() if self._animating else None
        self._gif: tuple[tuple[int, int], bytes] | None = None  # iTerm2: (canvas size, GIF)

    @property
    def _picture(self) -> "PILImage":
        """The picture to draw when not stepping: the first frame of an animation."""
        return self._first if self._first is not None else self.image

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
        if self._animating:
            if self._steps and self._steps_key == (columns, rows):
                return self._halfblock_frame(columns, rows)
            self._prepare_steps((columns, rows))  # still until the frames are ready
        if self._halfblock is None or self._halfblock[0] != (columns, rows):
            self._halfblock = ((columns, rows), _strips(self._picture, columns, rows))
        return self._halfblock[1]

    def _halfblock_frame(self, columns: int, rows: int) -> list[Strip]:
        """The current frame's strips: all kept if they're small, else only the current one."""
        from PIL import Image as Pillow

        index = self._step_index
        if (strips := self._step_strips.get(index)) is None:
            if len(self._steps) * columns * rows > HALFBLOCK_CACHE_CELLS:
                self._step_strips.clear()
            picture = Pillow.frombytes("RGBA", (columns, rows * 2), self._steps[index][0])
            strips = self._step_strips[index] = _strips(picture, columns, rows)
        return strips

    def on_resize(self) -> None:
        self._halfblock = None
        self.refresh()

    def on_mount(self) -> None:
        if self.protocol in OVERLAY:
            _Painter.for_app(self.app).widgets.add(self)
        if self._animating and self.protocol in STEPPED:
            self.set_interval(0.5, self._pause_when_hidden)

    def _pause_when_hidden(self) -> None:
        """Stop an animation nobody can see; start it again on return."""
        if self.protocol == "kitty" and self._sent_box is None:
            return
        hidden = self._visible(self.screen) is None if self.is_attached else True
        if hidden != self._paused:
            self._paused = hidden
            if self._animation == "native":
                self._write(kitty.animate(self._image_id, run=not hidden))

    def _step(self) -> None:
        """Stepped animation: on to the next frame, if the current one can be shown."""
        self._stepping = False
        if not self.is_attached or not self._animating or not self._steps:
            return
        if not self._paused and self._step_ready():
            following = self._step_index + 1
            # Frames still being made: play what's there, and wait on the newest frame
            # if playback catches up (round to the start only once they're all made).
            if following < len(self._steps) or self._steps_complete:
                self._step_index = following % len(self._steps)
                self._show_step()
        self._schedule_step()

    def _schedule_step(self) -> None:
        """Start the step timer, unless it's running (one chain of timers per widget)."""
        if self._stepping or not self._steps:
            return
        self._stepping = True
        gap = self._steps[self._step_index % len(self._steps)][1]
        self.set_timer(max(gap, frames.MIN_GAP) / 1000, self._step)

    def _step_ready(self) -> bool:
        """Are the frames made for how the picture is shown now? (Not while it's being
        sent again, or before frames for its new size are ready.) Sixel only steps while
        fully visible: a part would need every frame encoded again for each crop."""
        if self.protocol == "kitty":
            return self._sent_box is not None and self._placed is not None
        if self.protocol == "halfblock":
            return self._steps_key == tuple(self.content_size)
        return self._steps_key == self._sixel_key() and self._fully_visible()

    def _show_step(self) -> None:
        if self.protocol != "kitty":
            self._repaint()
            return
        # Double-buffered: send the next frame as a whole image under the spare id (with
        # its own placement), then switch the placeholder text to it. Terminals that
        # don't redraw in-place edits (Rex) still show every frame.
        png = self._steps[self._step_index][0]
        columns, rows = self._placed
        shown, spare = self._image_id, self._spare_id
        self._write(kitty.delete(spare) + kitty.transmit(spare, png) + kitty.place(spare, columns, rows))
        _registry.remove(shown)
        _registry.add(spare, self._sent_box[0] * self._sent_box[1] * 4, self)
        self._image_id, self._spare_id = spare, shown
        self.refresh()  # the placeholder text now names the new image

    def _repaint(self) -> int:
        """Show the current frame now. Overlay pictures are painted straight over the old
        one: asking Textual to redraw the cells would blank them first, and the blank
        shows as flicker. Text (half blocks, placeholders) is redrawn. Returns the bytes
        written by painting (0 for a redraw, which Textual writes later)."""
        if self.protocol in OVERLAY:
            return _Painter.for_app(self.app).paint_now(self)
        self.refresh()
        return 0

    def _prepare_steps(self, key: tuple) -> None:
        """Encode every frame for Sixel or half blocks at this size, in a worker."""
        if self._preparing or not self.is_attached:
            return
        from PIL import Image as Pillow

        self._preparing = True
        if self.protocol == "sixel":
            columns, rows, cell_width, cell_height, background = key
            box = (columns * cell_width, rows * cell_height)

            def fit(picture):
                return _fit_into(picture, box)

            def encode(picture):
                # Opaque everywhere: a transparent pixel would let the last frame show.
                flat = Pillow.new("RGBA", picture.size, (*background, 255))
                flat.alpha_composite(picture)
                return sixel.encode(flat.crop((0, 0, flat.width, flat.height - flat.height % 6))).encode("ascii")
        else:
            columns, rows = key

            def fit(picture):
                return picture.resize((columns, rows * 2), Pillow.Resampling.LANCZOS)

            def encode(picture):
                return picture.tobytes()

        def work() -> None:
            # Frames are handed over as they're made (the first at once, then every
            # quarter second), so the animation starts before the last is encoded.
            batch: list[tuple[bytes, int]] = []
            first, handed, encoded, failed = True, 0.0, 0, False
            try:
                for data, gap, _size in frames.each_encoded_frame(self.image, fit, encode):
                    encoded += len(data)
                    if encoded > frames.MAX_ENCODED_BYTES:
                        failed = True  # too big even encoded: still
                        break
                    batch.append((data, gap))
                    if first or time.monotonic() - handed > STEP_BATCH_SECONDS:
                        self.app.call_from_thread(self._frames_made, key, batch, first, False, False)
                        batch, first, handed = [], False, time.monotonic()
            except Exception:
                failed = True
            self.app.call_from_thread(self._frames_made, key, batch, first, True, failed)

        # Later: this is asked for while drawing (render_line, or painting after a frame).
        self.call_later(self.run_worker, work, thread=True, exit_on_error=False)

    def _frames_made(self, key: tuple, batch: list, first: bool, done: bool, failed: bool) -> None:
        """More frames from the worker (``first``: the start of a new set, for ``key``)."""
        if done:
            self._preparing = False
        if not self.is_attached:
            return
        if first:
            self._steps, self._steps_key, self._step_strips = [], key, {}
            self._steps_complete, self._step_index = False, 0
        self._steps.extend(batch)
        if done:
            if failed or len(self._steps) < 2:
                self._animating, self._steps = False, []  # too long, too big, or still: still
            else:
                self._steps_complete = True
        if self._animating and self._steps:
            self._schedule_step()
        if self.protocol == "halfblock":
            self.refresh()  # its frames, or (if it moved on meanwhile) a new size to prepare
        elif done and self._animating and self._steps_key != self._sixel_key():
            self._prepare_steps(self._sixel_key())  # resized while encoding
        elif first or done:
            self._repaint()

    def _sixel_key(self) -> tuple:
        columns, rows = self.content_size
        background = self.background_colors[1].rgb
        return (columns, rows, *cell_size(), background)

    def _fully_visible(self) -> bool:
        if not self.is_attached:
            return False
        content = self.content_region
        return self._visible(self.screen) == (content.x, content.y, content.width, content.height)

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
        exact pixel size of those cells, so any group of cells is a clean crop. Fitted by
        the picture's own size: a LiveImage's frames needn't be the size it started at."""
        columns, rows = self.content_size
        cell_width, cell_height = cell_size()
        key = (columns, rows, cell_width, cell_height)
        if self._canvas is None or self._canvas[0] != key:
            box = (columns * cell_width, rows * cell_height)
            self._canvas = (key, _fit_into(self._picture, box))
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
        if self.protocol == "sixel" and self._animating:
            # Stepped: each frame encoded once, for the whole picture. Partly visible, it
            # shows still (below) until it's whole again.
            steps_key = self._sixel_key()
            if self._steps and self._steps_key == steps_key:
                if crop == (0, 0, canvas.width, canvas.height):
                    return move + self._steps[self._step_index][0].decode("ascii")
            else:
                self._prepare_steps(steps_key)
        whole = crop == (0, 0, canvas.width, canvas.height)
        moving = self._iterm2_gif(canvas.size) if self.protocol == "iterm2" and self._animating and whole else None
        key = (self.protocol, crop, canvas.size, moving is not None)
        if self._crop_cache is None or self._crop_cache[0] != key:
            left, top, crop_width, crop_height = crop
            piece = canvas.crop((left, top, left + crop_width, top + crop_height))
            if self.protocol == "sixel":
                # Whole bands of six pixels only: a partial band would reach the row below
                # (and at the bottom of the screen, scroll it).
                piece = piece.crop((0, 0, crop_width, crop_height - crop_height % 6))
                code = sixel.encode(piece)
            elif moving is not None:
                # Fully visible: the terminal plays a GIF itself. Partly visible: a still crop.
                code = iterm2.inline(moving, width, height)
            else:
                code = iterm2.inline(_png(piece), width, height)
            self._crop_cache = (key, code)
        return move + self._crop_cache[1]

    def _iterm2_gif(self, box: tuple[int, int]) -> bytes | None:
        """The animation as a GIF the size of the canvas, once a worker has made it (still
        until then: making it can take seconds)."""
        if self._gif is not None and self._gif[0] == box:
            return self._gif[1]
        if not self._preparing:
            self._preparing = True

            def work() -> None:
                try:
                    data = frames.gif(self.image, lambda picture: _fit_into(picture, box))
                except Exception:
                    data = None
                self.app.call_from_thread(self._gif_made, box, data)

            self.call_later(self.run_worker, work, thread=True, exit_on_error=False)
        return None

    def _gif_made(self, box: tuple[int, int], data: bytes | None) -> None:
        self._preparing = False
        if data is None:
            self._animating = False  # too long or too big: still
        else:
            self._gif = (box, data)
        if self.is_attached:
            self._repaint()  # the GIF (or, if it moved on meanwhile, one for the new size)

    def _frame_budget(self) -> int:
        """Decoded bytes an animation's frames may take. Played natively, the terminal keeps
        every frame (half its image budget at most); stepped, it only ever holds two, and
        the frames wait here."""
        if self._animation == "stepped":
            return frames.MAX_BYTES
        return min(frames.MAX_BYTES, _registry.budget // 2)

    def _canvas_frames(self, canvas) -> list | None:
        """The animation's frames, each fitted and padded like the canvas (cached)."""
        key = canvas.size
        if self._frames is None or self._frames[0] != key:
            moving = frames.frames(self.image, lambda picture: _fit_into(picture, key), max_bytes=self._frame_budget())
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
            # Encoded as they're decoded: only the PNGs are kept, so long GIFs fit too.
            moving = frames.encoded_frames(self.image, fit, _png)
            if moving is not None and self._animation == "native" and moving[2] > self._frame_budget():
                # Too big for the terminal to keep every frame: step through them instead
                # (it then holds two at a time).
                self.app.call_from_thread(setattr, self, "_animation", "stepped")
            if moving is None:
                self.app.call_from_thread(setattr, self, "_animating", False)  # still, then
        if moving is not None:
            encoded, size, _decoded = moving
        else:
            first = fit(self.image)
            encoded, size = [(_png(first), 0)], first.size
        self.app.call_from_thread(self._sent, encoded, size, box, columns, rows)

    def _sent(self, encoded: list, size: tuple[int, int], box: tuple[int, int], columns: int, rows: int) -> None:
        if not self.is_attached:
            return
        # What the terminal keeps: every frame when it plays them, two when stepped.
        kept = len(encoded) if self._animation == "native" else min(2, len(encoded))
        decoded = size[0] * size[1] * 4 * kept
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
            self._steps, self._step_index, self._steps_complete = encoded, 0, True
            self._schedule_step()
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

STEPPED = ("kitty", "sixel", "halfblock")
"""Protocols pixcell can animate itself, a frame at a time."""

STEP_BATCH_SECONDS = 0.25
"""How often a worker encoding frames hands what it has made to the widget."""

HALFBLOCK_CACHE_CELLS = 400_000
"""Half-block animations up to this many cells (frames x cells) keep every frame's
strips; bigger ones draw each frame as it comes."""


def _strips(picture, columns: int, rows: int) -> list[Strip]:
    """A picture as half-block lines."""
    strips = []
    for line in halfblock.cells(picture, columns, rows):
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
    return strips


def _fit_into(picture, box: tuple[int, int]):
    """The picture fitted (aspect kept) and centred in a transparent RGBA box."""
    from PIL import Image as Pillow

    scale = min(box[0] / picture.width, box[1] / picture.height)
    size = (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
    padded = Pillow.new("RGBA", box, (0, 0, 0, 0))
    padded.paste(picture.convert("RGBA").resize(size, Pillow.Resampling.LANCZOS), ((box[0] - size[0]) // 2, (box[1] - size[1]) // 2))
    return padded


def _animation_mode(protocol: str) -> str:
    """How animations move in this protocol here: ``native``, ``stepped`` or ``""``."""
    return Capabilities(protocol, terminal=_capabilities.terminal, version=_capabilities.version).animation


def _better_for_animation(mode: str) -> tuple[str, str] | None:
    """Another protocol the terminal has that would move this picture, when the usual one
    can't (or only steps it): (protocol, animation mode), or None. Never past a protocol
    forced with ``PIXCELL_PROTOCOL``, nor inside tmux (placeholders only there)."""
    found = _capabilities
    if found.tmux or found.reason == "PIXCELL_PROTOCOL" or os.environ.get("PIXCELL_ANIMATION") == "0":
        return None
    if "iterm2" in found.supported and found.fact("iterm2.gif_animation") is True:
        return "iterm2", "native"  # the terminal plays a GIF itself
    if not mode and "sixel" in found.supported:
        return "sixel", "stepped"  # repainted frame by frame
    return None


def _kitty_animation(image_id: int, encoded: list) -> str:
    """Codes for an animation: the first frame as the image, the rest as frames, its
    first frame's gap, then play it, looping."""
    (first, first_gap), rest = encoded[0], encoded[1:]
    codes = [kitty.transmit(image_id, first)]
    codes += [kitty.frame(image_id, png, gap) for png, gap in rest]
    codes.append(kitty.animate(image_id, frame=1, gap=first_gap))
    codes.append(kitty.animate(image_id, run=True))
    return "".join(codes)


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
        painter = getattr(app, "_pixcell_painter", None)
        if painter is None:
            painter = app._pixcell_painter = cls(app)
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
    from pixcell.probe import _wrap

    return "".join(_wrap(code + "\x1b\\", tmux=True) for code in codes.split("\x1b\\") if code)
