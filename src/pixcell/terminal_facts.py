"""Read terminals.toml: recognise the terminal and look up what it really does.

The reference reader for SPEC.md: standard library only, Python 3.11+, one file to
vendor. ::

    import terminal_facts

    rows = terminal_facts.load()                      # the terminals.toml next to this file
    found = terminal_facts.identify(rows, xtversion="ghostty 1.3.1")
    found.get("kitty.frame_edit")                      # False: 1.3.1 lacks a=f

``python terminal_facts.py check [file]`` checks a file against the spec.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SCHEMA = 1

DEFAULT_PATH = Path(__file__).with_name("terminals.toml")

FACTS: dict[str, Any] = {
    "kitty.placeholders": bool,
    "kitty.frame_edit": bool,
    "kitty.frame_edit_reply": bool,
    "kitty.animation": bool,
    "kitty.shm": bool,
    "kitty.max_escape_bytes": int,
    "kitty.storage_bytes": int,
    "kitty.fastest_format": ("zlib", "png", "raw"),
    "iterm2.gif_animation": bool,
    "window.ioctl_pixels": bool,
    "keyboard.kitty_protocol_config": str,
}
"""Every fact key and its type (or its allowed values). See SPEC.md for meanings."""

IDENTITY = ("xtversion", "env", "term_program", "term", "term_substring")
"""Ways to recognise a terminal, best evidence first."""

CONDITIONS = ("version_min", "version_max", "os")
"""What an extending row adds to its parent's identity."""

OS_NAMES = ("macos", "linux", "windows", "bsd")


class FactsError(ValueError):
    """The file doesn't follow the spec."""


@dataclass(frozen=True)
class Row:
    id: str
    name: str
    extends: str | None
    match: Mapping[str, Any]
    facts: Mapping[str, Any]
    evidence: Mapping[str, str]


@dataclass(frozen=True)
class Terminal:
    """A recognised terminal: its facts, with every matching row applied."""

    id: str
    """The base row's id (``ghostty``, also for Ghostty 1.3.1)."""
    name: str
    version: str
    """As the terminal reported it ("" if unknown)."""
    recognised_by: str
    """``xtversion``, ``env``, ``term_program`` or ``term``."""
    rows: tuple[str, ...]
    """The ids applied, in order (``("ghostty", "ghostty-1.3")``)."""
    facts: Mapping[str, Any]
    evidence: Mapping[str, str]

    def get(self, key: str, default: Any = None) -> Any:
        """A fact's value, or ``default`` when it's unknown for this terminal."""
        if key not in FACTS:
            raise KeyError(f"not a fact in schema {SCHEMA}: {key}")
        return self.facts.get(key, default)


# Reading -----------------------------------------------------------------------------


def load(path: str | os.PathLike | None = None) -> list[Row]:
    """The rows of a facts file, checked against the spec (FactsError if not)."""
    with open(path or DEFAULT_PATH, "rb") as file:
        try:
            data = tomllib.load(file)
        except tomllib.TOMLDecodeError as error:
            raise FactsError(f"not TOML: {error}") from None
    if data.get("schema") != SCHEMA:
        raise FactsError(f"schema must be {SCHEMA}, not {data.get('schema')!r}")
    unknown = set(data) - {"schema", "terminal"}
    if unknown:
        raise FactsError(f"unknown top-level keys: {sorted(unknown)}")
    rows: list[Row] = []
    for table in data.get("terminal", []):
        rows.append(_row(table, {row.id for row in rows}))
    return rows


def _row(table: dict, earlier: set[str]) -> Row:
    table = dict(table)
    row_id = table.pop("id", None)
    if not isinstance(row_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.\-]*", row_id):
        raise FactsError(f"bad or missing id: {row_id!r}")
    where = f"terminal {row_id!r}"
    if row_id in earlier:
        raise FactsError(f"{where}: id used twice")
    extends = table.pop("extends", None)
    name = table.pop("name", None)
    match = table.pop("match", {})
    evidence = table.pop("evidence", {})
    table.pop("notes", None)
    facts = _flatten(table, where)

    if extends is None:
        if not isinstance(name, str) or not name:
            raise FactsError(f"{where}: a base row needs a name")
        if set(match) - set(IDENTITY):
            raise FactsError(f"{where}: a base row matches by {IDENTITY}, not {sorted(set(match) - set(IDENTITY))}")
        if not any(match.get(key) for key in IDENTITY):
            raise FactsError(f"{where}: a base row needs at least one identity matcher")
    else:
        if extends not in earlier:
            raise FactsError(f"{where}: extends {extends!r}, which isn't an earlier row")
        if set(match) - set(CONDITIONS):
            raise FactsError(f"{where}: an extending row adds {CONDITIONS} only, not {sorted(set(match) - set(CONDITIONS))}")
        if not match:
            raise FactsError(f"{where}: an extending row needs a condition")
    for key in IDENTITY:
        if key in match and not (isinstance(match[key], list) and all(isinstance(v, str) and v for v in match[key])):
            raise FactsError(f"{where}: match.{key} must be a list of strings")
    for key in ("version_min", "version_max"):
        if key in match and (not isinstance(match[key], str) or version_key(match[key]) is None):
            raise FactsError(f"{where}: match.{key} must be a version string")
    if "os" in match and not (isinstance(match["os"], list) and set(match["os"]) <= set(OS_NAMES) and match["os"]):
        raise FactsError(f"{where}: match.os must list some of {OS_NAMES}")
    if not isinstance(evidence, dict) or not all(isinstance(v, str) for v in evidence.values()):
        raise FactsError(f"{where}: evidence must map fact keys to strings")
    if set(evidence) - set(FACTS):
        raise FactsError(f"{where}: evidence for unknown facts: {sorted(set(evidence) - set(FACTS))}")
    return Row(row_id, name or "", extends, match, facts, evidence)


