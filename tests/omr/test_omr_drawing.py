"""tools/omr/vector_read.py and drawing_check.py on a generated engraved PDF:
reading bars from the drawing, and correcting a misread score with it."""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

pymupdf = pytest.importorskip("pymupdf")

import drawing_check  # noqa: E402
import repair  # noqa: E402
import score  # noqa: E402
import vector  # noqa: E402
import vector_read  # noqa: E402
from musicxml_maker import attributes, bar, part, score as make_score  # noqa: E402
from vector_maker import GAP, Engraver  # noqa: E402

BARS = [50.0, 130.0, 210.0, 290.0]
TREBLE = {"C5": 5, "D5": 6, "E5": 7, "F5": 8, "G5": 9, "A5": 10, "B5": 11, "C6": 12}


def engrave(tmp_path: Path) -> tuple[Path, Path]:
    """Three staves of three bars: a flute in F major, a cello, a drum kit.

    Flute   | C5 D5 E5 F5 crotchets | E5 F5 quavers (beamed), G5 dotted
            crotchet, A5 quaver (flag), Bb5 crotchet | C6 minim tied to C6 minim |
    Cello   | whole rest | C3 whole note | C3 crotchet, crotchet rest, minim rest |
    Drums   | hi-hat quavers over two kick minims | hi-hat quavers over one
            kick crotchet on beat 3, no rests drawn | whole rest |
    """
    e = Engraver()
    flute = e.stave(100.0, BARS)
    e.clef(flute, "treble")
    e.sign(flute, "flat", 59.0, 4)                                  # key: one flat, on B4
    for x, name in zip((70.0, 83.0, 96.0, 109.0), ("C5", "D5", "E5", "F5")):
        e.note(flute, x, TREBLE[name], stem="down")
    # bar 2
    tip = flute.y(TREBLE["F5"]) - GAP * 3.5
    for x, name in ((137.0, "E5"), (147.0, "F5")):
        rect = e.head(flute, x, TREBLE[name])
        e.stem(rect.x1, tip, flute.y(TREBLE[name]))
    e.beam(137.0 + 3.5, 147.0 + 3.5, tip)
    e.note(flute, 160.0, TREBLE["G5"], stem="down", dot=True)
    e.note(flute, 175.0, TREBLE["A5"], stem="down", flag=True)
    e.note(flute, 190.0, TREBLE["B5"], stem="down")
    # bar 3
    first, _ = e.note(flute, 220.0, TREBLE["C6"], kind="half", stem="down")
    second, _ = e.note(flute, 255.0, TREBLE["C6"], kind="half", stem="down")
    e.tie(first.x1 + 0.25, second.x0 - 0.25, first.y0 - 0.5)

    cello = e.stave(160.0, BARS)
    e.clef(cello, "bass")
    e.rest(cello, 88.0, "whole")
    e.head(cello, 165.0, 3, "whole")                                # C3: the bass clef's second space
    e.note(cello, 220.0, 3, stem="up")
    e.rest(cello, 240.0, "quarter")
    e.rest(cello, 262.0, "half")

    drums = e.stave(220.0, BARS)
    e.clef(drums, "percussion")
    for left, kicks in ((50.0, (0, 4)), (130.0, (4,))):
        xs = [left + 12.0 + i * 8.0 for i in range(8)]
        tip = drums.y(9) - GAP * 3.5
        for i, x in enumerate(xs):
            rect = e.head(drums, x, 9, "x")
            e.stem(rect.x1, tip, drums.y(9))
            if i % 2:
                e.beam(xs[i - 1] + 3.25, x + 3.25, tip)
        for i in kicks:
            e.note(drums, xs[i], 1, kind="half" if len(kicks) == 2 else "black", stem="down")
    e.rest(drums, 245.0, "whole")

    piece = tmp_path / "piece"
    piece.mkdir()
    pdf = e.save(piece / "page.pdf")
    e.catalogue(pdf, piece)
    return piece, pdf


def drum_bar(number: int, extra: str = "") -> str:
    return (f'<measure number="{number}">{extra}<note><unpitched><display-step>C</display-step>'
            "<display-octave>5</display-octave></unpitched><duration>16</duration><voice>1</voice></note></measure>")


