"""tools/omr/tab.py on a generated tab page: fret numbers on six lines, the
rhythm drawn as stems below, read into MusicXML with string and fret."""
from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

pymupdf = pytest.importorskip("pymupdf")

import score  # noqa: E402
import tab  # noqa: E402
from vector_maker import BAR_WIDTH, GAP, SHAPES, STAVE_WIDTH, Engraver  # noqa: E402

TOP = 100.0
BOTTOM = TOP + 5 * GAP
BARS = [50.0, 130.0, 210.0, 290.0]
STEM_TOP, STEM_FOOT, SHORT_FOOT = BOTTOM + GAP * 1.2, BOTTOM + GAP * 4.7, BOTTOM + GAP * 2.9


def line_y(string: int) -> float:
    return TOP + (string - 1) * GAP


def fret(e: Engraver, x: float, string: int, number: int) -> float:
    """The number's digits from x, centred on its string; returns its centre."""
    width = SHAPES["fret-0"][0]
    for i, digit in enumerate(str(number)):
        e.glyph(f"fret-{digit}", x + i * width, line_y(string) - SHAPES["fret-0"][1] / 2)
    return x + len(str(number)) * width / 2


def engrave_tab(tmp_path: Path) -> Path:
    """Three bars of 4/4 tab:
    | A#2 G#2 quavers (beamed), A3 crotchet (fret 12), crotchet rest, E4 crotchet |
    | D4 dotted minim tied to D4 crotchet | A3 semibreve |"""
    e = Engraver()
    for k in range(6):
        e._line(BARS[0], line_y(k + 1), BARS[-1], line_y(k + 1), STAVE_WIDTH)
    for x in BARS:
        e._line(x, TOP, x, BOTTOM, BAR_WIDTH)
    for y in (TOP + GAP * 0.5, TOP + GAP * 2.5):
        e.glyph("time-4", 53.0, y)
    a = fret(e, 62.0, 5, 1)
    b = fret(e, 72.0, 6, 4)
    for x in (a, b):
        e.stem(x, STEM_TOP, STEM_FOOT)
    e.beam(a, b, STEM_FOOT - 1.5)
    c = fret(e, 85.0, 5, 12)
    e.stem(c, STEM_TOP, STEM_FOOT)
    e.glyph("rest-quarter", 100.0, TOP + GAP)
    d = fret(e, 115.0, 1, 0)
    e.stem(d, STEM_TOP, STEM_FOOT)
    # bar 2
    first = fret(e, 140.0, 2, 3)
    e.stem(first, STEM_TOP, SHORT_FOOT)
    e.glyph("dot", first + 1.5, STEM_TOP + 1.0)
    second = fret(e, 185.0, 2, 3)
    e.stem(second, STEM_TOP, STEM_FOOT)
    e.tie(first + 1.5, second - 1.5, line_y(2) - 2.5)
    # bar 3
    fret(e, 240.0, 3, 2)
    piece = tmp_path / "piece"
    piece.mkdir()
    pdf = e.save(piece / "tab.pdf")
    e.catalogue(pdf, piece)
    return piece


@pytest.fixture
def read(tmp_path):
    piece = engrave_tab(tmp_path)
    return piece, tab.read_tab(piece)


def test_bars_events_and_lengths(read):
    _, bars = read
    assert len(bars) == 3 and bars[0].time == (4, 4)
    assert [(e.length, [(f.string, f.fret) for f in e.frets]) for e in bars[0].events] == [
        (Fraction(1, 2), [(5, 1)]), (Fraction(1, 2), [(6, 4)]), (1, [(5, 12)]), (1, []), (1, [(1, 0)])]
    assert [e.length for e in bars[1].events] == [3, 1]
    assert [e.length for e in bars[2].events] == [4]


def test_a_tie_joins_the_same_fret(read):
    _, bars = read
    first, second = (e.frets[0] for e in bars[1].events)
    assert first.tie_start and second.tie_stop


def test_written_as_a_guitar_stave_with_string_and_fret(read, tmp_path):
    piece, _ = read
    assert tab.main([str(piece)]) == 0
    root = score.load(piece / "omr" / "piece-merged.musicxml")
    assert root.find("part/measure/attributes/clef/clef-octave-change").text == "-1"
    notes = [n for n in root.iter("note") if n.find("pitch") is not None]
    assert [score.midi(n) for n in notes] == [46, 44, 57, 64, 62, 62, 57]
    assert [(n.findtext("notations/technical/string"), n.findtext("notations/technical/fret")) for n in notes] == [
        ("5", "1"), ("6", "4"), ("5", "12"), ("1", "0"), ("2", "3"), ("2", "3"), ("3", "2")]
    assert [t.get("type") for t in notes[4].findall("tie")] == ["start"]
    assert "bars that do not add up: 0 of 3" in (piece / "omr" / "tab-report.txt").read_text()


def test_keys_follow_the_notes():
    c_major = [[60, 62, 64, 65, 67]] * 4
    f_sharp = [[66, 68, 70, 71, 73, 75, 77]] * 4
    assert tab.fifths_by_bar(c_major + f_sharp) == [0] * 4 + [6] * 4


def test_spelling_follows_the_key():
    assert tab.spell(70, 6) == ("A", 1, 4)
    assert tab.spell(70, -2) == ("B", -1, 4)
    assert tab.spell(60, 7) == ("B", 1, 3)                        # B sharp, an octave's edge
    assert tab.spell(61, 0) == ("C", 1, 4)                        # out of key: sharp in C
