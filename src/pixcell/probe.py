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

What can't be asked comes from the shared terminal facts (``terminals.toml``, from
github.com/visionik/terminal-facts, vendored by ``tools/update_facts.py``): whether the
terminal draws Kitty Unicode placeholders, plays animations, redraws in-place edits,
plays iTerm2 GIFs, which frame format it takes fastest, how much image memory it keeps.
``PIXCELL_PLACEHOLDERS=1``/``0`` overrides the placeholder fact.

Inside tmux the graphics query, XTVERSION and cell size go to the outer terminal through
tmux's passthrough (it needs ``set -g allow-passthrough on``). Only placeholder images are
offered there: they're text, which tmux redraws itself.

``PIXCELL_PROTOCOL`` forces a protocol (one of :data:`PROTOCOLS`).

``host`` is never detected, only forced: whatever is hosting the app, such as a browser,
draws the pictures. pixcell keeps each widget's size and timing, draws blank cells and
writes nothing. The host reads ``Image.image``, ``Image.animated``, ``Image.alt`` and
``Image.live`` for any widget whose ``protocol`` is ``"host"``.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from pixcell import kitty, terminal_facts

PROTOCOLS = ("kitty", "iterm2", "kitty-direct", "sixel", "halfblock", "off", "host")
"""Best first. ``kitty`` draws with Unicode placeholders; ``kitty-direct``, ``iterm2``
and ``sixel`` paint at the cursor, after each frame (see :mod:`pixcell.textual`)."""

BUDGET_SHARE = 0.8
"""The share of a terminal's image memory (``kitty.storage_bytes``) to use before evicting
pictures: Rex keeps 10 MB, so pixcell stays under 8 MB there."""
DEFAULT_BUDGET = 64 * 1024 * 1024
"""Image memory to stay under where a terminal's limit is unknown (or larger)."""

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

    def fact(self, key: str, default: Any = None) -> Any:
        """A shared terminal fact about this terminal (``kitty.frame_edit``...), or
        ``default`` when it's unknown (or the terminal is)."""
        found = _facts_for(self.terminal, self.version)
        return found.get(key, default) if found is not None else default

    @property
    def frame_format(self) -> str:
        """How to send pictures that change often: ``pixels`` (raw, zlib-compressed) or
        ``png``, by the ``kitty.fastest_format`` fact (tools/decode_bench.py measures it:
        iTerm2 takes PNG fastest, the rest raw + zlib)."""
        return "png" if self.fact("kitty.fastest_format") == "png" else "pixels"

    @property
    def edits(self) -> bool:
        """Does the terminal redraw a placeholder image edited in place (``a=f`` with
        ``r``)? The ``kitty.frame_edit`` fact; unknown counts as no. Elsewhere live
        pictures swap whole frames instead."""
        override = os.environ.get("PIXCELL_EDITS", "")
        if override in ("0", "1"):
            return override == "1"
        return self.protocol == "kitty" and self.fact("kitty.frame_edit") is True

    @property
    def animation(self) -> str:
        """How pictures move in the chosen protocol: ``native`` (the terminal plays them),
        ``stepped`` (pixcell shows each frame itself: placeholder terminals without
        playback, Sixel, half blocks), or ``""`` (they stay still).
        ``PIXCELL_ANIMATION=native|stepped|0`` overrides it for Kitty terminals; ``0``
        also stills Sixel and half blocks."""
        override = os.environ.get("PIXCELL_ANIMATION", "")
        if self.protocol == "host":
            return "" if override == "0" else "native"  # the host plays it
        if self.protocol == "iterm2":
            plays = self.fact("iterm2.gif_animation") is True and override != "0"
            return "native" if plays else ""  # the terminal plays a GIF
        if self.protocol in ("sixel", "halfblock"):
            return "" if override == "0" else "stepped"  # repainted frame by frame
        if self.protocol not in ("kitty", "kitty-direct"):
            return ""
        if override in ("native", "stepped"):
            return override
        if override == "0":
            return ""
        if self.fact("kitty.animation") is True:
            return "native"
        # Stepping needs nothing but sending and placing images (double-buffered): any
        # placeholder terminal can. Placements at the cursor would flicker: still there.
        return "stepped" if self.protocol == "kitty" else ""


