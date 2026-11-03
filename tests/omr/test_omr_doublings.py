"""tools/omr/doublings.py: parts that double each other as a second opinion."""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

import doublings  # noqa: E402
from musicxml_maker import attributes, bar, part, score as make_score  # noqa: E402

BARS = 10
CLARINET = "<transpose><diatonic>-1</diatonic><chromatic>-2</chromatic></transpose>"


def line(pitches: list[str], broken: dict[int, list[tuple]] | None = None, transpose: str = "",
         clef: str = "G", octave_line: int = 2) -> list[str]:
    """BARS bars of `pitches` as crotchets; `broken` bars as given."""
    broken = broken or {}
    out = []
    for n in range(1, BARS + 1):
        events = broken.get(n, [(p, 1) for p in pitches])
        out.append(bar(events, n, attributes(transpose=transpose, clef=clef, line=octave_line) if n == 1 else ""))
    return out


def notes(root: ET.Element, pid: str, number: str) -> list[tuple]:
    p = next(p for p in root.findall("part") if p.get("id") == pid)
    measure = next(m for m in p.findall("measure") if m.get("number") == number)
    return [((n.findtext("pitch/step") or "r") + (n.findtext("pitch/alter") or "") + (n.findtext("pitch/octave") or ""),
             n.findtext("duration")) for n in measure.findall("note")]


def test_a_broken_bar_takes_its_doubles_rhythm_and_keeps_its_pitches():
    flute = part("P1", line(["C5", "D5", "E5", "F5"]))
    oboe = part("P2", line(["C5", "D5", "E5", "F5"], {5: [("C5", 1), ("D5", 1.5), ("E5", 1), ("F5", 1)]}))
    root = ET.fromstring(make_score([("P1", "Flute", flute), ("P2", "Oboe", oboe)]))
    report = doublings.doublings(root, Fraction(4))
    assert "doubling: Flute = Oboe" in report[1]
    assert notes(root, "P2", "5") == [("C5", "4"), ("D5", "4"), ("E5", "4"), ("F5", "4")]
    assert any("its own pitches kept" in line for line in report)


def test_pitches_respelt_across_a_transposition():
    # Concert G A C D; a clarinet in Bb writes them A B D E.
    flute = part("P1", line(["G4", "A4", "C5", "D5"]))
    clarinet = part("P2", line(["A4", "B4", "D5", "E5"], {5: [("A4", 1), ("D5", 1)]}, transpose=CLARINET))
    root = ET.fromstring(make_score([("P1", "Flute", flute), ("P2", "Clarinet in Bb", clarinet)]))
    report = doublings.doublings(root, Fraction(4))
    assert notes(root, "P2", "5") == [("A4", "4"), ("B4", "4"), ("D5", "4"), ("E5", "4")]
    assert any("pitches too (check them)" in line for line in report)


def test_an_octave_double_is_moved_by_its_octave():
    flute = part("P1", line(["C6", "D6", "E6", "F6"]))
    oboe = part("P2", line(["C5", "D5", "E5", "F5"], {5: [("C5", 2)]}))
    root = ET.fromstring(make_score([("P1", "Flute", flute), ("P2", "Oboe", oboe)]))
    report = doublings.doublings(root, Fraction(4))
    assert "1 octave(s) above" in report[1]
    assert notes(root, "P2", "5") == [("C5", "4"), ("D5", "4"), ("E5", "4"), ("F5", "4")]


def test_parts_that_play_different_lines_are_no_doubling():
    flute = part("P1", line(["C5", "D5", "E5", "F5"]))
    oboe = part("P2", line(["G4", "A4", "B4", "C5"], {5: [("G4", 2)]}))
    root = ET.fromstring(make_score([("P1", "Flute", flute), ("P2", "Oboe", oboe)]))
    report = doublings.doublings(root, Fraction(4))
    assert report == ["doublings: 0 pair(s) of staves found; 0 bar(s) filled from a double"]
    assert notes(root, "P2", "5") == [("G4", "8")]


def test_a_patched_bar_is_left_alone():
    flute = part("P1", line(["C5", "D5", "E5", "F5"]))
    oboe = part("P2", line(["C5", "D5", "E5", "F5"], {5: [("C5", 2)]}))
    root = ET.fromstring(make_score([("P1", "Flute", flute), ("P2", "Oboe", oboe)]))
    doublings.doublings(root, Fraction(4), skip={("Oboe", "5")})
    assert notes(root, "P2", "5") == [("C5", "8")]


def test_respell_by_an_interval():
    pitch = ET.fromstring("<pitch><step>B</step><alter>-1</alter><octave>3</octave></pitch>")
    assert doublings._respell(pitch, 1, 2)                       # up a major second
    assert (pitch.findtext("step"), pitch.findtext("alter"), pitch.findtext("octave")) == ("C", None, "4")
    pitch = ET.fromstring("<pitch><step>E</step><octave>4</octave></pitch>")
    assert doublings._respell(pitch, 1, 2)
    assert (pitch.findtext("step"), pitch.findtext("alter"), pitch.findtext("octave")) == ("F", "1", "4")
