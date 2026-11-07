"""``python -m cellpix [image ...]``: show images in a scrolling Textual view.

Each image is drawn twice: with the protocol the probe found, and with half blocks.
Without arguments it draws a test gradient.
"""

from __future__ import annotations

import sys


def _gradient():
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 360))
    draw = ImageDraw.Draw(image)
    for x in range(640):
        draw.line([(x, 0), (x, 359)], fill=(x * 255 // 639, 120, 255 - x * 255 // 639))
    draw.ellipse((220, 80, 420, 280), fill=(250, 250, 250), outline=(0, 0, 0), width=6)
    draw.text((20, 20), "cellpix", fill=(255, 255, 255))
    return image


def _bouncing(frames: int = 16):
    """An animated GIF: a ball bouncing across a gradient, with a frame counter. Small
    enough (320x120, 16 frames: ~2.4 MB decoded) to play natively within Rex's budget."""
    import io

    from PIL import Image, ImageDraw

    pictures = []
    for number in range(frames):
        image = Image.new("RGB", (320, 120))
        draw = ImageDraw.Draw(image)
        for x in range(320):
            draw.line([(x, 0), (x, 119)], fill=(40, 60 + x * 120 // 319, 140))
        position = abs((number * 2 * 260 // frames) % 520 - 260)
        draw.ellipse((position + 10, 40, position + 50, 80), fill=(255, 200, 40))
        draw.text((10, 8), f"frame {number + 1}/{frames}", fill=(255, 255, 255))
        pictures.append(image)
    buffer = io.BytesIO()
    pictures[0].save(buffer, format="GIF", save_all=True, append_images=pictures[1:], duration=80, loop=0)
    buffer.seek(0)
    return Image.open(buffer)


def live_demo() -> None:
    """``python -m cellpix live``: a generated 640x400 scene at 15 fps. Click to drop a
    marker at that pixel; scroll to change the orbit's speed."""
    import math
    import threading
    import time

    from PIL import Image, ImageDraw
    from textual.app import App, ComposeResult
    from textual.widgets import Footer, Static

    import cellpix.textual as ct
    from cellpix.live import LiveImage

    found = ct.configure()
    width, height = 640, 400
    marks: list[tuple[int, int]] = []
    speed = [1.0]

    def scene(t: float) -> Image.Image:
        picture = Image.new("RGB", (width, height), (18, 24, 38))
        draw = ImageDraw.Draw(picture)
        for x in range(0, width, 40):
            draw.line([(x, 0), (x, height)], fill=(30, 40, 60))
        for y in range(0, height, 40):
            draw.line([(0, y), (width, y)], fill=(30, 40, 60))
        angle = t * speed[0]
        cx, cy = width / 2 + 180 * math.cos(angle), height / 2 + 120 * math.sin(angle)
        draw.ellipse((cx - 22, cy - 22, cx + 22, cy + 22), fill=(80, 200, 255))
        bar = int((math.sin(t * 2) + 1) / 2 * (width - 120))
        draw.rectangle((bar, height - 30, bar + 120, height - 14), fill=(255, 170, 40))
        draw.text((14, 12), time.strftime("%H:%M:%S") + f"  t={t:6.2f}s", fill=(230, 230, 230))
        for mx, my in marks[-5:]:
            draw.line([(mx - 8, my), (mx + 8, my)], fill=(255, 60, 60), width=2)
            draw.line([(mx, my - 8), (mx, my + 8)], fill=(255, 60, 60), width=2)
            draw.text((mx + 10, my + 4), f"{mx},{my}", fill=(255, 120, 120))
        return picture

    class Live(App):
        CSS = "LiveImage { width: 80; height: auto; } Static { color: $text-muted; }"
        BINDINGS = [("q", "quit", "Quit")]

        def compose(self) -> ComposeResult:
            yield Static(f"{found.protocol} ({found.terminal} {found.version}); format {found.frame_format}; "
                         f"{'edits' if found.edits else 'whole frames'} — click to mark, scroll for speed")
            yield LiveImage((width, height), id="live")
            yield Static("", id="stats")
            yield Footer()

        def on_mount(self) -> None:
            live = self.query_one(LiveImage)
            started = time.monotonic()

            def produce() -> None:
                while self.is_running:
                    live.push(scene(time.monotonic() - started))
                    time.sleep(1 / 30)  # faster than shown: the newest frame wins

            threading.Thread(target=produce, daemon=True).start()
            self.set_interval(1, self.stats)

        def stats(self) -> None:
            live = self.query_one(LiveImage)
            per_frame = live.bytes_sent / max(1, live.frames_shown) / 1024
            self.query_one("#stats", Static).update(
                f"{live.frames_shown} frames shown, {live.bytes_sent / 1024:.0f} KB sent, {per_frame:.1f} KB/frame"
            )

        def on_live_image_clicked(self, message: LiveImage.Clicked) -> None:
            marks.append((message.x, message.y))

        def on_live_image_scrolled(self, message: LiveImage.Scrolled) -> None:
            speed[0] = max(0.2, min(5.0, speed[0] * (0.8 if message.delta > 0 else 1.25)))

    Live().run()


def main() -> None:
    from PIL import Image as PILImage
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.widgets import Footer, Static

    import cellpix.textual as ct

    found = ct.configure()
    images = [(path, PILImage.open(path)) for path in sys.argv[1:]] or [
        ("animated GIF", _bouncing()),
        ("gradient", _gradient()),
    ]

    class Demo(App):
        CSS = "Image { width: 40; height: auto; margin: 0 0 1 0; } Static { color: $text-muted; }"
        BINDINGS = [("q", "quit", "Quit")]

        def compose(self) -> ComposeResult:
            with VerticalScroll():
                yield Static(
                    f"{found.protocol}: {found.reason}; cell {found.cell_size}; terminal {found.terminal or '?'}; "
                    f"animation {'plays' if found.animation else 'stays still'}"
                )
                for name, image in images:
                    picture = ct.Image(image)
                    moving = f", {picture._animation} animation" if picture.animated else (
                        ", still" if getattr(image, "is_animated", False) else ""
                    )
                    yield Static(f"{name} ({picture.protocol}{moving})")
                    yield picture
                    yield Static(f"{name} (halfblock)")
                    yield ct.Image(image, protocol="halfblock")
            yield Footer()

    Demo().run()


if __name__ == "__main__":
    if sys.argv[1:2] == ["live"]:
        live_demo()
    else:
        main()
