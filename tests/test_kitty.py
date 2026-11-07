import base64
import re

import pytest

from cellpix import kitty
from cellpix._diacritics import DIACRITICS


def test_diacritics_start_as_the_spec_says():
    # The protocol's examples: U+0305 is 0, U+030D is 1, U+030E is 2.
    assert DIACRITICS[:3] == (0x0305, 0x030D, 0x030E)
    assert len(DIACRITICS) == 297 == kitty.MAX_CELLS


def test_one_chunk_has_no_m_key():
    code = kitty.transmit(42, b"\x89PNG small")
    assert code.startswith("\x1b_Ga=t,f=100,t=d,i=42,q=2;") and code.endswith("\x1b\\")
    assert "m=" not in code
    assert base64.b64decode(code.split(";", 1)[1][:-2]) == b"\x89PNG small"


def test_chunks_are_4096_multiples_of_4_and_reassemble():
    png = bytes(range(256)) * 40  # 10 KB -> ~13.7 KB of base64: 4 chunks
    code = kitty.transmit(7, png)
    parts = re.findall(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\", code)
    assert [keys for keys, _ in parts] == ["a=t,f=100,t=d,i=7,q=2,m=1", "m=1,q=2", "m=1,q=2", "m=0,q=2"]
    assert all(len(data) == 4096 for _, data in parts[:-1])
    assert base64.b64decode("".join(data for _, data in parts)) == png
    with pytest.raises(ValueError):
        kitty.transmit(7, png, chunk_bytes=4095)


def test_place_delete_query():
    assert kitty.place(42, 10, 3) == "\x1b_Ga=p,U=1,i=42,p=1,c=10,r=3,q=2\x1b\\"
    assert kitty.delete(42) == "\x1b_Ga=d,d=I,i=42,q=2\x1b\\"
    assert kitty.query(31) == "\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\"
    with pytest.raises(ValueError):
        kitty.place(42, 0, 3)
    with pytest.raises(ValueError):
        kitty.place(42, 298, 3)


def test_placeholder_rows_number_every_cell():
    row = kitty.placeholder_row(42, 1, 3)
    assert row == "".join(kitty.PLACEHOLDER + chr(0x030D) + chr(DIACRITICS[column]) for column in range(3))
    # An id with a fourth byte adds a third mark (the spec's example: 42 + (2 << 24)).
    assert kitty.placeholder_row(42 + (2 << 24), 0, 1) == kitty.PLACEHOLDER + chr(0x0305) + chr(0x0305) + chr(0x030E)


def test_id_colour_and_new_ids():
    assert kitty.id_colour(0x123456) == (0x12, 0x34, 0x56)
    for _ in range(200):
        image_id = kitty.new_image_id()
        assert 0x100 <= image_id <= 0xFFFFFF  # fits the colour, never zero
