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


def main() -> None:
    from PIL import Image as PILImage
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.widgets import Footer, Static

    import cellpix.textual as ct

    found = ct.configure()
    images = [(path, PILImage.open(path)) for path in sys.argv[1:]] or [("gradient", _gradient())]

    class Demo(App):
        CSS = "Image { width: 60; height: auto; margin: 0 0 1 0; } Static { color: $text-muted; }"
        BINDINGS = [("q", "quit", "Quit")]

        def compose(self) -> ComposeResult:
            with VerticalScroll():
                yield Static(f"{found.protocol}: {found.reason}; cell {found.cell_size}; terminal {found.terminal or '?'}")
                for name, image in images:
                    yield Static(f"{name} ({found.protocol})")
                    yield ct.Image(image)
                    yield Static(f"{name} (halfblock)")
                    yield ct.Image(image, protocol="halfblock")
            yield Footer()

    Demo().run()


if __name__ == "__main__":
    main()
