from PIL import Image

from cellpix import halfblock, probe


def test_cell_size_prefers_csi_16t_then_window_then_ioctl():
    assert probe._cell_size(b"\x1b[6;16;8t\x1b[4;800;1200t", 100, 50, (2000, 1000)) == (8, 16)
    assert probe._cell_size(b"\x1b[4;800;1200t", 100, 50, (2000, 1000)) == (12, 16)
    assert probe._cell_size(b"", 100, 50, (2000, 1000)) == (20, 20)
    assert probe._cell_size(b"", 100, 50, None) is None  # Rex: its ioctl pixels aren't used
    assert probe._cell_size(b"\x1b[6;0;0t", 100, 50, (0, 0)) is None


def test_detect_reads_the_answers(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("CELLPIX_PROTOCOL", raising=False)
    monkeypatch.setenv("TERM_PROGRAM", "Rex")
    graphics = b"\x1b_Gi=1234;OK\x1b\\\x1b[6;16;8t\x1b[?62;22c"
    monkeypatch.setattr(probe, "_ask", lambda path, timeout: (graphics, 100, 50, (1600, 1600)))
    found = probe.detect()
    assert (found.protocol, found.cell_size, found.terminal) == ("kitty", (8, 16), "rex")
    assert found.image_budget == 8 * 1024 * 1024  # under Rex's 10 MB

    monkeypatch.setattr(probe, "_ask", lambda path, timeout: (b"\x1b[?62;22c", 100, 50, (0, 0)))
    assert probe.detect().protocol == "halfblock"


def test_tmux_and_overrides(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    assert probe.detect().protocol == "halfblock"
    monkeypatch.delenv("TMUX")
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
