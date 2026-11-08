"""Sixel: a picture as a DEC Sixel escape code.

Sixel paints six-pixel-high bands, one colour at a time, from a palette of up to 256
registers. Written from the DEC VT330/VT340 graphics documentation. Transparent pixels
are left unpainted (``P2=1``), so the cell's own background shows through.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image

COLOURS = 255
"""Palette registers used for the picture (the 256th marks transparency)."""

_TRANSPARENT = 255
_SIXEL_CHARS = bytes(range(63, 63 + 64)) + bytes(192)  # value v -> chr(63 + v)
_RUN = re.compile(rb"(.)\1{3,}")


def encode(image: "Image") -> str:
    """The picture as one Sixel escape code. Its height should be a multiple of 6 so
    the last band doesn't reach into the cell row below."""
    from PIL import Image as PILImage

    rgba = image.convert("RGBA")
    width, height = rgba.size
    alpha = rgba.getchannel("A").tobytes()
    indexed = rgba.convert("RGB").quantize(colors=COLOURS, method=PILImage.Quantize.FASTOCTREE)
    palette = indexed.getpalette() or []
    pixels = bytearray(indexed.tobytes())
    for offset, value in enumerate(alpha):
        if value < 128:
            pixels[offset] = _TRANSPARENT

    used = sorted(set(pixels) - {_TRANSPARENT})
    out = [f'\x1bP0;1;0q"1;1;{width};{height}']
    for index in used:
        red, green, blue = palette[3 * index : 3 * index + 3]
        out.append(f"#{index};2;{red * 100 // 255};{green * 100 // 255};{blue * 100 // 255}")

    for top in range(0, height, 6):
        bands: dict[int, bytearray] = {}
        for dy in range(min(6, height - top)):
            bit = 1 << dy
            start = (top + dy) * width
            for x, index in enumerate(pixels[start : start + width]):
                if index != _TRANSPARENT:
                    row = bands.get(index)
                    if row is None:
                        row = bands[index] = bytearray(width)
                    row[x] |= bit
        for number, (index, row) in enumerate(bands.items()):
            if number:
                out.append("$")  # back to the band's start for the next colour
            out.append(f"#{index}{_compress(row)}")
        out.append("-")  # next band
    out.append("\x1b\\")
    return "".join(out)


def _compress(row: bytearray) -> str:
    """A band row as sixel characters, runs of four or more as ``!<count><char>``."""
    data = bytes(row).translate(_SIXEL_CHARS).rstrip(b"?")  # '?' (0) at the end paints nothing
    return _RUN.sub(lambda match: b"!%d%c" % (len(match.group(0)), match.group(1)[0]), data).decode("ascii")
