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
    main()
