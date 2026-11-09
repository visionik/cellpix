# pixcell

Images in terminal cells, MIT-licensed. (Formerly *cellpix*; renamed in 0.3.0, with its
`CELLPIX_*` environment variables now `PIXCELL_*`.)

- **Kitty graphics** with Unicode placeholders, for Kitty, Ghostty, iTerm2 (3.7+) and Rex. An image is
  sent to the terminal once, then drawn as ordinary coloured text, so it scrolls, clips
  and redraws with everything around it.
- **iTerm2 inline images, Sixel, and Kitty placements** for terminals without
  placeholders (older iTerm2, WezTerm, foot, xterm, Konsole...): painted over the image's cells
  after each frame, cropped to what's visible.
- **Half blocks** (`▀`, two pixels per cell) for any terminal with 24-bit colour.
- **Animated pictures** (GIF, WebP, APNG): played by the terminal where it can (Kitty,
  Ghostty 1.4+, Rex, iTerm2), otherwise stepped through by pixcell, a frame at a time
  (other placeholder terminals, Sixel, half blocks). In WezTerm, which shows iTerm2 GIFs
  still, moving pictures go to Sixel. `pixcell.textual.animations_play()` says whether
  they move here, and a widget's `still_reason` why one doesn't; `PIXCELL_ANIMATION=0`
  keeps them still.
- **Live pictures** (`pixcell.live.LiveImage`): push frames from any thread (a browser
  view, a camera, a game); only what changed is sent where the terminal allows, and
  clicks come back in the frame's own pixels.
- A **probe** that asks the terminal which it supports, and its cell size in pixels.
- A **Textual** widget (`pip install pixcell[textual]`).

```python
import pixcell.textual
from PIL import Image

pixcell.textual.configure()   # probe the terminal, before the app starts

class Viewer(App):
    def compose(self):
        yield pixcell.textual.Image(Image.open("photo.png"))

Viewer().run()
```

A live picture:

```python
from pixcell.live import LiveImage

live = LiveImage((640, 400), fps=30)   # in compose(), like any widget
live.push(frame)                       # a Pillow image, from any thread: the newest wins
```

`PIXCELL_PROTOCOL=kitty|iterm2|kitty-direct|sixel|halfblock|off` overrides the probe.
`python -m pixcell [image ...]` shows pictures; `python -m pixcell live [fps] [scroll]`
runs a live picture with its frame rate and cost.

## Notes

- What each terminal really does comes from [terminal-facts](https://github.com/visionik/terminal-facts),
  vendored as `src/pixcell/terminals.toml` by `tools/update_facts.py`. It's shared with sdl-kitty, and
  each fact has its evidence.
- The Kitty side is written from the protocol's documentation
  (<https://sw.kovidgoyal.net/kitty/graphics-protocol/>). The placeholder row/column marks
  are generated from the Unicode Character Database by `tools/gen_diacritics.py`.
- Image ids are carried in 24-bit foreground colours, so the app must draw in true colour.
- Pictures are sent scaled to the size they're shown at. A memory budget (8 MB in Rex,
  whose library keeps at most 10 MB) deletes pictures not drawn lately, and they're sent
  again if they come back into view.
- Inside tmux (with `allow-passthrough on`), Kitty placeholders only; elsewhere half blocks.
- On Windows the probe asks through the console API; terminals that answer with Sixel (Windows Terminal 1.22+) get Sixel.
