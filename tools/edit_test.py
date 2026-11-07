"""Do in-place edits show? A red square glides across a picture, sent as small partial
edits of frame 1 (a=f, r=1, x, y) every 100 ms. Run it in the terminal to check.

    uv run python tools/edit_test.py [log-file]
"""

from __future__ import annotations

import io
import sys
import time

from PIL import Image as PILImage
from textual.app import App, ComposeResult
from textual.widgets import Static

import cellpix.textual as ct
from cellpix import kitty

SIZE, SQUARE, STEP = (320, 160), 24, 8
BACKGROUND = (30, 60, 110)


def png(picture) -> bytes:
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG")
    return buffer.getvalue()


class EditTest(App):
    CSS = "Image { width: 40; height: 10; } Static { color: $text-muted; }"
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, log_path: str | None) -> None:
        super().__init__()
        self.log = open(log_path, "w", buffering=1) if log_path else None
        self.base = PILImage.new("RGB", SIZE, BACKGROUND)
        self.picture = ct.Image(self.base, protocol="kitty", animate=False)
        self.x, self.edits = 0, 0

    def compose(self) -> ComposeResult:
        found = ct.capabilities()
        yield Static(f"terminal {found.terminal} {found.version}: the red square should glide across; q quits")
        yield self.picture
        yield Static("", id="count")

    def on_mount(self) -> None:
        self.set_interval(0.1, self.move)

    def move(self) -> None:
        if self.picture._sent_box is None:
            return  # not sent yet
        old_x, self.x = self.x, (self.x + STEP) % (SIZE[0] - SQUARE)
        # One edit covering where the square was and where it is now.
        left, right = min(old_x, self.x), max(old_x, self.x) + SQUARE
        if self.x < old_x:  # wrapped round: two separate spots, send both
            spots = [(old_x, old_x + SQUARE), (self.x, self.x + SQUARE)]
        else:
            spots = [(left, right)]
        codes = ""
        for start, end in spots:
            patch = PILImage.new("RGB", (end - start, SQUARE), BACKGROUND)
            square_start = self.x - start
            if 0 <= square_start < end - start:
                patch.paste((220, 40, 40), (square_start, 0, square_start + SQUARE, SQUARE))
            codes += kitty.edit(self.picture._image_id, png(patch), start, (SIZE[1] - SQUARE) // 2)
        self.picture._write(codes)
        self.edits += 1
        self.query_one("#count", Static).update(f"{self.edits} edits sent ({len(codes)} bytes the last)")
        if self.log:
            self.log.write(f"{time.monotonic():.2f} edit x={self.x} {len(codes)}B {codes[:60]!r}\n")


if __name__ == "__main__":
    ct.configure()
    EditTest(sys.argv[1] if len(sys.argv) > 1 else None).run()
