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


def _bouncing(frames: int = 24):
    """An animated GIF: a ball bouncing across a gradient, with a frame counter."""
    import io

    from PIL import Image, ImageDraw

    pictures = []
    for number in range(frames):
        image = Image.new("RGB", (480, 200))
        draw = ImageDraw.Draw(image)
        for x in range(480):
            draw.line([(x, 0), (x, 199)], fill=(40, 60 + x * 120 // 479, 140))
        position = abs((number * 2 * 400 // frames) % 800 - 400)
        draw.ellipse((position + 10, 60, position + 70, 120), fill=(255, 200, 40))
        draw.text((12, 10), f"frame {number + 1}/{frames}", fill=(255, 255, 255))
        pictures.append(image)
    buffer = io.BytesIO()
    pictures[0].save(buffer, format="GIF", save_all=True, append_images=pictures[1:], duration=60, loop=0)
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
        CSS = "Image { width: 60; height: auto; margin: 0 0 1 0; } Static { color: $text-muted; }"
        BINDINGS = [("q", "quit", "Quit")]

        def compose(self) -> ComposeResult:
            with VerticalScroll():
                yield Static(
                    f"{found.protocol}: {found.reason}; cell {found.cell_size}; terminal {found.terminal or '?'}; "
                    f"animation {'plays' if found.animation else 'stays still'}"
                )
                for name, image in images:
                    yield Static(f"{name} ({found.protocol})")
                    yield ct.Image(image)
                    yield Static(f"{name} (halfblock)")
                    yield ct.Image(image, protocol="halfblock")
            yield Footer()

    Demo().run()


if __name__ == "__main__":
    main()
