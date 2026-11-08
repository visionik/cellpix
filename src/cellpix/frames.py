"""Frames of an animated picture (GIF, WebP, APNG), ready to send.

Pillow composes each frame (disposal and blending handled), so every frame here is a
whole picture.

Two ways to get them:

- :func:`frames`: every frame decoded, for when they're all needed at once (iTerm2's
  GIFs). :data:`MAX_FRAMES` frames at most, :data:`MAX_BYTES` decoded.
- :func:`encoded_frames`: each frame encoded (e.g. to PNG) as soon as it's decoded, and
  only the encoded copy kept, with the decoded size counted. That's what a terminal is
  sent; it allows far longer animations (:data:`MAX_STEPPED_FRAMES`,
  :data:`MAX_ENCODED_BYTES`), and the caller decides from the decoded size whether the
  terminal can keep every frame or should be stepped through them.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from PIL.Image import Image

MAX_FRAMES = 200
MAX_BYTES = 48 * 1024 * 1024
MAX_STEPPED_FRAMES = 3000
"""Frames at most for :func:`encoded_frames` (a 2-minute GIF at 25 frames a second)."""
MAX_ENCODED_BYTES = 96 * 1024 * 1024
"""Encoded bytes at most for :func:`encoded_frames` (kept in memory to step through)."""
DEFAULT_GAP = 100
"""Milliseconds for a frame that doesn't say (browsers use about this)."""
MIN_GAP = 20
"""Faster than this, browsers slow a frame down: so do we (a 0 or 10 ms GIF frame)."""


_locks_lock = threading.Lock()


@contextmanager
def _decoding(image: "Image"):
    """One thread at a time goes through an image's frames. Seeking moves the image
    itself, so two widgets showing the same picture (each encoding in a worker) would
    otherwise read each other's frames, or break Pillow mid-frame."""
    with _locks_lock:
        lock = image.__dict__.get("_cellpix_lock")
        if lock is None:
            lock = image.__dict__["_cellpix_lock"] = threading.Lock()
    with lock:
        yield


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
    with _decoding(image):
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


def encoded_frames(
    image: "Image",
    fit: Callable[["Image"], "Image"],
    encode: Callable[["Image"], bytes],
    *,
    max_frames: int = MAX_STEPPED_FRAMES,
    max_encoded: int = MAX_ENCODED_BYTES,
) -> tuple[list[tuple[bytes, int]], tuple[int, int], int] | None:
    """Every frame encoded (after ``fit``) with its gap in milliseconds, the frames' size,
    and the bytes they'd take decoded (what a terminal keeps to play them itself); or None
    if the picture isn't animated, or is too long or too big even encoded. Each decoded
    frame is dropped once encoded, so memory goes to the encoded frames only."""
    from PIL import ImageSequence

    if not is_animated(image):
        return None
    if getattr(image, "n_frames", 1) > max_frames:
        return None
    result: list[tuple[bytes, int]] = []
    size = (0, 0)
    decoded = encoded = 0
    with _decoding(image):
        position = image.tell()
        try:
            for picture in ImageSequence.Iterator(image):
                gap = picture.info.get("duration") or DEFAULT_GAP
                fitted = fit(picture.convert("RGBA"))
                size = fitted.size
                decoded += fitted.width * fitted.height * 4
                data = encode(fitted)
                encoded += len(data)
                if encoded > max_encoded:
                    return None
                result.append((data, max(MIN_GAP, int(gap))))
        finally:
            image.seek(position)
    return (result, size, decoded) if len(result) > 1 else None
