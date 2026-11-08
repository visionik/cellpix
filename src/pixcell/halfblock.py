"""Half-block images: two pixels per cell, for terminals without graphics.

Each cell is ``▀`` (upper half block): its foreground colour is the upper pixel, its
background the lower one. Works in any terminal with 24-bit colour; coarse, but always
there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image

UPPER_HALF = "▀"

Colour = tuple[int, int, int] | None
"""An RGB colour, or None where the image is transparent (the terminal's own shows)."""


def cells(image: "Image", columns: int, rows: int) -> list[list[tuple[Colour, Colour]]]:
    """The image as ``rows`` lines of ``columns`` cells, each (upper, lower) colour."""
    from PIL import Image as PILImage

    if columns <= 0 or rows <= 0:
        return []
    picture = image.convert("RGBA").resize((columns, rows * 2), PILImage.Resampling.LANCZOS)
    pixels = picture.load()

    def colour(x: int, y: int) -> Colour:
        red, green, blue, alpha = pixels[x, y]
        return None if alpha < 128 else (red, green, blue)

    return [[(colour(x, 2 * row), colour(x, 2 * row + 1)) for x in range(columns)] for row in range(rows)]