def detect(timeout: float = 0.5, tty_path: str = "/dev/tty") -> Capabilities:
    """Probe the terminal. Never raises: anything unexpected gives half blocks."""
    environment_name = _name_from_environment()
    forced = os.environ.get("PIXCELL_PROTOCOL", "").lower()
    # Answered without asking: "host" means the display isn't whichever terminal started us.
    if forced in ("halfblock", "off", "host"):
        return Capabilities(forced, None, environment_name, _budget(environment_name, ""), "PIXCELL_PROTOCOL")
    tmux = bool(os.environ.get("TMUX"))
    try:
        if os.name == "nt":
            answer, columns, rows, ioctl_pixels = _ask_windows(timeout)
        elif os.name == "posix":
            answer, columns, rows, ioctl_pixels = _ask(tty_path, timeout, tmux)
        else:
            return Capabilities("halfblock", None, environment_name, DEFAULT_BUDGET, f"no way to ask on {os.name}")
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
        name, version = terminal_facts.parse_xtversion(match.group(1).decode("utf-8", "replace"))
    supported = set()
    if _GRAPHICS_OK.search(answer):
        supported.add("kitty-direct")
        if _placeholders(name, version):
            supported.add("kitty")
    if _ITERM2_CELL.search(answer) or name == "iterm2":
        supported.add("iterm2")
    if (match := _DEVICE_ATTRIBUTES.search(answer)) and "4" in match.group(1).decode().split(";"):
        supported.add("sixel")
    # Some terminals' TIOCGWINSZ pixel sizes disagree with their escape-code answers (Rex).
    trusted = _facts_for(name, version)
    if trusted is not None and trusted.get("window.ioctl_pixels") is False:
        ioctl_pixels = None
    cell = _cell_size(answer, columns, rows, ioctl_pixels)
    budget = _budget(name, version)
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


def _why(protocol: str, name: str) -> str:
    return {
        "kitty": f"Kitty graphics with Unicode placeholders ({name})",
        "iterm2": "answered iTerm2's ReportCellSize",
        "kitty-direct": "answered the Kitty graphics query (no Unicode placeholders known)",
        "sixel": "device attributes include Sixel",
    }[protocol]


@lru_cache(maxsize=1)
def _facts() -> list[terminal_facts.Row]:
    return terminal_facts.load()


@lru_cache(maxsize=32)
def _facts_for(name: str, version: str) -> terminal_facts.Terminal | None:
    """The facts for a terminal by its XTVERSION-style name and version (None if it's
    not in the file). Names from the environment are the facts' row ids, the same names."""
    if not name:
        return None
    return terminal_facts.identify(_facts(), xtversion=f"{name} {version}".strip(), environ={})


def _placeholders(name: str, version: str) -> bool:
    override = os.environ.get("PIXCELL_PLACEHOLDERS", "")
    if override in ("0", "1"):
        return override == "1"
    found = _facts_for(name, version)
    return found is not None and found.get("kitty.placeholders") is True


def _budget(name: str, version: str) -> int:
    found = _facts_for(name, version)
    storage = found.get("kitty.storage_bytes") if found is not None else None
    return min(DEFAULT_BUDGET, int(storage * BUDGET_SHARE)) if storage else DEFAULT_BUDGET


def _name_from_environment() -> str:
    """The terminal's name from the environment, by the facts' matchers (variables only
    it sets, then TERM_PROGRAM and TERM, which can be inherited); else TERM_PROGRAM."""
    found = terminal_facts.identify(_facts(), environ=os.environ)
    return found.id if found is not None else os.environ.get("TERM_PROGRAM", "").lower()


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


