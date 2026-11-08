"""iTerm2 inline images (``OSC 1337 ; File=``), also drawn by WezTerm, VS Code and mintty.

Written from iTerm2's documentation (https://iterm2.com/documentation-images.html). The
picture is drawn at the cursor over ``width x height`` cells.
"""

from __future__ import annotations

import base64


def inline(png: bytes, columns: int, rows: int) -> str:
    """Draw a PNG over ``columns x rows`` cells at the cursor, leaving the cursor put."""
    data = base64.standard_b64encode(png).decode("ascii")
    return (
        f"\x1b]1337;File=inline=1;size={len(png)};width={columns};height={rows};"
        f"preserveAspectRatio=0;doNotMoveCursor=1:{data}\x07"
    )
