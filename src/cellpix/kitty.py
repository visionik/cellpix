"""Kitty graphics protocol: the escape codes, and the placeholder text that shows an image.

Written from the protocol's documentation
(https://sw.kovidgoyal.net/kitty/graphics-protocol/). Pure functions: nothing here
writes to a terminal.

An image is shown with *Unicode placeholders*. The image is sent once, given a virtual
placement of ``columns x rows`` cells, and then drawn by printing ordinary text: each
cell is U+10EEEE with two combining marks that number its row and column, in a
foreground colour that encodes the image id. To a TUI that's just coloured text, so the
picture scrolls, clips and redraws like the text around it.
"""

from __future__ import annotations

import base64
import random

from cellpix._diacritics import DIACRITICS

PLACEHOLDER = "\U0010eeee"
"""The character each placeholder cell holds."""

MAX_CELLS = len(DIACRITICS)
"""The most rows or columns an image can span (one combining mark per number)."""

CHUNK_BYTES = 4096
"""Payload per escape code: the protocol's limit for chunked transmission."""


def command(keys: dict[str, object], payload: str = "") -> str:
    """One graphics escape code: ``ESC _ G <keys> ; <payload> ESC \\``."""
    control = ",".join(f"{key}={value}" for key, value in keys.items())
    return f"\x1b_G{control};{payload}\x1b\\" if payload else f"\x1b_G{control}\x1b\\"


def transmit(image_id: int, png: bytes, *, chunk_bytes: int = CHUNK_BYTES) -> str:
    """Send a PNG for later display (``a=t``), quietly (``q=2``), in chunks.

    A single-chunk transmission carries no ``m`` key at all: some terminals (Rex) have
    shown nothing when a one-shot transmission says ``m=0``.
    """
    if chunk_bytes <= 0 or chunk_bytes % 4:
        raise ValueError("chunk_bytes must be a positive multiple of 4")
    data = base64.standard_b64encode(png).decode("ascii")
    chunks = [data[start : start + chunk_bytes] for start in range(0, len(data), chunk_bytes)] or [""]
    first = {"a": "t", "f": 100, "t": "d", "i": image_id, "q": 2}
    if len(chunks) == 1:
        return command(first, chunks[0])
    codes = [command({**first, "m": 1}, chunks[0])]
    codes += [command({"m": 1, "q": 2}, chunk) for chunk in chunks[1:-1]]
    codes.append(command({"m": 0, "q": 2}, chunks[-1]))
    return "".join(codes)


def place(image_id: int, columns: int, rows: int, placement_id: int = 1) -> str:
    """A virtual placement (``U=1``) of ``columns x rows`` cells, for placeholders to show.

    Sending it again with the same ids replaces it (e.g. after a resize).
    """
    _check_size(columns, rows)
    return command({"a": "p", "U": 1, "i": image_id, "p": placement_id, "c": columns, "r": rows, "q": 2})


def place_here(
    image_id: int,
    columns: int,
    rows: int,
    source: tuple[int, int, int, int] | None = None,
    placement_id: int = 1,
) -> str:
    """A placement at the cursor (for terminals without placeholders), over ``columns x
    rows`` cells, showing the ``source`` rectangle (x, y, width, height in pixels) of
    the image. The cursor stays put (``C=1``); the same ids replace an earlier one."""
    _check_size(columns, rows)
    keys: dict[str, object] = {"a": "p", "i": image_id, "p": placement_id, "c": columns, "r": rows}
    if source is not None:
        keys.update(zip("xywh", source))
    keys.update({"C": 1, "q": 2})
    return command(keys)


def delete_placement(image_id: int, placement_id: int = 1) -> str:
    """Remove one placement, keeping the image's data for the next one (``d=i``)."""
    return command({"a": "d", "d": "i", "i": image_id, "p": placement_id, "q": 2})


def delete(image_id: int) -> str:
    """Delete an image's placements and free its data (``d=I``)."""
    return command({"a": "d", "d": "I", "i": image_id, "q": 2})


def query(image_id: int) -> str:
    """Ask whether the terminal supports the protocol: a 1x1 RGB image, not stored.

    A supporting terminal answers ``ESC _ G i=<id> ; OK ESC \\``.
    """
    return command({"i": image_id, "s": 1, "v": 1, "a": "q", "t": "d", "f": 24}, "AAAA")


def id_colour(image_id: int) -> tuple[int, int, int]:
    """The 24-bit foreground colour carrying an image id's low three bytes."""
    return (image_id >> 16) & 0xFF, (image_id >> 8) & 0xFF, image_id & 0xFF


def placeholder_row(image_id: int, row: int, columns: int) -> str:
    """The text for one row of an image: every cell carries its row and column marks
    (and the id's fourth byte, if any), so nothing depends on the inheritance rules."""
    _check_size(columns, row + 1)
    high = image_id >> 24
    high_mark = chr(DIACRITICS[high]) if high else ""
    row_mark = chr(DIACRITICS[row])
    return "".join(PLACEHOLDER + row_mark + chr(DIACRITICS[column]) + high_mark for column in range(columns))


def new_image_id() -> int:
    """A random id that fits in the colour alone (no fourth byte), never zero.

    Random, so two programs sharing a terminal are unlikely to collide.
    """
    return random.randint(0x000100, 0xFFFFFF)


def _check_size(columns: int, rows: int) -> None:
    if not (0 < columns <= MAX_CELLS and 0 < rows <= MAX_CELLS):
        raise ValueError(f"an image spans 1 to {MAX_CELLS} cells each way, not {columns}x{rows}")
