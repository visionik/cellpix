# cellpix

Images in terminal cells, MIT-licensed:

- **Kitty graphics** with Unicode placeholders, for Kitty, Ghostty, WezTerm and Rex. An
  image is sent to the terminal once, then drawn as ordinary coloured text, so it scrolls,
  clips and redraws with everything around it.
- **Half blocks** (`▀`, two pixels per cell) for any terminal with 24-bit colour.
- A **probe** that asks the terminal which it supports, and its cell size in pixels.
- A **Textual** widget (`pip install cellpix[textual]`).

```python
import cellpix.textual
from PIL import Image

cellpix.textual.configure()   # probe the terminal, before the app starts

class Viewer(App):
    def compose(self):
        yield cellpix.textual.Image(Image.open("photo.png"))

Viewer().run()
```

`CELLPIX_PROTOCOL=kitty|halfblock|off` overrides the probe.

## Notes

- The Kitty side is written from the protocol's documentation
  (<https://sw.kovidgoyal.net/kitty/graphics-protocol/>). The placeholder row/column marks
  are generated from the Unicode Character Database by `tools/gen_diacritics.py`.
- Image ids are carried in 24-bit foreground colours, so the app must draw in true colour.
- Pictures are sent scaled to the size they're shown at. A memory budget (8 MB in Rex,
  whose library keeps at most 10 MB) deletes pictures not drawn lately, and they're sent
  again if they come back into view.
- Not yet: tmux passthrough, Windows, Sixel.