def _flatten(table: dict, where: str, prefix: str = "") -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for key, value in table.items():
        dotted = prefix + key
        if isinstance(value, dict):
            facts.update(_flatten(value, where, dotted + "."))
            continue
        kind = FACTS.get(dotted)
        if kind is None:
            raise FactsError(f"{where}: unknown fact {dotted!r}")
        if isinstance(kind, tuple):
            ok = value in kind
        elif kind is int:
            ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
        else:
            ok = isinstance(value, kind)
        if not ok:
            raise FactsError(f"{where}: {dotted} = {value!r} isn't {kind if isinstance(kind, tuple) else kind.__name__}")
        facts[dotted] = value
    return facts


# Versions ----------------------------------------------------------------------------


def version_key(text: str) -> tuple | None:
    """A sortable key for a version: the dotted numbers (after any non-digit prefix),
    and a ``-suffix`` sorting below the same numbers (``1.3.2-main`` < ``1.3.2``). None
    if there are no numbers."""
    match = re.search(r"\d+(?:\.\d+)*", text)
    if match is None:
        return None
    numbers = tuple(int(part) for part in match.group(0).split("."))
    numbers += (0,) * (4 - len(numbers))
    released = 0 if text[match.end():].startswith("-") else 1
    return numbers, released


def parse_xtversion(text: str) -> tuple[str, str]:
    """A terminal's XTVERSION answer (the text inside ``DCS > | ... ST``) as (name,
    version): ``kitty(0.50.0)``, ``WezTerm 20240203-110809``, ``iTerm2 3.7.7``. The name
    is the first word, lower case."""
    match = re.match(r"\s*([^\s(]+)[\s(]*([^)]*)\)?", text)
    if match is None:
        return text.strip().lower(), ""
    return match.group(1).lower(), match.group(2).strip()


def current_os() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform in ("win32", "cygwin"):
        return "windows"
    return "bsd" if "bsd" in sys.platform else sys.platform


# Recognising --------------------------------------------------------------------------


def identify(
    rows: list[Row],
    *,
    xtversion: str | None = None,
    environ: Mapping[str, str] | None = None,
    os_name: str | None = None,
) -> Terminal | None:
    """The terminal, from its XTVERSION answer (best: ask the terminal) or else the
    environment; None if no row matches. Every row extending the match whose conditions
    hold is applied after it, in file order."""
    environ = os.environ if environ is None else environ
    os_name = os_name or current_os()
    base_rows = [row for row in rows if row.extends is None]
    base, version, how = None, "", ""

    if xtversion:
        name, reported = parse_xtversion(xtversion)
        base = next((row for row in base_rows if name in _lower(row.match.get("xtversion"))), None)
        version, how = reported, "xtversion"
    if base is None:
        base = next((row for row in base_rows if any(environ.get(var) for var in row.match.get("env", []))), None)
        how = "env"
    program = environ.get("TERM_PROGRAM", "").lower()
    if base is None and program:
        base = next((row for row in base_rows if program in _lower(row.match.get("term_program"))), None)
        how = "term_program"
    if base is None:
        term = environ.get("TERM", "")
        base = next(
            (
                row
                for row in base_rows
                if term in row.match.get("term", []) or any(part in term for part in row.match.get("term_substring", []))
            ),
            None,
        )
        how = "term"
    if base is None:
        return None
    if how != "xtversion" and program and program in _lower(base.match.get("term_program")):
        version = environ.get("TERM_PROGRAM_VERSION", "")

    applied = [base]
    facts, evidence = dict(base.facts), dict(base.evidence)
    for row in rows:
        if row.extends in {each.id for each in applied} and _conditions_hold(row.match, version, os_name):
            applied.append(row)
            facts.update(row.facts)
            evidence.update(row.evidence)
    return Terminal(base.id, base.name, version, how, tuple(row.id for row in applied), facts, evidence)


def _lower(values) -> list[str]:
    return [value.lower() for value in values or []]


def _conditions_hold(match: Mapping[str, Any], version: str, os_name: str) -> bool:
    if "os" in match and os_name not in match["os"]:
        return False
    if "version_min" in match or "version_max" in match:
        key = version_key(version) if version else None
        if key is None:
            return False  # version bounds never match an unknown version
        if "version_min" in match and key < version_key(match["version_min"]):
            return False
        if "version_max" in match and key > version_key(match["version_max"]):
            return False
    return True


def resolved_facts(rows: list[Row], row_id: str) -> dict[str, Any]:
    """A row's facts with its ancestors' underneath, conditions ignored (for tables)."""
    by_id = {row.id: row for row in rows}
    chain = []
    row = by_id[row_id]
    while row is not None:
        chain.append(row)
        row = by_id.get(row.extends) if row.extends else None
    facts: dict[str, Any] = {}
    for row in reversed(chain):
        facts.update(row.facts)
    return facts


if __name__ == "__main__":
    if sys.argv[1:2] == ["check"]:
        try:
            rows = load(sys.argv[2] if len(sys.argv) > 2 else None)
        except (FactsError, OSError) as error:
            sys.exit(f"terminals.toml: {error}")
        print(f"ok: {len(rows)} rows, {sum(len(row.facts) for row in rows)} facts")
    else:
        sys.exit("usage: python terminal_facts.py check [terminals.toml]")
