import sys
from pathlib import Path

import pytest
from PIL import Image

from pixcell import halfblock, probe, terminal_facts


def test_cell_size_prefers_csi_16t_then_window_then_ioctl():
    assert probe._cell_size(b"\x1b[6;16;8t\x1b[4;800;1200t", 100, 50, (2000, 1000)) == (8, 16)
    assert probe._cell_size(b"\x1b[4;800;1200t", 100, 50, (2000, 1000)) == (12, 16)
    assert probe._cell_size(b"", 100, 50, (2000, 1000)) == (20, 20)
    assert probe._cell_size(b"", 100, 50, None) is None  # Rex: its ioctl pixels aren't used
    assert probe._cell_size(b"\x1b[6;0;0t", 100, 50, (0, 0)) is None


ANSWERS = Path(__file__).parent / "answers"


def _from(name, **kwargs):
    return probe.capabilities_from((ANSWERS / f"{name}.bin").read_bytes(), 80, 24, (0, 0), **kwargs)


def test_real_answers(monkeypatch):
    """Recorded from the real terminals (raw answers to the probe's queries)."""
    monkeypatch.delenv("PIXCELL_PLACEHOLDERS", raising=False)
    rex = _from("rex", environment_name="rex")
    assert (rex.protocol, rex.terminal, rex.cell_size) == ("kitty", "rex", (8, 16))
    assert rex.image_budget == 8_000_000  # 80% of Rex's 10 MB (terminals.toml: kitty.storage_bytes)
    kitty_terminal = _from("kitty", environment_name="rex")  # TERM_PROGRAM inherited: XTVERSION wins
    assert (kitty_terminal.protocol, kitty_terminal.terminal, kitty_terminal.version) == ("kitty", "kitty", "0.50.0")
    ghostty = _from("ghostty")
    assert (ghostty.protocol, ghostty.terminal, ghostty.version, ghostty.cell_size) == ("kitty", "ghostty", "1.4.0", (8, 17))
    iterm = _from("iterm2")
    assert (iterm.protocol, iterm.terminal, iterm.version, iterm.cell_size) == ("kitty", "iterm2", "3.7.7", (7, 17))
    assert iterm.supported == {"kitty", "kitty-direct", "iterm2", "sixel"}
    older_iterm = probe.capabilities_from(b"\x1b]1337;ReportCellSize=17.0;7.0;1.0\x1b\\\x1b[?64;4c", 80, 24, None, "iterm2")
    assert older_iterm.protocol == "iterm2"  # no Kitty graphics answer: its own protocol
    wezterm = _from("wezterm")
    assert wezterm.terminal == "wezterm" and wezterm.supported == {"kitty-direct", "iterm2", "sixel"}
    assert wezterm.protocol == "iterm2"  # no Unicode placeholders known: the best overlay


def test_placeholder_override(monkeypatch):
    monkeypatch.setenv("PIXCELL_PLACEHOLDERS", "1")
    assert _from("wezterm").protocol == "kitty"
    monkeypatch.setenv("PIXCELL_PLACEHOLDERS", "0")
    assert _from("rex").protocol == "kitty-direct"


def test_tmux_uses_placeholders_only(monkeypatch):
    monkeypatch.delenv("PIXCELL_PLACEHOLDERS", raising=False)
    assert _from("rex", tmux=True).protocol == "kitty"
    wezterm = _from("wezterm", tmux=True)
    assert wezterm.protocol == "halfblock" and "no Kitty placeholders" in wezterm.reason
    silent = probe.capabilities_from(b"\x1b[?1;2c", 80, 24, None, tmux=True)
    assert silent.protocol == "halfblock" and "allow-passthrough" in silent.reason


def test_tmux_passthrough_wrapping():
    assert probe._wrap("\x1b[>0q", tmux=True) == "\x1bPtmux;\x1b\x1b[>0q\x1b\\"
    assert probe._wrap("\x1b[>0q", tmux=False) == "\x1b[>0q"


def test_xtversion_names():
    assert terminal_facts.parse_xtversion("kitty(0.50.0)") == ("kitty", "0.50.0")
    assert terminal_facts.parse_xtversion("WezTerm 20240203-110809-5046fc22") == ("wezterm", "20240203-110809-5046fc22")
    assert terminal_facts.parse_xtversion("iTerm2 3.6.5") == ("iterm2", "3.6.5")
    assert terminal_facts.parse_xtversion("ghostty 1.3.1") == ("ghostty", "1.3.1")


