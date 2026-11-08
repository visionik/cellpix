"""Find out what the terminal can draw, before a TUI takes it over.

Asks the terminal directly, on its tty, in one round trip:

- a Kitty graphics query (``a=q``): answered ``OK`` by terminals with Kitty graphics;
- ``XTVERSION`` (``CSI > 0 q``): the terminal's name and version;
- ``CSI 16 t`` / ``CSI 14 t``: the cell and window size in pixels;
- iTerm2's ``ReportCellSize``: answered by terminals with iTerm2 inline images;
- primary device attributes (``CSI c``), last: every terminal answers it, so it ends the
  wait, and a ``4`` among its attributes means Sixel.

Environment variables are only a fallback: a program started from one terminal in
another inherits the first one's ``TERM_PROGRAM``.

Kitty's Unicode placeholders can't be queried, so whether a terminal draws them comes
from :data:`PLACEHOLDERS` (by XTVERSION name), or ``PIXCELL_PLACEHOLDERS=1``/``0``.

Inside tmux the graphics query, XTVERSION and cell size go to the outer terminal through
tmux's passthrough (it needs ``set -g allow-passthrough on``). Only placeholder images are
offered there: they're text, which tmux redraws itself.

``PIXCELL_PROTOCOL`` forces a protocol (one of :data:`PROTOCOLS`).
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field

from pixcell import kitty

PROTOCOLS = ("kitty", "iterm2", "kitty-direct", "sixel", "halfblock", "off")
"""Best first. ``kitty`` draws with Unicode placeholders; ``kitty-direct``, ``iterm2``
and ``sixel`` paint at the cursor, after each frame (see :mod:`pixcell.textual`)."""

PLACEHOLDERS = {"kitty", "ghostty", "rex", "iterm2"}
"""Terminals (XTVERSION name, lower case) known to draw Kitty Unicode placeholders.