def misread() -> ET.Element:
    """What a reader might make of that page: a wrong pitch, no key, a lost
    tie, a lost whole note, a lost rest; drums as one snare note a bar."""
    flute = part("P1", [
        bar([("C5", 1), ("E5", 1), ("E5", 1), ("F5", 1)], 1, attributes()),
        bar([("E5", 0.5), ("F5", 0.5), ("G5", 1.5), ("A5", 0.5), ("B5", 1)], 2),
        bar([("C6", 2), ("C6", 2)], 3),
    ])
    cello = part("P2", [
        bar([("r", 4)], 1, attributes(clef="F", line=4)),
        bar([("r", 4)], 2),
        bar([("C3", 1), ("r", 2)], 3),
    ])
    drum_attributes = ("<attributes><divisions>4</divisions><key><fifths>0</fifths></key><time><beats>4</beats>"
                       "<beat-type>4</beat-type></time><clef><sign>percussion</sign></clef></attributes>")
    drums = f'<part id="P3">{drum_bar(1, drum_attributes)}{drum_bar(2)}{drum_bar(3)}</part>'
    xml = make_score([("P1", "Flute", flute), ("P2", "Cello", cello), ("P3", "Drum Set", drums)])
    return ET.fromstring(xml)


def notes_of(root: ET.Element, pid: str, number: str) -> list[ET.Element]:
    p = next(p for p in root.findall("part") if p.get("id") == pid)
    return next(m for m in p.findall("measure") if m.get("number") == number).findall("note")


# --- reading the drawing ---------------------------------------------------------------

@pytest.fixture
def page(tmp_path):
    piece, pdf = engrave(tmp_path)
    with pymupdf.open(pdf) as doc:
        pg = doc[0]
        drawn = vector.read_page(pg)
        beams = vector_read.beams_on_page(pg)
        curves = vector_read.curves_on_page(pg)
    vector.name_glyphs(drawn, vector.load_catalogue(piece))
    return drawn, beams, curves


def test_page_has_three_staves_of_three_bars(page):
    drawn, _, _ = page
    assert len(drawn.staves) == 3
    assert [drawn.bar_count(s) for s in range(3)] == [3, 3, 3]


def test_reads_lengths_from_beams_flags_and_dots(page):
    drawn, beams, curves = page
    voices = vector_read.read_bar(drawn, beams, 0, 1, Fraction(4), curves=curves)
    assert [e.length for e in voices["single"]] == [Fraction(1, 2), Fraction(1, 2), Fraction(3, 2),
                                                   Fraction(1, 2), Fraction(1)]


def test_spells_pitches_in_the_drawn_key(page):
    drawn, beams, curves = page
    assert drawing_check.drawn_keys(drawn, 0) == [-1, None, None]
    voices = vector_read.read_bar(drawn, beams, 0, 1, Fraction(4), curves=curves)
    vector_read.spell(voices, vector_read.drawn_clef(drawn, 0), -1)
    assert [h.pitch for e in voices["single"] for h in e.heads] == [
        ("E", 0, 5), ("F", 0, 5), ("G", 0, 5), ("A", 0, 5), ("B", -1, 5)]


def test_reads_a_tie_between_two_notes(page):
    drawn, beams, curves = page
    voices = vector_read.read_bar(drawn, beams, 0, 2, Fraction(4), curves=curves)
    first, second = voices["single"]
    assert (first.heads[0].tie_start, first.heads[0].tie_stop) == (True, False)
    assert (second.heads[0].tie_start, second.heads[0].tie_stop) == (False, True)


def test_reads_whole_and_half_rests_by_where_they_sit(page):
    drawn, beams, curves = page
    whole = vector_read.read_bar(drawn, beams, 1, 0, Fraction(4))
    assert whole["single"][0].whole_bar
    mixed = vector_read.read_bar(drawn, beams, 1, 2, Fraction(4))
    assert [e.length for e in mixed["single"]] == [1, 1, 2]


def test_drum_voices_split_by_stem(page):
    drawn, beams, curves = page
    voices = vector_read.read_bar(drawn, beams, 2, 0, Fraction(4), prefer_two=True)
    assert len(voices["up"]) == 8 and all(e.length == Fraction(1, 2) for e in voices["up"])
    assert [e.length for e in voices["down"]] == [2, 2]


# --- correcting a score with it -------------------------------------------------------------

@pytest.fixture
def checked(tmp_path):
    piece, pdf = engrave(tmp_path)
    root = misread()
    findings, report = drawing_check.check(root, piece, [pdf])
    return root, findings, report


def pitches(notes: list[ET.Element]) -> list:
    return [None if n.find("rest") is not None else
            (n.findtext("pitch/step"), int(n.findtext("pitch/alter") or 0), int(n.findtext("pitch/octave")))
            for n in notes]