def test_forced_protocol(monkeypatch):
    monkeypatch.setenv("PIXCELL_PROTOCOL", "off")
    assert probe.detect().protocol == "off"


def test_detect_without_a_tty_is_halfblock(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("PIXCELL_PROTOCOL", raising=False)
    assert probe.detect(tty_path="/nonexistent/tty").protocol == "halfblock"


def test_halfblock_cells_are_upper_and_lower_pixels():
    image = Image.new("RGBA", (2, 4))
    image.putpixel((0, 0), (255, 0, 0, 255))
    image.putpixel((0, 1), (0, 0, 255, 255))
    rows = halfblock.cells(image.resize((2, 4)), 2, 2)
    assert len(rows) == 2 and len(rows[0]) == 2
    assert rows[0][0] == ((255, 0, 0), (0, 0, 255))
    assert rows[1][1] == (None, None)  # transparent: the terminal's colours show


def test_facts_come_from_the_shared_file():
    """What can't be asked comes from terminals.toml (github.com/visionik/terminal-facts),
    vendored unchanged; versions and OS rows stack as SPEC.md says."""
    from pixcell.probe import Capabilities

    rows = terminal_facts.load()  # the vendored copy checks out against the spec
    assert {row.id for row in rows} >= {"kitty", "ghostty", "ghostty-1.3", "rex", "wezterm", "iterm2"}
    assert Capabilities("kitty", terminal="ghostty", version="1.3.1").fact("kitty.animation") is False
    # Ghostty dev builds after 1.3.1 (e.g. 1.3.2-main) have a=f: they play animations now.
    assert Capabilities("kitty", terminal="ghostty", version="1.3.2-main+22f100c").animation == "native"
    assert Capabilities("kitty", terminal="foot").fact("kitty.animation", "unknown") == "unknown"
    assert Capabilities("kitty", terminal="kitty").image_budget == probe.DEFAULT_BUDGET  # 320 MiB: capped
    assert probe._budget("kitty", "0.50.0") == probe.DEFAULT_BUDGET and probe._budget("foot", "") == probe.DEFAULT_BUDGET


def test_name_from_environment_uses_the_shared_matchers(monkeypatch):
    for variable in ("KITTY_WINDOW_ID", "WEZTERM_EXECUTABLE", "GHOSTTY_RESOURCES_DIR", "REX_SESSION", "ITERM_SESSION_ID", "TERM_PROGRAM"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("TERM", "xterm-kitty")
    assert probe._name_from_environment() == "kitty"
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert probe._name_from_environment() == "iterm2"
    monkeypatch.setenv("TERM_PROGRAM", "SomethingNew")
    monkeypatch.setenv("TERM", "xterm-256color")
    assert probe._name_from_environment() == "somethingnew"  # unknown: TERM_PROGRAM as before


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows console")
def test_windows_probe_asks_the_console_and_reads_the_answers(tmp_path):
    """detect() in a real ConPTY, with this test playing the terminal outside it."""
    import json
    import threading
    import time

    import winpty

    result = tmp_path / "result.json"
    script = tmp_path / "child.py"
    script.write_text(
        "import json\n"
        "from pixcell import probe\n"
        "found = probe.detect(timeout=3.0)\n"
        f"open({str(result)!r}, 'w').write(json.dumps([found.protocol, found.cell_size, found.reason]))\n"
    )
    terminal = winpty.PTY(100, 30)
    assert terminal.spawn(sys.executable, f'"{script}"', None, None)
    seen = ""

    def play_the_terminal() -> None:
        nonlocal seen
        answered = False
        while True:
            try:
                text = terminal.read(blocking=True)
            except Exception:
                return
            if not text and not terminal.isalive():
                return
            seen += text
            if not answered and "\x1b[>0q" in seen:
                answered = True
                terminal.write("\x1b[6;20;10t\x1b[?62;4;22c")  # a cell size, and Sixel

    threading.Thread(target=play_the_terminal, daemon=True).start()
    deadline = time.monotonic() + 30
    while terminal.isalive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert json.loads(result.read_text()) == ["sixel", [10, 20], "device attributes include Sixel"]
