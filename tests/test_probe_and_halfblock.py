from pathlib import Path

from PIL import Image

from cellpix import halfblock, probe


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
    monkeypatch.delenv("CELLPIX_PLACEHOLDERS", raising=False)
    rex = _from("rex", environment_name="rex")
    assert (rex.protocol, rex.terminal, rex.cell_size) == ("kitty", "rex", (8, 16))
    assert rex.image_budget == 8 * 1024 * 1024  # under Rex's 10 MB
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
    monkeypatch.setenv("CELLPIX_PLACEHOLDERS", "1")
    assert _from("wezterm").protocol == "kitty"
    monkeypatch.setenv("CELLPIX_PLACEHOLDERS", "0")
    assert _from("rex").protocol == "kitty-direct"


def test_tmux_uses_placeholders_only(monkeypatch):
    monkeypatch.delenv("CELLPIX_PLACEHOLDERS", raising=False)
    assert _from("rex", tmux=True).protocol == "kitty"
    wezterm = _from("wezterm", tmux=True)
    assert wezterm.protocol == "halfblock" and "no Kitty placeholders" in wezterm.reason
    silent = probe.capabilities_from(b"\x1b[?1;2c", 80, 24, None, tmux=True)
    assert silent.protocol == "halfblock" and "allow-passthrough" in silent.reason


def test_tmux_passthrough_wrapping():
    assert probe._wrap("\x1b[>0q", tmux=True) == "\x1bPtmux;\x1b\x1b[>0q\x1b\\"
    assert probe._wrap("\x1b[>0q", tmux=False) == "\x1b[>0q"


def test_xtversion_names():
    assert probe._split_version("kitty(0.50.0)") == ("kitty", "0.50.0")
    assert probe._split_version("WezTerm 20240203-110809-5046fc22") == ("wezterm", "20240203-110809-5046fc22")
    assert probe._split_version("iTerm2 3.6.5") == ("iterm2", "3.6.5")
    assert probe._split_version("ghostty 1.3.1") == ("ghostty", "1.3.1")


def test_forced_protocol(monkeypatch):
    monkeypatch.setenv("CELLPIX_PROTOCOL", "off")
    assert probe.detect().protocol == "off"


def test_detect_without_a_tty_is_halfblock(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("CELLPIX_PROTOCOL", raising=False)
    assert probe.detect(tty_path="/nonexistent/tty").protocol == "halfblock"


def test_halfblock_cells_are_upper_and_lower_pixels():
    image = Image.new("RGBA", (2, 4))
    image.putpixel((0, 0), (255, 0, 0, 255))
    image.putpixel((0, 1), (0, 0, 255, 255))
    rows = halfblock.cells(image.resize((2, 4)), 2, 2)
    assert len(rows) == 2 and len(rows[0]) == 2
    assert rows[0][0] == ((255, 0, 0), (0, 0, 255))
    assert rows[1][1] == (None, None)  # transparent: the terminal's colours show
