"""Find out what the terminal can draw, before a TUI takes it over.

Asks the terminal directly, on its tty:

- Kitty graphics: a graphics query (``a=q``) followed by a primary device attributes
  request (``CSI c``). Every terminal answers the latter; one that supports graphics
  answers the query first.
- Cell size in pixels: ``CSI 16 t``, then ``CSI 14 t`` (window size) divided by the
  cells, then the kernel's window size (``TIOCGWINSZ``).

Per-terminal rules, from running real programs in them:

- Rex reports pixel sizes through ``TIOCGWINSZ`` that can disagree with its escape-code
  answers, so there only the escape codes count; and it keeps at most 10 MB of images.
- Inside tmux, graphics need passthrough, which isn't supported yet: half blocks.

``CELLPIX_PROTOCOL=kitty|halfblock|off`` overrides the answer.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass

from cellpix import kitty

PROTOCOLS = ("kitty", "halfblock", "off")

_GRAPHICS_OK = re.compile(rb"\x1b_Gi=(\d+);OK\x1b\\")
_DEVICE_ATTRIBUTES = re.compile(rb"\x1b\[\?[\d;]*c")
_CELL_SIZE = re.compile(rb"\x1b\[6;(\d+);(\d+)t")
_WINDOW_SIZE = re.compile(rb"\x1b\[4;(\d+);(\d+)t")


@dataclass(frozen=True)
class Capabilities:
    """What the terminal can draw."""

    protocol: str
    """``kitty``, ``halfblock`` or ``off``."""
    cell_size: tuple[int, int] | None = None
    """A cell's (width, height) in pixels, if the terminal said."""
    terminal: str = ""
    """``TERM_PROGRAM``, lower-cased (e.g. ``rex``, ``ghostty``, ``wezterm``)."""
    image_budget: int = 64 * 1024 * 1024
    """Bytes of decoded images to keep in the terminal before evicting old ones."""
    reason: str = ""
    """Why the protocol was chosen (for diagnostics)."""


def detect(timeout: float = 0.5, tty_path: str = "/dev/tty") -> Capabilities:
    """Probe the terminal. Never raises: anything unexpected gives half blocks or off."""
    terminal = os.environ.get("TERM_PROGRAM", "").lower()
    budget = 8 * 1024 * 1024 if terminal == "rex" else 64 * 1024 * 1024
    forced = os.environ.get("CELLPIX_PROTOCOL", "").lower()
    if forced in PROTOCOLS and forced != "kitty":
        return Capabilities(forced, None, terminal, budget, "CELLPIX_PROTOCOL")
    if os.name != "posix":
        return Capabilities("halfblock", None, terminal, budget, "not a POSIX terminal (phase 2)")
    if os.environ.get("TMUX"):
        return Capabilities("halfblock", None, terminal, budget, "inside tmux (passthrough is phase 2)")
    try:
        answer, columns, rows, ioctl_pixels = _ask(tty_path, timeout)
    except OSError as error:
        return Capabilities("halfblock", None, terminal, budget, f"no tty ({error.strerror or error})")

    cell = _cell_size(answer, columns, rows, None if terminal == "rex" else ioctl_pixels)
    graphics = bool(_GRAPHICS_OK.search(answer))
    if forced == "kitty" or graphics:
        reason = "CELLPIX_PROTOCOL" if forced == "kitty" else "answered the graphics query"
        return Capabilities("kitty", cell, terminal, budget, reason)
    if _DEVICE_ATTRIBUTES.search(answer):
        return Capabilities("halfblock", cell, terminal, budget, "no graphics answer")
    return Capabilities("halfblock", cell, terminal, budget, "the terminal didn't answer")


def _ask(tty_path: str, timeout: float) -> tuple[bytes, int, int, tuple[int, int]]:
    """Send the queries on the tty and read the answers, the terminal in no-echo mode."""
    import fcntl
    import select
    import struct
    import termios
    import tty

    fd = os.open(tty_path, os.O_RDWR | os.O_NOCTTY)
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, columns, x_pixels, y_pixels = struct.unpack("HHHH", packed)
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd, termios.TCSANOW)  # no echo, no line buffering
            query_id = kitty.new_image_id()
            os.write(fd, (kitty.query(query_id) + "\x1b[16t\x1b[14t\x1b[c").encode())
            answer = b""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not _DEVICE_ATTRIBUTES.search(answer):
                ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
                if ready:
                    answer += os.read(fd, 4096)
            # Device attributes come last; anything after it isn't ours.
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, saved)
    finally:
        os.close(fd)
    return answer, columns, rows, (x_pixels, y_pixels)


def _cell_size(
    answer: bytes, columns: int, rows: int, ioctl_pixels: tuple[int, int] | None
) -> tuple[int, int] | None:
    if match := _CELL_SIZE.search(answer):
        height, width = int(match.group(1)), int(match.group(2))
        if width and height:
            return width, height
    if (match := _WINDOW_SIZE.search(answer)) and columns and rows:
        height, width = int(match.group(1)), int(match.group(2))
        if width and height:
            return width // columns, height // rows
    if ioctl_pixels and all(ioctl_pixels) and columns and rows:
        return ioctl_pixels[0] // columns, ioctl_pixels[1] // rows
    return None


if __name__ == "__main__":  # python -m cellpix.probe
    capabilities = detect()
    print(capabilities, file=sys.stderr)
