"""Frames of an animated picture (GIF, WebP, APNG), ready to send.

Pillow composes each frame (disposal and blending handled), so every frame here is a
whole picture. Long or huge animations stay still: :data:`MAX_FRAMES` frames at most,
and no more than :data:`MAX_BYTES` once decoded (the terminal keeps every frame).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from PIL.Image import Image

MAX_FRAMES = 200
MAX_BYTES = 48 * 1024 * 1024
DEFAULT_GAP = 100
"""Milliseconds for a frame that doesn't say (browsers use about this)."""
MIN_GAP = 20
"""Faster than this, browsers slow a frame down: so do we (a 0 or 10 ms GIF frame)."""


def is_animated(image: "Image") -> bool:
    return bool(getattr(image, "is_animated", False)) and getattr(image, "n_frames", 1) > 1


def frames(
    image: "Image",
    fit: Callable[["Image"], "Image"],
    max_bytes: int = MAX_BYTES,
) -> list[tuple["Image", int]] | None:
    """Every frame (after ``fit``, e.g. resizing to the box) and its gap in milliseconds,
    or None if the picture isn't animated or is too long or too big to animate."""
    from PIL import ImageSequence

    if not is_animated(image):
        return None
    count = getattr(image, "n_frames", 1)
    if count > MAX_FRAMES:
        return None
    result: list[tuple[Image, int]] = []
    total = 0
    position = image.tell()
    try:
        for picture in ImageSequence.Iterator(image):
            gap = picture.info.get("duration") or DEFAULT_GAP
            fitted = fit(picture.convert("RGBA"))
            total += fitted.width * fitted.height * 4
            if total > max_bytes:
                return None
            result.append((fitted, max(MIN_GAP, int(gap))))
    finally:
        image.seek(position)
    return result if len(result) > 1 else None
