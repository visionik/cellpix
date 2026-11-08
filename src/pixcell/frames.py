"""Frames of an animated picture (GIF, WebP, APNG), ready to send.

Pillow composes each frame (disposal and blending handled), so every frame here is a
whole picture.

Two ways to get them:

- :func:`frames`: every frame decoded, for when they're all needed at once (Kitty
  placements). :data:`MAX_BYTES` decoded at most: memory is the limit, not the count.
- :func:`gif`: the animation as one GIF, frames fed to the encoder as they're decoded
  (iTerm2 plays it).
- :func:`encoded_frames`: each frame encoded (e.g. to PNG) as soon as it's decoded, and
  only the encoded copy kept, with the decoded size counted. That's what a terminal is
  sent; it allows far longer animations (:data:`MAX_STEPPED_FRAMES`,
  :data:`MAX_ENCODED_BYTES`), and the caller decides from the decoded size whether the
  terminal can keep every frame or should be stepped through them.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable, Iterator

if TYPE_CHECKING:
    from PIL.Image import Image

MAX_BYTES = 128 * 1024 * 1024
"""Decoded bytes at most for :func:`frames` (kept in memory here; what a terminal may
keep is its own image budget)."""
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
        lock = image.__dict__.get("_pixcell_lock")
        if lock is None:
            lock = image.__dict__["_pixcell_lock"] = threading.Lock()
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


def gif(
    image: "Image",
    fit: Callable[["Image"], "Image"],
    *,
    max_frames: int = MAX_STEPPED_FRAMES,
    max_bytes: int = MAX_ENCODED_BYTES,
) -> bytes | None:
    """The animation as one looping GIF (for terminals that play GIFs themselves), each
    frame after ``fit``; or None if it isn't animated, or is too long or too big. Frames
    are fed to the encoder as they're decoded, so none are kept decoded meanwhile."""
    import io

    from PIL import ImageSequence

    if not is_animated(image) or getattr(image, "n_frames", 1) > max_frames:
        return None
    buffer = io.BytesIO()
    with _decoding(image):
        position = image.tell()
        try:

            def each():
                for picture in ImageSequence.Iterator(image):
                    fitted = fit(picture.convert("RGBA"))
                    fitted.info["duration"] = max(MIN_GAP, int(picture.info.get("duration") or DEFAULT_GAP))
                    yield fitted

            pictures = each()
            first = next(pictures)
            first.save(buffer, format="GIF", save_all=True, append_images=pictures, loop=0, disposal=2)
        finally:
            image.seek(position)
    data = buffer.getvalue()
    return data if len(data) <= max_bytes else None


def each_encoded_frame(
    image: "Image",
    fit: Callable[["Image"], "Image"],
    encode: Callable[["Image"], bytes],
    *,
    max_frames: int = MAX_STEPPED_FRAMES,
) -> Iterator[tuple[bytes, int, tuple[int, int]]]:
    """Each frame as it's decoded: encoded (after ``fit``), with its gap in milliseconds
    and its fitted size, so a caller can start showing frames before the last is made.
    Nothing if the picture isn't animated or has more than ``max_frames`` frames. The
    image is held (for this thread) until the iteration ends: finish or close it."""
    from PIL import ImageSequence

    if not is_animated(image) or getattr(image, "n_frames", 1) > max_frames:
        return
    with _decoding(image):
        position = image.tell()
        try:
            for picture in ImageSequence.Iterator(image):
                gap = picture.info.get("duration") or DEFAULT_GAP
                fitted = fit(picture.convert("RGBA"))
                yield encode(fitted), max(MIN_GAP, int(gap)), fitted.size
        finally:
            image.seek(position)


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
    result: list[tuple[bytes, int]] = []
    size = (0, 0)
    decoded = encoded = 0
    frames_made = each_encoded_frame(image, fit, encode, max_frames=max_frames)
    try:
        for data, gap, size in frames_made:
            decoded += size[0] * size[1] * 4
            encoded += len(data)
            if encoded > max_encoded:
                return None
            result.append((data, gap))
    finally:
        frames_made.close()  # lets the image go at once
    return (result, size, decoded) if len(result) > 1 else None