Checked by eye (2026-11-07): Kitty 0.50.0, Ghostty 1.4.0, iTerm2 3.7.7 and Rex draw them
(only used when the terminal also answers the Kitty graphics query: older iTerm2 doesn't). WezTerm 20240203 answers
the Kitty graphics query but doesn't draw placeholders properly, so it gets iTerm2
inline images (or Kitty placements, or Sixel, which all work there)."""

KITTY_ANIMATION = {"kitty": (0,), "ghostty": (1, 4), "rex": (0,)}
"""Terminals (XTVERSION name: lowest version) that play Kitty animations themselves
(``a=f`` frames, ``a=a``). Checked by eye (2026-11-07): Kitty 0.50, Ghostty 1.4.0 and Rex
yes; Ghostty 1.3.1 lacks ``a=f``; iTerm2 3.7.7 doesn't (it plays GIFs in its own protocol).
Elsewhere with placeholders, pixcell steps through the frames itself."""

ITERM2_GIFS = {"iterm2"}
"""Terminals (XTVERSION name) that play animated GIFs sent as iTerm2 inline images.
Checked by eye (2026-11-07): iTerm2 yes; WezTerm 20240203 shows the first frame only.
Elsewhere an iTerm2 picture stays still (or moves as Sixel, where the terminal has it)."""

BUDGETS = {"rex": 8 * 1024 * 1024}
"""Image memory to stay under, where a terminal keeps less than usual (Rex: 10 MB)."""
DEFAULT_BUDGET = 64 * 1024 * 1024

_GRAPHICS_OK = re.compile(rb"\x1b_Gi=(\d+);OK\x1b\\")
_DEVICE_ATTRIBUTES = re.compile(rb"\x1b\[\?([\d;]*)c")
_XTVERSION = re.compile(rb"\x1bP>\|([^\x1b]*)\x1b\\")
_CELL_SIZE = re.compile(rb"\x1b\[6;(\d+);(\d+)t")
_WINDOW_SIZE = re.compile(rb"\x1b\[4;(\d+);(\d+)t")
_ITERM2_CELL = re.compile(rb"\x1b\]1337;ReportCellSize=([\d.]+);([\d.]+)")


@dataclass(frozen=True)
class Capabilities:
    """What the terminal can draw."""

    protocol: str
    """The protocol to use: one of :data:`PROTOCOLS`."""
    cell_size: tuple[int, int] | None = None
    """A cell's (width, height) in pixels, if the terminal said."""
    terminal: str = ""
    """The terminal's name, lower case (XTVERSION, else the environment): ``rex``,
    ``kitty``, ``wezterm``, ``ghostty``, ``iterm2``..."""
    image_budget: int = DEFAULT_BUDGET
    """Bytes of decoded images to keep in the terminal before evicting old ones."""
    reason: str = ""
    """Why the protocol was chosen (for diagnostics)."""
    supported: frozenset[str] = field(default_factory=frozenset)
    """Every protocol the terminal answered for (the chosen one is the best of them)."""
    version: str = ""
    """The terminal's version, if it said."""
    tmux: bool = False
    """Escape codes for the terminal must go through tmux's passthrough."""

    @property
    def frame_format(self) -> str:
        """How to send pictures that change often: ``pixels`` (raw, zlib-compressed) or
        ``png``. Measured (tools/decode_bench.py): raw + zlib is cheapest for Ghostty,
        Rex, Kitty and WezTerm to take in; iTerm2 takes raw pixels slowly, PNG quickly."""
        return "png" if self.terminal == "iterm2" else "pixels"

    @property
    def edits(self) -> bool:
        """Does the terminal redraw a placeholder image edited in place (``a=f`` with
        ``r``)? Where Kitty animations play natively (the same frame commands). Checked
        by eye: Rex and Kitty 0.50 yes; iTerm2 3.7.7 no (the picture stays on its first
        frame). Elsewhere live pictures swap whole frames instead."""
        override = os.environ.get("PIXCELL_EDITS", "")
        if override in ("0", "1"):
            return override == "1"
        minimum = KITTY_ANIMATION.get(self.terminal)
        return self.protocol == "kitty" and minimum is not None and _version(self.version) >= minimum

    @property
    def animation(self) -> str:
        """How pictures move in the chosen protocol: ``native`` (the terminal plays them),
        ``stepped`` (pixcell shows each frame itself: placeholder terminals without
        playback, Sixel, half blocks), or ``""`` (they stay still).
        ``PIXCELL_ANIMATION=native|stepped|0`` overrides it for Kitty terminals; ``0``
        also stills Sixel and half blocks."""
        override = os.environ.get("PIXCELL_ANIMATION", "")
        if self.protocol == "iterm2":
            plays = self.terminal in ITERM2_GIFS and override != "0"
            return "native" if plays else ""  # the terminal plays a GIF
        if self.protocol in ("sixel", "halfblock"):
            return "" if override == "0" else "stepped"  # repainted frame by frame
        if self.protocol not in ("kitty", "kitty-direct"):
            return ""
        if override in ("native", "stepped"):
            return override
        if override == "0":
            return ""
        minimum = KITTY_ANIMATION.get(self.terminal)
        if minimum is not None and _version(self.version) >= minimum:
            return "native"
        # Stepping needs nothing but sending and placing images (double-buffered): any
        # placeholder terminal can. Placements at the cursor would flicker: still there.
        return "stepped" if self.protocol == "kitty" else ""


def detect(timeout: float = 0.5, tty_path: str = "/dev/tty") -> Capabilities:
    """Probe the terminal. Never raises: anything unexpected gives half blocks."""
    environment_name = _name_from_environment()
    forced = os.environ.get("PIXCELL_PROTOCOL", "").lower()
    if forced in ("halfblock", "off"):
        return Capabilities(forced, None, environment_name, _budget(environment_name), "PIXCELL_PROTOCOL")
    tmux = bool(os.environ.get("TMUX"))
    if os.name != "posix":
        return Capabilities("halfblock", None, environment_name, DEFAULT_BUDGET, "not a POSIX terminal")
    try:
        answer, columns, rows, ioctl_pixels = _ask(tty_path, timeout, tmux)
    except OSError as error:
        reason = f"no tty ({error.strerror or error})"
        return Capabilities("halfblock", None, environment_name, DEFAULT_BUDGET, reason, tmux=tmux)
    return capabilities_from(answer, columns, rows, ioctl_pixels, environment_name, tmux, forced)


def capabilities_from(
    answer: bytes,
    columns: int,
    rows: int,
    ioctl_pixels: tuple[int, int] | None,
    environment_name: str = "",
    tmux: bool = False,
    forced: str = "",
) -> Capabilities:
    """Decide from the terminal's answers (separate from the asking, for tests)."""
    name, version = environment_name, ""
    if match := _XTVERSION.search(answer):
        name, version = _split_version(match.group(1).decode("utf-8", "replace"))
    supported = set()
    if _GRAPHICS_OK.search(answer):
        supported.add("kitty-direct")
        if _placeholders(name):
            supported.add("kitty")
    if _ITERM2_CELL.search(answer) or name == "iterm2":
        supported.add("iterm2")
    if (match := _DEVICE_ATTRIBUTES.search(answer)) and "4" in match.group(1).decode().split(";"):
        supported.add("sixel")
    # Rex's TIOCGWINSZ pixel sizes can disagree with its escape-code answers.
    cell = _cell_size(answer, columns, rows, None if name == "rex" else ioctl_pixels)
    budget = _budget(name)
    if tmux:
        usable = supported & {"kitty"}
        if not usable:
            reason = (
                "inside tmux: the terminal outside didn't answer (set -g allow-passthrough on?)"
                if not _GRAPHICS_OK.search(answer)
                else f"inside tmux, and {name or 'the terminal'} has no Kitty placeholders"
            )
            return Capabilities("halfblock", cell, name, budget, reason, frozenset(supported), version, tmux)
        supported_here = usable
    else:
        supported_here = supported
    if forced in PROTOCOLS:
        return Capabilities(forced, cell, name, budget, "PIXCELL_PROTOCOL", frozenset(supported), version, tmux)
    for protocol in PROTOCOLS:
        if protocol in supported_here:
            return Capabilities(protocol, cell, name, budget, _why(protocol, name), frozenset(supported), version, tmux)
    reason = "no graphics answer" if _DEVICE_ATTRIBUTES.search(answer) else "the terminal didn't answer"
    return Capabilities("halfblock", cell, name, budget, reason, frozenset(supported), version, tmux)


def _version(text: str) -> tuple[int, ...]:
    """``1.4.0`` -> (1, 4, 0); anything unreadable counts as new (an unknown build)."""
    numbers = re.findall(r"\d+", text.split("-")[0])
    return tuple(int(number) for number in numbers) if numbers else (999,)


def _why(protocol: str, name: str) -> str:
    return {
        "kitty": f"Kitty graphics with Unicode placeholders ({name})",
        "iterm2": "answered iTerm2's ReportCellSize",
        "kitty-direct": "answered the Kitty graphics query (no Unicode placeholders known)",
        "sixel": "device attributes include Sixel",
    }[protocol]


def _placeholders(name: str) -> bool:
    override = os.environ.get("PIXCELL_PLACEHOLDERS", "")
    if override in ("0", "1"):
        return override == "1"
    return name in PLACEHOLDERS


def _budget(name: str) -> int:
    return BUDGETS.get(name, DEFAULT_BUDGET)


def _split_version(text: str) -> tuple[str, str]:
    """``kitty(0.50.0)``, ``WezTerm 20240203-110809``, ``rex 0.0.17`` -> (name, version)."""
    if match := re.match(r"\s*([^\s(]+)[\s(]*([^)]*)\)?", text):
        name = match.group(1).lower()
        return ("iterm2" if name.startswith("iterm") else name), match.group(2).strip()
    return text.strip().lower(), ""


def _name_from_environment() -> str:
    """The terminal's name from variables only it sets, then TERM_PROGRAM (inheritable)."""
    if os.environ.get("KITTY_WINDOW_ID"):
        return "kitty"
    if os.environ.get("WEZTERM_EXECUTABLE"):
        return "wezterm"
    if os.environ.get("GHOSTTY_RESOURCES_DIR"):
        return "ghostty"
    program = os.environ.get("TERM_PROGRAM", "").lower()
    return "iterm2" if program.startswith("iterm") else program


def _wrap(codes: str, tmux: bool) -> str:
    """Send escape codes to the terminal outside tmux (doubling ESC, as tmux requires)."""
    if not tmux:
        return codes
    return "\x1bPtmux;" + codes.replace("\x1b", "\x1b\x1b") + "\x1b\\"


def _ask(tty_path: str, timeout: float, tmux: bool = False) -> tuple[bytes, int, int, tuple[int, int]]:
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
            outer = kitty.query(kitty.new_image_id()) + "\x1b[>0q\x1b[16t\x1b[14t\x1b]1337;ReportCellSize\x07"
            # Device attributes last, unwrapped: tmux answers it itself, after the rest.
            os.write(fd, (_wrap(outer, tmux) + "\x1b[c").encode())
            answer = b""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
                if ready:
                    answer += os.read(fd, 4096)
                    if _DEVICE_ATTRIBUTES.search(answer):
                        # Answers from the outer terminal may trail tmux's own: a moment more.
                        deadline = min(deadline, time.monotonic() + (0.15 if tmux else 0.02))
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
    if match := _ITERM2_CELL.search(answer):  # height;width in points (* scale ignored)
        height, width = float(match.group(1)), float(match.group(2))
        if width and height:
            return round(width), round(height)
    if (match := _WINDOW_SIZE.search(answer)) and columns and rows:
        height, width = int(match.group(1)), int(match.group(2))
        if width and height:
            return width // columns, height // rows
    if ioctl_pixels and all(ioctl_pixels) and columns and rows:
        return ioctl_pixels[0] // columns, ioctl_pixels[1] // rows
    return None


if __name__ == "__main__":  # python -m pixcell.probe
    print(detect(), file=sys.stderr)
