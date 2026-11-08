"""cellpix: images in terminal cells.

- :mod:`cellpix.kitty` — Kitty graphics protocol escape codes and Unicode placeholders.
- :mod:`cellpix.iterm2`, :mod:`cellpix.sixel` — iTerm2 inline images and Sixel.
- :mod:`cellpix.halfblock` — two pixels per cell for any 24-bit-colour terminal.
- :mod:`cellpix.frames` — the frames of animated pictures (GIF, WebP, APNG).
- :mod:`cellpix.probe` — find out which the terminal supports, and its cell size.
- :mod:`cellpix.textual` — an ``Image`` widget for Textual (``pip install cellpix[textual]``).
- :mod:`cellpix.live` — ``LiveImage``, a Textual widget you keep pushing frames into.
"""

__version__ = "0.2.0"
