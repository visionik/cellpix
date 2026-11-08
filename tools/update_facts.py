"""Copy the shared terminal facts into pixcell, unchanged.

    python tools/update_facts.py [path/to/terminal-facts]   # default: ../terminal-facts

Copies terminals.toml and terminal_facts.py (github.com/visionik/terminal-facts) into
src/pixcell/, checks the file, and records the commit they came from.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "src" / "pixcell"


def main() -> None:
    source = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT.parent / "terminal-facts").resolve()
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain", "terminals.toml", "terminal_facts.py"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if dirty:
        sys.exit(f"{source} has uncommitted changes to the facts: commit them first")
    for name in ("terminals.toml", "terminal_facts.py"):
        shutil.copyfile(source / name, TARGET / name)
    sys.path.insert(0, str(TARGET.parent))
    from pixcell import terminal_facts

    rows = terminal_facts.load(TARGET / "terminals.toml")
    (TARGET / "terminal_facts.source").write_text(f"https://github.com/visionik/terminal-facts {commit}\n")
    print(f"terminal facts from {commit[:7]}: {len(rows)} rows")


if __name__ == "__main__":
    main()
