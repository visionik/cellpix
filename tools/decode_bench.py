"""How long does the terminal take to take in a frame, by format?

Frames are encoded first, then sent as fast as possible, followed by a device
attributes request (CSI c). A terminal handles its input in order, so the answer comes
only after every frame before it is decoded and stored: first byte to answer is the
terminal's cost. Each format: 30 distinct frames, best of 3 runs.

    uv run python tools/decode_bench.py results.txt [web-page.png]
"""

from __future__ import annotations

import base64
import io
import os
import re
import select
import sys
import termios
import time
import tty
import zlib

from PIL import Image, ImageDraw

from pixcell import kitty

FRAMES, RUNS, SIZE = 30, 3, (640, 400)
DA1 = re.compile(rb"\x1b\[\?[\d;]*c")


def content(page_path: str | None) -> dict[str, Image.Image]:
    if page_path:
        page = Image.open(page_path).convert("RGB").resize(SIZE, Image.Resampling.LANCZOS)
    else:
        page = Image.new("RGB", SIZE, (15, 23, 42))
    base = Image.effect_mandelbrot(SIZE, (-2.2, -1.2, 1.0, 1.2), 200)
    photo = Image.merge("RGB", (base, base.point(lambda v: (v * 3) % 256), base.point(lambda v: 255 - v)))
    return {"web": page, "photo": photo}


def frames(picture: Image.Image) -> list[Image.Image]:
    """Distinct frames: a counter and a moving bar drawn on each copy."""
    result = []
    for number in range(FRAMES):
        frame = picture.copy()
        draw = ImageDraw.Draw(frame)
        draw.rectangle((number * 20, 380, number * 20 + 40, 400), fill=(255, 80, 80))
        draw.text((10, 10), f"frame {number}", fill=(255, 255, 0))
        result.append(frame)
    return result


def png(picture, level):
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG", compress_level=level)
    return buffer.getvalue()


def command(image_id: int, picture: Image.Image, kind: str) -> str:
    """The transmit (a=t, no placement) for one frame in one format."""
    width, height = picture.size
    if kind.startswith("png"):
        data, keys = png(picture, int(kind[-1])), {"f": 100}
    elif kind == "raw":
        data, keys = picture.tobytes(), {"f": 24, "s": width, "v": height}
    else:  # zlib1 / zlib6
        data, keys = zlib.compress(picture.tobytes(), int(kind[-1])), {"f": 24, "s": width, "v": height, "o": "z"}
    encoded = base64.standard_b64encode(data).decode("ascii")
    chunks = [encoded[start : start + 4096] for start in range(0, len(encoded), 4096)]
    first = {"a": "t", "t": "d", "i": image_id, "q": 2, **keys}
    if len(chunks) == 1:
        return kitty.command(first, chunks[0])
    codes = [kitty.command({**first, "m": 1}, chunks[0])]
    codes += [kitty.command({"m": 1, "q": 2}, chunk) for chunk in chunks[1:-1]]
    codes.append(kitty.command({"m": 0, "q": 2}, chunks[-1]))
    return "".join(codes)


def accepted(fd: int, picture: Image.Image, kind: str) -> str:
    """Send one frame as a query (a=q: the terminal loads it, doesn't store it, and
    answers 'OK' or its error; a plain transmit gets no answer in Rex or WezTerm)."""
    image_id = kitty.new_image_id()
    payload = command(image_id, picture, kind).replace("a=t,", "a=q,", 1).replace(",q=2", "", 1).encode("ascii")
    answer = b""
    view = memoryview(payload + b"\x1b[c")
    while view:
        view = view[os.write(fd, view[:65536]) :]
    while not DA1.search(answer):
        if not select.select([fd], [], [], 10)[0]:
            break
        answer += os.read(fd, 4096)
    os.write(fd, kitty.delete(image_id).encode())
    reply = re.search(rb"\x1b_Gi=\d+;([^\x1b]*)\x1b\\", answer)
    return reply.group(1).decode(errors="replace") if reply else "no reply"


def timed(fd: int, payload: bytes) -> float:
    """Seconds from the first byte to the device attributes answer after it."""
    answer = b""
    started = time.perf_counter()
    view = memoryview(payload + b"\x1b[c")
    while view:
        _, writable, _ = select.select([fd], [fd], [], 5)
        if writable:
            view = view[os.write(fd, view[:65536]) :]
        if select.select([fd], [], [], 0)[0]:
            answer += os.read(fd, 4096)
    while not DA1.search(answer):
        if not select.select([fd], [], [], 30)[0]:
            raise TimeoutError("no answer")
        answer += os.read(fd, 4096)
    return time.perf_counter() - started


def main() -> None:
    out_path = sys.argv[1]
    pictures = content(sys.argv[2] if len(sys.argv) > 2 else None)
    kinds = ["png1", "png6", "raw", "zlib1", "zlib6"]
    prepared = {}
    for name, picture in pictures.items():
        for kind in kinds:
            ids = [kitty.new_image_id() for _ in range(FRAMES)]
            payload = "".join(command(image_id, frame, kind) for image_id, frame in zip(ids, frames(picture)))
            prepared[(name, kind)] = (payload.encode("ascii"), ids)
    fd = os.open("/dev/tty", os.O_RDWR)
    saved = termios.tcgetattr(fd)
    from pixcell.probe import detect

    found = detect()  # XTVERSION: TERM_PROGRAM is inherited across terminals
    lines = [f"terminal: {found.terminal} {found.version}  frames: {FRAMES} x {SIZE[0]}x{SIZE[1]}, best of {RUNS}"]
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        os.write(fd, b"\x1b[2J\x1b[Hmeasuring...\r\n")
        for kind in kinds:
            # A small picture: one chunk in every format (queries may not be chunked).
            line = f"accepted {kind:6s}: {accepted(fd, pictures['web'].crop((0, 0, 32, 16)), kind)}"
            lines.append(line)
            os.write(fd, (line + "\r\n").encode())
        for (name, kind), (payload, ids) in prepared.items():
            clear = "".join(kitty.delete(image_id) for image_id in ids).encode()
            times = []
            for _ in range(RUNS):
                times.append(timed(fd, payload))
                timed(fd, clear)  # deleted before the next run reuses the ids
            best = min(times)
            per_frame = best / FRAMES * 1000
            line = f"{name:6s} {kind:6s} {per_frame:7.2f} ms/frame  {len(payload) / FRAMES / 1024:7.1f} KB/frame  {len(payload) / best / 1e6:6.1f} MB/s"
            lines.append(line)
            os.write(fd, (line + "\r\n").encode())
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, saved)
        os.close(fd)
    with open(out_path, "w") as out:
        out.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