def _ask_windows(timeout: float) -> tuple[bytes, int, int, tuple[int, int]]:
    """`_ask` on Windows: the same queries, on the console, with its input giving the
    answers as characters (ENABLE_VIRTUAL_TERMINAL_INPUT). Reads input records, not bytes:
    waiting says something arrived, which may be no character, and a read would block."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )  # fmt: skip
    invalid = wintypes.HANDLE(-1).value

    class Coord(ctypes.Structure):
        _fields_ = [("x", wintypes.SHORT), ("y", wintypes.SHORT)]

    class Rect(ctypes.Structure):
        _fields_ = [(side, wintypes.SHORT) for side in ("left", "top", "right", "bottom")]

    class BufferInfo(ctypes.Structure):
        _fields_ = [
            ("size", Coord), ("cursor", Coord), ("attributes", wintypes.WORD), ("window", Rect), ("maximum", Coord),
        ]  # fmt: skip

    class KeyEvent(ctypes.Structure):
        _fields_ = [
            ("down", wintypes.BOOL), ("repeat", wintypes.WORD), ("key_code", wintypes.WORD), ("scan_code", wintypes.WORD),
            ("char", wintypes.WCHAR), ("control", wintypes.DWORD),
        ]  # fmt: skip

    class EventUnion(ctypes.Union):
        _fields_ = [("key", KeyEvent), ("padding", ctypes.c_byte * 16)]

    class InputRecord(ctypes.Structure):
        _fields_ = [("type", wintypes.WORD), ("event", EventUnion)]

    def console(name: str) -> int:
        generic_read_write, share_read_write, open_existing = 0xC0000000, 3, 3
        handle = kernel32.CreateFileW(name, generic_read_write, share_read_write, None, open_existing, 0, None)
        if handle == invalid or handle is None:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def mode_of(handle: int) -> int:
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(wintypes.HANDLE(handle), ctypes.byref(mode)):
            raise ctypes.WinError(ctypes.get_last_error())
        return mode.value

    handle_in, handle_out = console("CONIN$"), console("CONOUT$")
    try:
        info = BufferInfo()
        columns = rows = 0
        if kernel32.GetConsoleScreenBufferInfo(wintypes.HANDLE(handle_out), ctypes.byref(info)):
            columns = info.window.right - info.window.left + 1
            rows = info.window.bottom - info.window.top + 1
        saved_in, saved_out = mode_of(handle_in), mode_of(handle_out)
        virtual_terminal_input, processed_output, virtual_terminal_output = 0x200, 0x1, 0x4
        try:
            kernel32.SetConsoleMode(wintypes.HANDLE(handle_in), virtual_terminal_input)  # no echo, no lines
            kernel32.SetConsoleMode(
                wintypes.HANDLE(handle_out), saved_out | processed_output | virtual_terminal_output
            )
            queries = (kitty.query(kitty.new_image_id()) + "\x1b[>0q\x1b[16t\x1b[14t\x1b]1337;ReportCellSize\x07\x1b[c").encode()
            written = wintypes.DWORD()
            if not kernel32.WriteFile(wintypes.HANDLE(handle_out), queries, len(queries), ctypes.byref(written), None):
                raise ctypes.WinError(ctypes.get_last_error())
            answer = b""
            deadline = time.monotonic() + timeout
            records = (InputRecord * 64)()
            read = wintypes.DWORD()
            key_event = 1
            while (left := deadline - time.monotonic()) > 0:
                if kernel32.WaitForSingleObject(wintypes.HANDLE(handle_in), max(1, int(left * 1000))) != 0:
                    break  # nothing more came in time
                if not kernel32.ReadConsoleInputW(wintypes.HANDLE(handle_in), records, len(records), ctypes.byref(read)):
                    raise ctypes.WinError(ctypes.get_last_error())
                for record in records[: read.value]:
                    if record.type == key_event and record.event.key.down and record.event.key.char != "\0":
                        answer += record.event.key.char.encode("utf-8", "replace")
                if _DEVICE_ATTRIBUTES.search(answer):
                    deadline = min(deadline, time.monotonic() + 0.02)
        finally:
            kernel32.SetConsoleMode(wintypes.HANDLE(handle_in), saved_in)
            kernel32.SetConsoleMode(wintypes.HANDLE(handle_out), saved_out)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle_in))
        kernel32.CloseHandle(wintypes.HANDLE(handle_out))
    return answer, columns, rows, (0, 0)


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