def test_staves_are_matched_to_parts(checked):
    _, _, report = checked
    assert any("0=Flute, 1=Cello, 2=Drum Set" in line for line in report)


def test_key_signature_from_the_drawing(checked):
    root, _, _ = checked
    flute = next(p for p in root.findall("part") if p.get("id") == "P1")
    assert flute.find("measure/attributes/key/fifths").text == "-1"


def test_a_wrong_pitch_is_replaced_by_the_drawing(checked):
    root, _, _ = checked
    assert pitches(notes_of(root, "P1", "1")) == [("C", 0, 5), ("D", 0, 5), ("E", 0, 5), ("F", 0, 5)]


def test_a_note_the_key_flattens_is_spelt_flat(checked):
    root, _, _ = checked
    assert pitches(notes_of(root, "P1", "2"))[-1] == ("B", -1, 5)


def test_a_lost_tie_is_put_back(checked):
    root, _, _ = checked
    first, second = notes_of(root, "P1", "3")
    assert [t.get("type") for t in first.findall("tie")] == ["start"]
    assert [t.get("type") for t in second.findall("tie")] == ["stop"]
    assert second.find("notations/tied").get("type") == "stop"


def test_a_lost_whole_note_is_put_back(checked):
    root, _, _ = checked
    (note,) = notes_of(root, "P2", "2")
    assert pitches([note]) == [("C", 0, 3)] and note.findtext("duration") == "16"


def test_a_lost_rest_is_put_back_keeping_the_notes(checked):
    root, findings, _ = checked
    notes = notes_of(root, "P2", "3")
    assert pitches(notes) == [("C", 0, 3), None, None]
    assert [n.findtext("duration") for n in notes] == ["4", "4", "8"]
    assert any(f.bar == "3" and f.part == "Cello" and "rests put back" in f.what for f in findings)


def test_drums_are_written_from_the_drawing(checked):
    root, _, _ = checked
    notes = notes_of(root, "P3", "1")
    hats = [n for n in notes if n.findtext("voice") == "1"]
    kicks = [n for n in notes if n.findtext("voice") == "2"]
    assert len(hats) == 8 and {n.findtext("notehead") for n in hats} == {"x"}
    assert [n.findtext("unpitched/display-step") + n.findtext("unpitched/display-octave") for n in kicks] == \
        ["F4", "F4"]


def test_a_drum_voice_without_rests_is_padded_invisibly(checked):
    root, _, _ = checked
    kicks = [n for n in notes_of(root, "P3", "2") if n.findtext("voice") == "2"]
    assert [(n.get("print-object"), n.find("rest") is not None, n.findtext("duration")) for n in kicks] == [
        ("no", True, "8"), (None, False, "4"), ("no", True, "4")]


def test_bars_left_alone_are_not_touched(tmp_path):
    piece, pdf = engrave(tmp_path)
    root = misread()
    drawing_check.check(root, piece, [pdf], skip={("Flute", "1")})
    assert pitches(notes_of(root, "P1", "1"))[1] == ("E", 0, 5)


def test_no_catalogue_no_check(tmp_path):
    piece, pdf = engrave(tmp_path)
    (piece / "glyphs.json").unlink()
    root = misread()
    findings, report = drawing_check.check(root, piece, [pdf])
    assert findings == [] and "skipped" in report[0]


# --- keeping what is not notes ------------------------------------------------------------

def test_rebuild_keeps_a_direction_at_its_beat():
    measure = ET.fromstring(
        '<measure number="1"><attributes><divisions>1</divisions></attributes>'
        "<note><rest/><duration>2</duration><voice>1</voice></note>"
        "<direction><direction-type><dynamics><f/></dynamics></direction-type></direction>"
        "<note><rest/><duration>2</duration><voice>1</voice></note></measure>")
    new = [ET.fromstring(f"<note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration>"
                         f"<voice>1</voice></note>") for _ in range(4)]
    drawing_check._rebuild(measure, [new], Fraction(1))
    assert [e.tag for e in measure] == ["attributes", "note", "note", "direction", "note", "note"]


def test_divisions_restated_in_a_bar_count_from_the_last():
    measure = ET.fromstring('<measure><attributes><divisions>8</divisions></attributes>'
                            '<attributes><divisions>2</divisions></attributes><note/></measure>')
    assert repair._divisions(measure, {}) == 2
