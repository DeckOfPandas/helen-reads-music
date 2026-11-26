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
    _engrave_into(e)
    piece = tmp_path / "piece"
    piece.mkdir()
    pdf = e.save(piece / "page.pdf")
    e.catalogue(pdf, piece)
    return piece, pdf


def _engrave_into(e: Engraver) -> None:
    """engrave()'s page, drawn with `e`."""
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


def test_a_picture_of_bars_to_check_by_eye(tmp_path):
    piece, _ = engrave(tmp_path)
    out = vector.picture(piece, 1, 0, 1, 2)
    assert out == piece / "omr" / "views" / "bars" / "page1-stave0-bars1-2.png"
    assert out.read_bytes()[:4] == b"\x89PNG"


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


def test_a_mark_nearer_the_stave_above_belongs_to_its_note(tmp_path):
    # A whole note three ledger lines above the lower stave, an accent above
    # it: the accent sits nearer the upper stave, but it is the note's.
    e = Engraver()
    upper = e.stave(100.0, [50.0, 130.0])
    e.rest(upper, 88.0, "whole")
    lower = e.stave(136.0, [50.0, 130.0])
    head = e.head(lower, 88.0, 14, "whole")
    e.glyph("accent", head.x0 + 0.25, head.y0 - 8.0)
    piece = tmp_path / "piece"
    piece.mkdir()
    pdf = e.save(piece / "page.pdf")
    e.catalogue(pdf, piece)
    with pymupdf.open(pdf) as doc:
        drawn = vector.read_page(doc[0])
        beams = vector_read.beams_on_page(doc[0])
    vector.name_glyphs(drawn, vector.load_catalogue(piece))
    accent = next(g for g in drawn.glyphs if g.name == "accent")
    assert accent.stave == 0                                    # placed with the upper stave
    (note,) = vector_read.read_bar(drawn, beams, 1, 0, Fraction(4))["single"]
    assert note.marks == {"accent"}
    (rest,) = vector_read.read_bar(drawn, beams, 0, 0, Fraction(4))["single"]
    assert rest.marks == set()


def read_one_bar(e: Engraver, tmp_path: Path) -> dict:
    piece = tmp_path / "piece"
    piece.mkdir()
    pdf = e.save(piece / "page.pdf")
    e.catalogue(pdf, piece)
    with pymupdf.open(pdf) as doc:
        drawn = vector.read_page(doc[0])
        beams = vector_read.beams_on_page(doc[0])
        curves = vector_read.curves_on_page(doc[0])
    vector.name_glyphs(drawn, vector.load_catalogue(piece))
    return vector_read.read_bar(drawn, beams, 0, 0, Fraction(4), curves=curves)


def test_stacked_flats_in_a_chord_go_to_their_own_notes(tmp_path):
    # Flats in three columns before a four-note chord: each to the head at
    # its bulb, the farthest reached across the column beside it.
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    for step in (0, 2, 4, 6):
        e.head(stave, 100.0, step)
    e.stem(103.5, stave.y(6) - GAP * 3.5, stave.y(0))
    for x, step in ((96.5, 0), (93.75, 4), (91.0, 6)):
        e.sign(stave, "flat", x, step)
    (chord,) = read_one_bar(e, tmp_path)["single"]
    assert {h.step: h.accidental for h in chord.heads} == {0: -1, 2: None, 4: -1, 6: -1}


def test_a_page_background_is_not_a_beam(tmp_path):
    # Some software fills the page white with a four-line path first.
    e = Engraver()
    e.shape.draw_polyline([(0, 0), (e.width, 0), (e.width, e.height), (0, e.height), (0, 0)])
    e.shape.finish(color=None, fill=(1, 1, 1), closePath=True)
    stave = e.stave(100.0, [50.0, 130.0])
    for x in (60.0, 75.0, 90.0, 105.0):
        e.note(stave, x, 5, stem="down")
    assert [ev.length for ev in read_one_bar(e, tmp_path)["single"]] == [1, 1, 1, 1]


def test_a_second_on_a_down_stem_is_one_chord(tmp_path):
    # C5 and D5 on one down-stem: D5 on the stem's right, C5 displaced to its
    # left, the stem starting at D5, just above C5's centre. Four of them.
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    for x in (60.0, 75.0, 90.0, 105.0):
        e.head(stave, x - 3.5, 5)
        top = e.head(stave, x, 6)
        e.stem(x, top.y0 + 1.5, stave.y(6) + GAP * 3.5)
    events = read_one_bar(e, tmp_path)["single"]
    assert [(ev.length, ev.stem, sorted(h.step for h in ev.heads)) for ev in events] == [(1, "down", [5, 6])] * 4


def test_dynamics_where_the_drawing_has_none_are_dropped(tmp_path):
    # An f drawn under the flute's first bar; the read has an f there and a
    # p in bar 2, which the page does not draw (a chord box read as a "p").
    root = misread()
    flute = root.find("part")
    for number, mark in (("1", "f"), ("2", "p")):
        measure = next(m for m in flute.findall("measure") if m.get("number") == number)
        direction = ET.Element("direction")
        ET.SubElement(ET.SubElement(ET.SubElement(direction, "direction-type"), "dynamics"), mark)
        measure.insert(1, direction)
    drawn = Engraver()
    _engrave_into(drawn)
    drawn.glyph("dynamic", 72.0, 128.0)                     # under the flute's bar 1
    piece = tmp_path / "with-f"
    piece.mkdir()
    pdf = drawn.save(piece / "page.pdf")
    drawn.catalogue(pdf, piece)
    _, report = drawing_check.check(root, piece, [pdf])
    assert "  dynamics in bars that draw none, dropped: 1 (Flute 2)" in report
    marks = [[c.tag for d in m.findall("direction") for c in d.iter() if c.tag in ("f", "p")]
             for m in flute.findall("measure")]
    assert marks[:2] == [["f"], []]


def test_a_music_font_character_is_a_glyph_with_its_own_ink_box(tmp_path, monkeypatch):
    # MuPDF's built-in sans stands in for a music font: its "o" a notehead,
    # its "x" one character drawing an accent and a staccato.
    monkeypatch.setitem(vector.FONT_GLYPHS, "NimbusSans-Regular",
                        {ord("o"): "notehead-black", ord("x"): ("accent", "dot-small")})
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    e.page.insert_font(fontname="mus", fontbuffer=pymupdf.Font("helv").buffer)
    e.page.insert_text((70.0, stave.y(4)), "o", fontname="mus", fontsize=12)
    e.page.insert_text((90.0, stave.y(12)), "x", fontname="mus", fontsize=12)
    pdf = e.save(tmp_path / "font.pdf")
    with pymupdf.open(pdf) as doc:
        drawn = vector.read_page(doc[0])
    head = next(g for g in drawn.glyphs if g.name == "notehead-black")
    # The ink of an "o" sits on the baseline and is about half the size tall,
    # where the text box would run the font's whole height above and below.
    assert abs(head.y1 - stave.y(4)) < 0.6 and 4.0 < head.height < 7.5
    assert sorted(g.name for g in drawn.glyphs if g.key.endswith(":0078")) == ["accent", "dot-small"]


def test_a_music_font_dot_is_a_duration_beside_a_head_a_staccato_over_one_and_a_repeat_by_a_bar_line():
    stave = vector.Stave([100.0, 103.0, 106.0, 109.0, 112.0], 50.0, 230.0)
    dot = "font:Maestro:002E"
    glyphs = [vector.Glyph("smufl:E0A4", 70.0, 104.5, 73.5, 107.5, "notehead-black", 0, 0),
              vector.Glyph(dot, 74.5, 104.0, 75.5, 105.0, "dot", 0, 0),         # beside the head
              vector.Glyph(dot, 71.3, 97.0, 72.3, 98.0, "dot", 0, 0),           # over it
              vector.Glyph(dot, 135.5, 103.5, 136.5, 104.5, "dot", 0, 1),       # a repeat's pair, by a bar line
              vector.Glyph(dot, 135.5, 106.5, 136.5, 107.5, "dot", 0, 1)]
    page = vector.Page([stave], [[50.0, 140.0, 230.0]], glyphs, [])
    vector.name_glyphs(page, {})
    assert [g.name for g in glyphs[1:]] == ["dot", "dot-small", "other", "other"]


def test_a_stroke_across_one_stave_only_is_no_bar_line(tmp_path):
    # A divisi bracket (or a chord's stem across the whole stave) on the upper stave, as thick as a bar line.
    e = Engraver()
    upper = e.stave(100.0, [50.0, 130.0, 200.0])
    e.stave(130.0, [50.0, 130.0, 200.0])
    e._line(90.0, upper.top - 2.0, 90.0, upper.bottom + 2.0, e.bar_width)
    pdf = e.save(tmp_path / "bracket.pdf")
    with pymupdf.open(pdf) as doc:
        drawn = vector.read_page(doc[0])
    assert drawn.bar_lines == [[50.0, 130.0, 200.0], [50.0, 130.0, 200.0]]


def test_systems_of_one_stave_keep_bar_lines_their_neighbours_lack():
    lines = [[50.0, 130.0, 200.0], [50.0, 90.0, 160.0, 200.0], [50.0, 120.0, 200.0]]
    assert vector._lined_up(lines) == lines


def test_a_time_signature_between_a_double_bar_line_and_a_repeat_is_not_a_bar(tmp_path):
    e = Engraver()
    e.stave(100.0, [50.0, 130.0, 138.0, 200.0])           # 8 points: under three spaces
    pdf = e.save(tmp_path / "bars.pdf")
    with pymupdf.open(pdf) as doc:
        drawn = vector.read_page(doc[0])
    assert drawn.bar_lines[0] == [50.0, 138.0, 200.0]


def test_a_beam_drawn_as_three_lines_closed_by_its_fill_is_a_beam(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    shape = page.new_shape()
    shape.draw_polyline([(100, 50), (100, 52), (112, 52.8), (112, 50.8)])  # left side, bottom, right side
    shape.finish(color=None, fill=(0, 0, 0), closePath=False)
    shape.commit()
    doc.save(tmp_path / "stroke.pdf")
    with pymupdf.open(tmp_path / "stroke.pdf") as saved:
        beams = vector_read.beams_on_page(saved[0])
    assert [(b.x0, b.x1, round(b.thickness, 1)) for b in beams] == [(100.0, 112.0, 2.0)]


def test_crotchet_pairs_with_strokes_between_are_fingered_tremolos():
    # Two pairs of crotchets, each with four tremolo strokes between, fill two beats, not four.
    gap = 2.0
    heads = [vector_read.Head(5, "black", x, 50.0) for x in (10.0, 20.0, 30.0, 40.0)]
    events = [vector_read.Event(h.x, Fraction(1), [h], "up") for h in heads]
    strokes = [vector_read.Beam(x + 2.0, x + 8.0, 44.0 + k, 42.0 + k, 0.5) for x in (10.0, 30.0) for k in range(4)]
    found = vector_read._find_tremolos(events, strokes, gap, Fraction(2))
    assert [e.length for e in found] == [Fraction(1, 2)] * 4
    assert [sorted(e.marks) for e in found] == [["tremolo-start:4"], ["tremolo-stop:4"]] * 2
    found[0].heads[0].pitch = ("C", 0, 5)
    element = drawing_check._pitched_notes([found[0]], "1", None, 1, Fraction(2), Fraction(2))[0]
    assert element.findtext("type") == "quarter" and element.findtext("duration") == "1"
    assert element.find("notations/ornaments/tremolo").text == "4"


def test_the_clef_and_key_before_a_repeat_at_a_system_start_is_not_a_bar(tmp_path):
    from vector_maker import GAP
    e = Engraver()
    start = 50.0 + 8 * GAP                                 # the repeat sign, eight spaces in
    e.stave(100.0, [50.0, start, start + 40 * GAP, start + 80 * GAP])
    pdf = e.save(tmp_path / "strip.pdf")
    with pymupdf.open(pdf) as doc:
        drawn = vector.read_page(doc[0])
    assert drawn.bar_lines[0] == [start, start + 40 * GAP, start + 80 * GAP]


def test_articulations_and_bowings_are_set_as_drawn_on_notes_found_by_onset_and_pitch():
    measure = ET.fromstring(
        '<measure number="1"><attributes><divisions>1</divisions></attributes>'
        "<note><pitch><step>C</step><octave>5</octave></pitch><duration>1</duration><voice>1</voice>"
        "<notations><articulations><accent/><tenuto/></articulations></notations></note>"
        "<note><pitch><step>D</step><octave>5</octave></pitch><duration>3</duration><voice>1</voice></note>"
        "</measure>")
    c5, d5 = vector_read.Head(5, "black", 60.0, 0.0), vector_read.Head(6, "half", 80.0, 0.0)
    c5.pitch, d5.pitch = ("C", 0, 5), ("D", 0, 5)
    voices = {"single": [vector_read.Event(60.0, Fraction(1), [c5], marks={"staccato", "down-bow"}),
                         vector_read.Event(80.0, Fraction(3), [d5])]}
    # The drawing names accents, staccatos and bowings, but no tenutos.
    assert drawing_check._set_marks(measure, 1, 1, voices, Fraction(1), {"accent", "staccato", "down-bow"})
    first = measure.find("note")
    assert [c.tag for c in first.find("notations/articulations")] == ["tenuto", "staccato"]
    assert [c.tag for c in first.find("notations/technical")] == ["down-bow"]
    assert measure.findall("note")[1].find("notations") is None


def test_a_mark_drawn_far_above_its_note_is_kept_though_not_read_as_the_notes():
    marcato = ("<note><pitch><step>A</step><octave>4</octave></pitch><duration>{d}</duration><voice>1</voice>"
               "<notations><articulations><strong-accent/></articulations></notations></note>")
    measure = ET.fromstring(f'<measure number="1">{marcato.format(d=2)}{marcato.format(d=2)}</measure>')
    first, second = vector_read.Head(3, "half", 60.0, 0.0), vector_read.Head(3, "half", 90.0, 0.0)
    first.pitch = second.pitch = ("A", 0, 4)
    voices = {"single": [vector_read.Event(60.0, Fraction(2), [first]), vector_read.Event(90.0, Fraction(2), [second])]}
    # A marcato drawn above the stave over the first note only, too far to be read as its own.
    drawing_check._set_marks(measure, 1, 1, voices, Fraction(1), {"strong"}, [(60.5, "strong")], 3.6)
    assert [n.find("notations/articulations/strong-accent") is not None for n in measure.findall("note")] == [
        True, False]


def test_smufl_dynamics_are_read_and_letters_side_by_side_are_one():
    stave = vector.Stave([100.0, 103.0, 106.0, 109.0, 112.0], 50.0, 230.0)
    glyphs = [vector.Glyph("smufl:E521", 60.0, 116.0, 63.0, 119.0, "dynamic", 0, 0),      # m
              vector.Glyph("smufl:E522", 63.2, 116.0, 66.0, 119.0, "dynamic", 0, 0),      # f, beside it: mf
              vector.Glyph("smufl:E539", 150.0, 116.0, 156.0, 119.0, "dynamic", 0, 1),    # sfz
              vector.Glyph("smufl:E0A4", 70.0, 104.0, 73.0, 107.0, "notehead-black", 0, 0)]
    page = vector.Page([stave], [[50.0, 140.0, 230.0]], glyphs, [])
    system = drawing_check.DrawnSystem(1, Path("x.pdf"), 0, page, [], [], [0], ["1", "2"], {0: 0})
    assert drawing_check._drawn_dynamics(system) == {(0, 0): [(60.0, "mf")], (0, 1): [(150.0, "sfz")]}


def test_an_f_and_a_p_side_by_side_are_fp_though_the_f_rises_higher():
    stave = vector.Stave([100.0, 103.0, 106.0, 109.0, 112.0], 50.0, 230.0)
    glyphs = [vector.Glyph("smufl:E522", 60.0, 114.0, 63.0, 121.0, "dynamic", 0, 0),      # f: tall
              vector.Glyph("smufl:E520", 63.2, 116.5, 66.0, 121.0, "dynamic", 0, 0),      # p: x-height
              vector.Glyph("smufl:E522", 150.0, 114.0, 153.0, 121.0, "dynamic", 0, 1),    # f, then
              vector.Glyph("smufl:E52C", 154.0, 116.5, 160.0, 121.0, "dynamic", 0, 1)]    # mp: no "fmp"
    page = vector.Page([stave], [[50.0, 140.0, 230.0]], glyphs, [])
    system = drawing_check.DrawnSystem(1, Path("x.pdf"), 0, page, [], [], [0], ["1", "2"], {0: 0})
    assert drawing_check._drawn_dynamics(system) == {(0, 0): [(60.0, "fp")], (0, 1): [(150.0, "f"), (154.0, "mp")]}


def test_a_misread_dynamic_is_replaced_by_the_drawn_one_unless_the_bar_draws_an_unknown_one():
    # Bar 1 draws sffz and Audiveris read ff; bar 2 draws an f and a shape named only "dynamic",
    # and the read p there stays: the drawing can't say all the bar holds.
    stave = vector.Stave([100.0, 103.0, 106.0, 109.0, 112.0], 50.0, 230.0)
    glyphs = [vector.Glyph("smufl:E53B", 60.0, 116.0, 66.0, 119.0, "dynamic", 0, 0),
              vector.Glyph("smufl:E522", 150.0, 116.0, 153.0, 119.0, "dynamic", 0, 1),
              vector.Glyph("l:0,0;1,1", 170.0, 116.0, 173.0, 119.0, "dynamic", 0, 1)]
    page = vector.Page([stave], [[50.0, 140.0, 230.0]], glyphs, [])
    system = drawing_check.DrawnSystem(1, Path("x.pdf"), 0, page, [], [], [0], ["1", "2"], {0: 0})
    bars = "".join(f'<measure number="{n}"><attributes><divisions>1</divisions>'
                   "<time><beats>4</beats><beat-type>4</beat-type></time></attributes>"
                   f"<direction><direction-type><dynamics><{mark}/></dynamics></direction-type></direction>"
                   "<note><rest/><duration>4</duration></note></measure>" for n, mark in (("1", "ff"), ("2", "p")))
    flute = ET.fromstring(f'<part id="P1">{bars}</part>')
    _, written = drawing_check._match_dynamics([system], [drawing_check.Slot("Flute", 1)], {"Flute": flute}, None)
    marks = [[c.tag for c in m.iter() if c.tag in ("ff", "sffz", "p", "f")] for m in flute.findall("measure")]
    assert marks == [["sffz"], ["p"]]
    assert written == ["Flute 1 sffz (read ff)"]


def test_slurs_no_drawn_curve_joins_are_taken_out():
    # Four crotchets B5 A5 B5 A5, slurred in pairs on the page. The read has those two slurs,
    # one more from the first note to the third, and the first pair's slur twice.
    gap = 2.0
    events = []
    for x, (step, octave) in zip((10.0, 20.0, 30.0, 40.0), [("B", 5), ("A", 5)] * 2):
        head = vector_read.Head(0, "black", x, 50.0)
        head.pitch = (step, 0, octave)
        events.append(vector_read.Event(x, Fraction(1), [head], "up"))
    curves = [vector_read.Curve((10.5, 46.0), (19.5, 46.0)), vector_read.Curve((30.5, 46.0), (39.5, 46.0))]
    slurs = [[("start", "1"), ("start", "2"), ("start", "3")], [("stop", "1"), ("stop", "3")],
             [("start", "1"), ("stop", "2")], [("stop", "1")]]
    notes = "".join(
        f"<note><pitch><step>{step}</step><octave>5</octave></pitch><duration>1</duration><voice>1</voice>"
        "<type>quarter</type><notations>"
        + "".join(f'<slur type="{t}" number="{n}"/>' for t, n in marks) + "</notations></note>"
        for step, marks in zip("BABA", slurs))
    measure = ET.fromstring(f'<measure number="1"><attributes><divisions>1</divisions></attributes>{notes}</measure>')
    removed = drawing_check._check_slurs(measure, 1, 1, {"single": events}, Fraction(1), curves, gap)
    assert removed == 2
    left = [[(s.get("type"), s.get("number")) for s in n.findall("notations/slur")] for n in measure.findall("note")]
    assert left == [[("start", "1")], [("stop", "1")], [("start", "1")], [("stop", "1")]]


def test_dynamics_are_left_alone_where_the_drawing_names_none(checked):
    _, _, report = checked
    assert not any("dynamics in bars" in line for line in report)


def test_a_whole_note_under_a_line_of_up_stems_is_a_second_voice(tmp_path):
    # | crotchet rest, C5 crotchet, D5 minim | over a whole-note B4 below,
    # held from the first beat: every stem points up, but the bar is two voices.
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    e.rest(stave, 60.0, "quarter")
    e.head(stave, 61.0, 1, "whole")
    e.note(stave, 80.0, 5)
    e.note(stave, 100.0, 6, kind="half")
    voices = read_one_bar(e, tmp_path)
    assert [(ev.length, [h.step for h in ev.heads]) for ev in voices["up"]] == [(1, []), (1, [5]), (2, [6])]
    assert [(ev.length, [h.step for h in ev.heads]) for ev in voices["down"]] == [(4, [1])]


def test_a_whole_note_chord_with_a_second_is_one_chord(tmp_path):
    # B3, D4 and E4 as whole notes: E4 a second above D4, drawn a head to the right.
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    e.head(stave, 80.0, -3, "whole")
    e.head(stave, 80.0, -1, "whole")
    e.head(stave, 84.5, 0, "whole")
    (chord,) = read_one_bar(e, tmp_path)["single"]
    assert chord.length == 4 and sorted(h.step for h in chord.heads) == [-3, -1, 0]


def test_a_chord_spanning_its_up_stem_is_read_up(tmp_path):
    # Kick F4 and hi-hat G5 on one up-stem: the stem runs further below the
    # hi-hat than above it, but its foot is at the kick, so it goes up.
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    for x in (60.0, 75.0, 90.0, 105.0):
        kick = e.head(stave, x, 1)
        e.head(stave, x, 9, "x")
        e.stem(kick.x1, stave.y(9) - GAP * 2.5, stave.y(1))
    events = read_one_bar(e, tmp_path)["single"]
    assert [(ev.length, ev.stem, sorted(h.step for h in ev.heads)) for ev in events] == [(1, "up", [1, 9])] * 4


def test_a_double_dot_adds_three_quarters(tmp_path):
    # | minim, double-dotted crotchet, quaver |
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    e.note(stave, 60.0, 5, kind="half", stem="down")
    rect, _ = e.note(stave, 80.0, 5, stem="down", dot=True)
    e.glyph("dot", rect.x1 + 3.0, rect.y0 + 1.0)
    e.note(stave, 110.0, 5, stem="down", flag=True)
    lengths = [ev.length for ev in read_one_bar(e, tmp_path)["single"]]
    assert lengths == [2, Fraction(7, 4), Fraction(1, 2)]


def test_dots_pushed_out_to_line_up_still_count(tmp_path):
    # | dotted minim, its dot two spaces out; crotchet |
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    rect, _ = e.note(stave, 60.0, 5, kind="half", stem="down")
    e.glyph("dot", rect.x1 + 6.5, rect.y0 + 1.0)
    e.note(stave, 100.0, 5, stem="down")
    assert [ev.length for ev in read_one_bar(e, tmp_path)["single"]] == [3, 1]


def test_a_chord_tied_on_past_the_system_end(tmp_path):
    # A three-note chord, each note with a short tie running off the system.
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    e.note(stave, 60.0, 5, kind="whole", stem=None)
    for step in (0, 2, 4):
        rect = e.head(stave, 110.0, step, "half")
    e.stem(rect.x1, stave.y(4) - GAP * 3.5, stave.y(0))
    for step in (0, 2, 4):
        e.tie(rect.x1 + 0.25, rect.x1 + 8.0, stave.y(step) + 1.0)
    _, chord = read_one_bar(e, tmp_path)["single"]
    assert [h.tie_start for h in chord.heads] == [True, True, True]


def test_two_ties_drawn_as_one_path_are_two_ties(tmp_path):
    # | C5 tied to C5, then E5 tied to E5 |, both ties in one filled path.
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    heads = [e.note(stave, x, step, stem="down")[0] for x, step in ((60.0, 5), (75.0, 5), (95.0, 7), (110.0, 7))]
    for first, second in ((heads[0], heads[1]), (heads[2], heads[3])):
        x0, x1, y = first.x1 + 0.25, second.x0 - 0.25, first.y0 - 0.5
        middle = (x0 + x1) / 2
        e.shape.draw_bezier((x0, y), (middle - 1, y - 2.5), (middle + 1, y - 2.5), (x1, y))
        e.shape.draw_bezier((x1, y), (middle + 1, y - 1.5), (middle - 1, y - 1.5), (x0, y))
    e.shape.finish(color=None, fill=(0, 0, 0), closePath=False)
    events = read_one_bar(e, tmp_path)["single"]
    assert [(ev.heads[0].tie_start, ev.heads[0].tie_stop) for ev in events] == [
        (True, False), (False, True), (True, False), (False, True)]


def test_a_tie_with_blunt_ends_is_a_tie(tmp_path):
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    first, _ = e.note(stave, 60.0, 5, kind="half", stem="down")
    second, _ = e.note(stave, 95.0, 5, kind="half", stem="down")
    e.tie(first.x1 + 0.25, second.x0 - 0.25, first.y0 - 0.5, blunt=True)
    first_note, second_note = read_one_bar(e, tmp_path)["single"]
    assert first_note.heads[0].tie_start and second_note.heads[0].tie_stop


def test_a_short_squat_tie_is_a_tie(tmp_path):
    # Two crotchets close together: their tie is 5 points wide, 2.8 high.
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    first, _ = e.note(stave, 60.0, 5, stem="down")
    second, _ = e.note(stave, 70.5, 5, stem="down")
    e.shape.draw_bezier((first.x1 + 1.0, first.y0), (first.x1 + 2.0, first.y0 - 2.8),
                        (second.x0 - 2.0, first.y0 - 2.8), (second.x0 - 1.0, first.y0))
    e.shape.draw_bezier((second.x0 - 1.0, first.y0), (second.x0 - 2.0, first.y0 - 2.0),
                        (first.x1 + 2.0, first.y0 - 2.0), (first.x1 + 1.0, first.y0))
    e.shape.finish(color=None, fill=(0, 0, 0), closePath=True)
    e.note(stave, 90.0, 5, kind="half", stem="down")
    one, two, _ = read_one_bar(e, tmp_path)["single"]
    assert one.heads[0].tie_start and two.heads[0].tie_stop


def test_a_stave_drawn_bar_by_bar_is_one_stave(tmp_path):
    e = Engraver()
    e.stave(100.0, BARS, by_bar=True)
    pdf = e.save(tmp_path / "page.pdf")
    with pymupdf.open(pdf) as doc:
        drawn = vector.read_page(doc[0])
    assert len(drawn.staves) == 1 and drawn.bar_count(0) == 3


class WithMusicText:
    """A real page, plus music written as SMuFL characters: (codepoint, x,
    y of its origin). Test fonts have no SMuFL glyphs, so the characters
    are given as the page's text trace."""

    def __init__(self, page, chars: list[tuple[int, float, float]]):
        self._page, self._chars = page, chars

    def __getattr__(self, name):
        return getattr(self._page, name)

    def get_texttrace(self):
        return [{"size": 4 * GAP, "chars": [(code, 1, (x, y), (x, y - 2 * GAP, x + 3.5, y + 2 * GAP))
                                            for code, x, y in self._chars]}]


def test_music_written_in_a_smufl_font_is_read(tmp_path):
    # | x-head crotchet G5, black crotchets C5 F4, a half rest |
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    heads = [(0xE0A9, 60.0, 9, "up"), (0xE0A4, 75.0, 5, "up"), (0xE0A4, 90.0, 1, "up")]
    for _, x, step, _ in heads:
        e.stem(x + 3.54, stave.y(step) - GAP * 3.5, stave.y(step))
    pdf = e.save(tmp_path / "page.pdf")
    chars = [(code, x, stave.y(step)) for code, x, step, _ in heads] + [(0xE4E4, 105.0, stave.y(4))]
    with pymupdf.open(pdf) as doc:
        drawn = vector.read_page(WithMusicText(doc[0], chars))
    vector.name_glyphs(drawn, {})
    events = vector_read.read_bar(drawn, [], 0, 0, Fraction(4))["single"]
    assert [(e.length, [(h.kind, h.step) for h in e.heads]) for e in events] == [
        (1, [("x", 9)]), (1, [("black", 5)]), (1, [("black", 1)]), (2, [])]


def test_a_page_drawing_all_its_symbols_as_one_path_is_read_symbol_by_symbol(tmp_path):
    # A browser's print of a score: every symbol on the page outlined in one filled path.
    e = Engraver(merged=True)
    upper, lower = e.stave(100.0, [50.0, 130.0]), e.stave(160.0, [50.0, 130.0])
    for stave, step in ((upper, 4), (lower, 2)):
        e.clef(stave, "treble")
        for x in (60.0, 75.0, 90.0, 105.0):
            e.note(stave, x, step, stem="up")
    events = read_one_bar(e, tmp_path)["single"]
    with pymupdf.open(tmp_path / "piece" / "page.pdf") as doc:
        assert len([d for d in doc[0].get_drawings() if d.get("fill") == (0.0, 0.0, 0.0)]) == 1
    assert [(ev.length, [h.step for h in ev.heads]) for ev in events] == [(1, [4])] * 4


def test_a_symbol_of_several_outlines_side_by_side_stays_one_symbol(tmp_path):
    # A bass clef and its two dots in one path, on a page that draws each symbol on its own.
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    e.note(stave, 80.0, 4)
    e.shape.draw_polyline([(52.0, 101.0), (58.0, 103.0), (54.0, 110.0), (52.0, 101.0)])
    e.shape.draw_oval(pymupdf.Rect(59.0, 102.0, 60.0, 103.0))
    e.shape.draw_oval(pymupdf.Rect(59.0, 105.0, 60.0, 106.0))
    e.shape.finish(color=None, fill=(0, 0, 0), closePath=True)
    pdf = e.save(tmp_path / "page.pdf")
    with pymupdf.open(pdf) as doc:
        drawn = vector.read_page(doc[0])
    clefs = [g for g in drawn.glyphs if g.x0 < 61.0]
    assert [(round(g.x0), round(g.x1)) for g in clefs] == [(52, 60)]


def engrave_octave_clef(tmp_path: Path) -> tuple[Path, Path]:
    """A treble clef with its small 8 drawn apart below it; four B crotchets."""
    e = Engraver()
    stave = e.stave(100.0, [50.0, 130.0])
    e.clef(stave, "treble")
    e.glyph("clef-8", stave.left + 3.5, stave.y(4) + 10.3)
    for x in (66.0, 80.0, 94.0, 108.0):
        e.note(stave, x, 4)
    piece = tmp_path / "piece"
    piece.mkdir()
    pdf = e.save(piece / "page.pdf")
    e.catalogue(pdf, piece)
    return piece, pdf


def test_an_8_drawn_under_a_clef_makes_it_an_octave_clef(tmp_path):
    piece, pdf = engrave_octave_clef(tmp_path)
    with pymupdf.open(pdf) as doc:
        drawn = vector.read_page(doc[0])
    vector.name_glyphs(drawn, vector.load_catalogue(piece))
    assert vector_read.drawn_clef(drawn, 0) == ("G", 2, -1)
    assert vector_read.pitch_at(4, ("G", 2, -1)) == ("B", 3)
    assert vector_read.pitch_at(4, ("G", 2)) == ("B", 4)


def test_a_lost_octave_clef_is_put_back_and_its_notes_moved(tmp_path):
    # The reader missed the 8: a plain treble clef, the notes an octave high.
    piece, pdf = engrave_octave_clef(tmp_path)
    root = ET.fromstring(make_score([("P1", "Guitar", part("P1", [bar([("B4", 1)] * 4, 1, attributes())]))]))
    _, report = drawing_check.check(root, piece, [pdf])
    assert any("bar 1 Guitar: clef octave drawn -1, score 0 -- FIXED" in line for line in report)
    assert root.findtext(".//clef/clef-octave-change") == "-1"
    assert pitches(notes_of(root, "P1", "1")) == [("B", 0, 3)] * 4
    assert "0 bar(s) replaced" in report[0]


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


def test_a_kit_written_as_one_voice_with_stems_both_ways(tmp_path):
    # | crotchet rest, hi-hat crotchet stem up, crotchet rest, two quavers of
    # hi-hat and kick on one down-stem | as some engravers write a kit.
    e = Engraver()
    drums = e.stave(220.0, [50.0, 130.0])
    e.clef(drums, "percussion")
    e.rest(drums, 60.0, "quarter")
    e.note(drums, 75.0, 9, kind="x", stem="up")
    e.rest(drums, 90.0, "quarter")
    for x in (102.0, 114.0):
        e.head(drums, x, 9, "x")
        e.head(drums, x, 1)
        e.stem(x, drums.y(9), drums.y(1) + GAP * 3.5)
    e.beam(102.0, 114.0, drums.y(1) + GAP * 3.5 - 1.5)
    piece = tmp_path / "piece"
    piece.mkdir()
    pdf = e.save(piece / "page.pdf")
    e.catalogue(pdf, piece)
    drum_attributes = ("<attributes><divisions>4</divisions><key><fifths>0</fifths></key><time><beats>4</beats>"
                       "<beat-type>4</beat-type></time><clef><sign>percussion</sign></clef></attributes>")
    root = ET.fromstring(make_score([("P1", "Drum Set", f'<part id="P1">{drum_bar(1, drum_attributes)}</part>')]))
    _, report = drawing_check.check(root, piece, [pdf])
    assert "1 drum bar(s) written from the drawing" in report[0]
    notes = [n for n in notes_of(root, "P1", "1") if n.find("rest") is None]
    assert sorted(n.findtext("notehead") or "normal" for n in notes) == ["normal", "normal", "x", "x", "x"]


def test_a_drum_voice_without_rests_is_padded_invisibly(checked):
    root, _, _ = checked
    kicks = [n for n in notes_of(root, "P3", "2") if n.findtext("voice") == "2"]
    assert [(n.get("print-object"), n.find("rest") is not None, n.findtext("duration")) for n in kicks] == [
        ("no", True, "8"), (None, False, "4"), ("no", True, "4")]


def _kick(x: float, length: Fraction) -> vector_read.Event:
    return vector_read.Event(x, length, [vector_read.Head(1, "black", x, 0.0)], "down")


def test_a_short_drum_voice_is_padded_where_its_gaps_are():
    # Hi-hat quavers every 10 pt from x 60. Kicks: a crotchet on beat 1, then
    # nothing until a quaver on beat 3 and a semiquaver straight after it.
    hats = [vector_read.Event(60.0 + 10 * i, Fraction(1, 2), [vector_read.Head(9, "x", 60.0 + 10 * i, 0.0)], "up")
            for i in range(8)]
    kicks = [_kick(60.0, Fraction(1)), _kick(100.0, Fraction(1, 2)), _kick(105.0, Fraction(1, 4))]
    padded = drawing_check._pad({"up": hats, "down": kicks}, Fraction(4))["down"]
    assert [(e.length, e.hidden) for e in padded] == [
        (1, False), (1, True), (Fraction(1, 2), False), (Fraction(1, 4), False), (Fraction(5, 4), True)]


def test_a_drum_note_after_a_drawn_rest_follows_it_between_the_other_voices_onsets():
    # Kick crotchet, dotted quaver rest drawn, a semiquaver on the "a" of beat 2
    # (between two hi-hat quavers), a quaver on beat 3; the rest of the bar left empty.
    hats = [vector_read.Event(60.0 + 10 * i, Fraction(1, 2), [vector_read.Head(9, "x", 60.0 + 10 * i, 0.0)], "up")
            for i in range(8)]
    kicks = [_kick(60.0, Fraction(1)), vector_read.Event(80.0, Fraction(3, 4)), _kick(95.0, Fraction(1, 4)),
             _kick(100.0, Fraction(1, 2))]
    padded = drawing_check._pad({"up": hats, "down": kicks}, Fraction(4))["down"]
    assert [(e.length, e.hidden) for e in padded] == [
        (1, False), (Fraction(3, 4), False), (Fraction(1, 4), False), (Fraction(1, 2), False), (Fraction(3, 2), True)]


def test_a_drum_bar_with_a_flam_is_compared_never_rewritten(tmp_path):
    # The kit page of the one-voice test, read into a bar holding a grace note:
    # the drawing reads no grace notes, so rewriting the bar would lose it.
    e = Engraver()
    drums = e.stave(220.0, [50.0, 130.0])
    e.clef(drums, "percussion")
    for x in (60.0, 75.0, 90.0, 105.0):
        e.note(drums, x, 5, stem="up")
    piece = tmp_path / "piece"
    piece.mkdir()
    pdf = e.save(piece / "page.pdf")
    e.catalogue(pdf, piece)
    grace = ('<note><grace slash="yes"/><unpitched><display-step>C</display-step><display-octave>5</display-octave>'
             "</unpitched><voice>1</voice><type>eighth</type></note>")
    drum_attributes = ("<attributes><divisions>4</divisions><key><fifths>0</fifths></key><time><beats>4</beats>"
                       "<beat-type>4</beat-type></time><clef><sign>percussion</sign></clef></attributes>")
    root = ET.fromstring(make_score([("P1", "Drum Set", f'<part id="P1">{drum_bar(1, drum_attributes + grace)}</part>')]))
    _, report = drawing_check.check(root, piece, [pdf])
    assert "0 drum bar(s) written from the drawing" in report[0]
    assert any("bar 1 Drum Set: drum bar differs from the drawing's reading" in line for line in report)
    assert notes_of(root, "P1", "1")[0].find("grace") is not None


def test_drum_signature_merges_a_run_of_rests_and_leaves_out_grace_notes():
    def note(body: str, duration: int | None, voice: str = "1") -> str:
        length = f"<duration>{duration}</duration>" if duration is not None else ""
        return f"<note>{body}{length}<voice>{voice}</voice></note>"
    snare = "<unpitched><display-step>C</display-step><display-octave>5</display-octave></unpitched>"
    one = ET.fromstring("<measure>" + note(snare, 4) + note("<rest/>", 4) + note("<rest/>", 8) + "</measure>")
    two = ET.fromstring("<measure>" + note("<grace/>" + snare, None) + note(snare, 4)
                        + note('<rest/>', 12) + "</measure>")
    assert drawing_check._drum_signature(one) == drawing_check._drum_signature(two)


def test_bars_left_alone_are_not_touched(tmp_path):
    piece, pdf = engrave(tmp_path)
    root = misread()
    drawing_check.check(root, piece, [pdf], skip={("Flute", "1")})
    assert pitches(notes_of(root, "P1", "1"))[1] == ("E", 0, 5)


def test_a_patched_bar_gains_the_drawn_ties_and_keeps_its_own(tmp_path):
    # Bar 3's tie is drawn but lost from the score; bar 1's last note carries
    # a tie the patch wrote and the drawing does not show.
    piece, pdf = engrave(tmp_path)
    root = misread()
    flute = next(p for p in root.findall("part") if p.get("id") == "P1")
    bar1 = flute.find("measure")
    bar1.remove(bar1.findall("note")[1])
    bar1.insert(list(bar1).index(bar1.findall("note")[1]),
                ET.fromstring("<note><pitch><step>D</step><octave>5</octave></pitch><duration>4</duration>"
                              "<voice>1</voice></note>"))
    last = bar1.findall("note")[-1]
    last.append(ET.fromstring('<tie type="start"/>'))
    drawing_check.check(root, piece, [pdf], only={("Flute", "1"), ("Flute", "3")})
    assert [t.get("type") for t in notes_of(root, "P1", "1")[-1].findall("tie")] == ["start"]
    first, second = notes_of(root, "P1", "3")
    assert [t.get("type") for t in first.findall("tie")] == ["start"]
    assert [t.get("type") for t in second.findall("tie")] == ["stop"]


def test_no_catalogue_no_check(tmp_path):
    piece, pdf = engrave(tmp_path)
    (piece / "glyphs.json").unlink()
    root = misread()
    findings, report = drawing_check.check(root, piece, [pdf])
    assert findings == [] and "skipped" in report[0]


# --- a part printed several systems to a page, over several pages ---------------------------

PIANO_BARS = [50.0, 130.0, 210.0]
SYSTEM_TOPS = (100.0, 220.0)                                        # the treble stave's; the bass 40 below


def treble_steps(number: int) -> list[int]:
    """Bar `number`'s four treble crotchets, C5 to F5 in a turn that differs bar to bar."""
    return [5 + (number + i) % 4 for i in range(4)]


def engrave_piano(tmp_path: Path, stem_width: float = 0.35, bar_width: float = 0.5) -> tuple[Path, Path]:
    """A piano part on grand staves, two systems a page of two bars each, over
    two pages, in one PDF: bars 1-8. Every bar: four treble crotchets, the
    first a chord on D4 with its stem across the whole stave; a bass whole
    note C3."""
    e = Engraver(stem_width=stem_width, bar_width=bar_width)
    number = 1
    for page_index in range(2):
        if page_index:
            e.new_page()
        for top in SYSTEM_TOPS:
            treble = e.stave(top, PIANO_BARS)
            bass = e.stave(top + 40.0, PIANO_BARS)
            e.join(treble, bass)
            e.clef(treble, "treble")
            e.clef(bass, "bass")
            for left in PIANO_BARS[:-1]:
                first, *rest = treble_steps(number)
                low = e.head(treble, left + 20.0, -1)
                e.head(treble, left + 20.0, first)
                e.stem(low.x1, treble.y(first) - GAP * 3.5, treble.y(-1))
                for i, step in enumerate(rest, start=1):
                    e.note(treble, left + 20.0 + 15.0 * i, step, stem="down")
                e.head(bass, left + 35.0, 3, "whole")
                number += 1
    piece = tmp_path / "piece"
    piece.mkdir()
    pdf = e.save(piece / "piano.pdf")
    e.catalogue(pdf, piece)
    return piece, pdf


PIANO_ATTRIBUTES = ("<attributes><divisions>4</divisions><key><fifths>0</fifths></key><time><beats>4</beats>"
                    "<beat-type>4</beat-type></time><staves>2</staves><clef number=\"1\"><sign>G</sign>"
                    "<line>2</line></clef><clef number=\"2\"><sign>F</sign><line>4</line></clef></attributes>")


def piano_note(step: str, octave: int, duration: int, staff: int, voice: int, chord: bool = False) -> str:
    body = "<rest/>" if step == "r" else f"<pitch><step>{step}</step><octave>{octave}</octave></pitch>"
    return (f"<note>{'<chord/>' if chord else ''}{body}<duration>{duration}</duration><voice>{voice}</voice>"
            f"<staff>{staff}</staff></note>")


def misread_piano(wrong_treble: int, lost_bass: int) -> ET.Element:
    """The piano part as read, but bar `wrong_treble`'s first note a step
    high and bar `lost_bass`'s whole note read as a rest."""
    names = "CDEFGAB"
    bars = []
    for number in range(1, 9):
        steps = treble_steps(number)
        if number == wrong_treble:
            steps[0] += 1
        treble = piano_note("D", 4, 4, 1, 1) + "".join(
            piano_note(names[(s - 5) % 7], 5 + (s - 5) // 7, 4, 1, 1, chord=i == 0) for i, s in enumerate(steps))
        bass = piano_note("r" if number == lost_bass else "C", 3, 16, 2, 5)
        bars.append(f'<measure number="{number}">{PIANO_ATTRIBUTES if number == 1 else ""}{treble}'
                    f"<backup><duration>16</duration></backup>{bass}</measure>")
    return ET.fromstring(make_score([("P1", "Piano", f'<part id="P1">{"".join(bars)}</part>')]))


def test_a_page_of_grand_staves_is_two_staves_a_system(tmp_path):
    _, pdf = engrave_piano(tmp_path)
    with pymupdf.open(pdf) as doc:
        drawn = vector.read_page(doc[1])
    assert drawn.systems(2) == [[0, 1], [2, 3]]
    assert drawn.systems(4) == [[0, 1, 2, 3]]                    # a full score: the page is one system


def test_stems_as_wide_as_some_software_draws_them_are_not_bar_lines(tmp_path):
    # Stems at 0.6 pt, bar lines at 0.8: every stem crossing a stave would
    # otherwise split its bar.
    _, pdf = engrave_piano(tmp_path, stem_width=0.6, bar_width=0.8)
    with pymupdf.open(pdf) as doc:
        drawn = vector.read_page(doc[0])
    assert [drawn.bar_count(s) for s in range(4)] == [2, 2, 2, 2]


def test_every_page_of_a_pdf_is_a_page_of_the_piece(tmp_path):
    piece, pdf = engrave_piano(tmp_path)
    assert vector.pdf_pages([pdf]) == [(pdf, 0), (pdf, 1)]
    out = vector.picture(piece, 2, 3, 0, 1)
    assert out.name == "page2-stave3-bars0-1.png" and out.read_bytes()[:4] == b"\x89PNG"


@pytest.fixture(params=[(0.35, 0.5), (0.6, 0.8)], ids=["thin stems", "wide stems"])
def piano_checked(tmp_path, request):
    piece, pdf = engrave_piano(tmp_path, *request.param)
    root = misread_piano(wrong_treble=7, lost_bass=4)
    findings, report = drawing_check.check(root, piece, [pdf])
    return root, findings, report


def test_each_system_of_each_page_is_matched_to_the_grand_stave(piano_checked):
    _, _, report = piano_checked
    layout = [line.strip() for line in report[report.index(
        "drawn staves matched to parts (stave numbers from the top of the page, from 0):") + 1:]]
    assert layout == ["page 1 (bars 1-2): 0=Piano/1, 1=Piano/2", "page 1 (bars 3-4): 2=Piano/1, 3=Piano/2",
                      "page 2 (bars 5-6): 0=Piano/1, 1=Piano/2", "page 2 (bars 7-8): 2=Piano/1, 3=Piano/2"]


def test_bars_on_a_later_system_and_page_are_corrected(piano_checked):
    root, findings, _ = piano_checked
    names = "CDEFGAB"
    treble = [n for n in notes_of(root, "P1", "7") if n.findtext("staff") == "1"]
    assert pitches(treble) == [("D", 0, 4)] + [(names[(s - 5) % 7], 0, 5) for s in treble_steps(7)]
    bass = [n for n in notes_of(root, "P1", "4") if n.findtext("staff") == "2"]
    assert pitches(bass) == [("C", 0, 3)]
    assert {(f.bar, f.staff) for f in findings} == {("7", 1), ("4", 2)}


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


def test_x_heads_and_default_x_written_from_the_drawing():
    head = vector_read.Head(2, "x", 72.0, 0.0, left=70.0)
    head.pitch = ("B", -1, 4)
    event = vector_read.Event(72.0, Fraction(1), [head], "up")
    (note,) = drawing_check._pitched_notes([event], "1", None, 1, Fraction(1), Fraction(4), origin=(50.0, 2.0))
    assert note.findtext("notehead") == "x"
    assert note.get("default-x") == "100"                       # 20 points, 10 spaces, from the bar line


def sung(where: str | None, text: str | None = None) -> ET.Element:
    note = ET.fromstring("<note><pitch><step>C</step><octave>5</octave></pitch><duration>1</duration></note>")
    if where is not None:
        note.set("default-x", where)
    if text:
        ET.SubElement(ET.SubElement(note, "lyric"), "text").text = text
    return note


def old_lyrics(notes: list[ET.Element]) -> list:
    return [(n.get("default-x"), n.findall("lyric")) for n in notes]


def test_lyrics_follow_their_notes_place_when_a_note_is_put_back():
    # The reader lost a note at the start of the bar: by count, every word
    # would slide one note left; by place, each stays on its note.
    old = [sung("100", "un"), sung("140", "der"), sung("180", "the")]
    new = [sung("16"), sung("103"), sung("142"), sung("182")]
    drawing_check._carry_to_new_notes(old_lyrics(old), new)
    assert [n.findtext("lyric/text") for n in new] == [None, "un", "der", "the"]


def test_lyrics_carried_one_to_one_without_places():
    old = [sung(None, "un"), sung(None, "der")]
    new = [sung(None), sung(None)]
    drawing_check._carry_to_new_notes(old_lyrics(old), new)
    assert [n.findtext("lyric/text") for n in new] == ["un", "der"]


def test_slurs_and_glissandi_follow_their_notes_but_not_the_marks_the_drawing_writes():
    first, second = sung("100"), sung("140")
    notations = ET.SubElement(first, "notations")
    ET.SubElement(notations, "slur", {"type": "start", "number": "1"})
    ET.SubElement(notations, "glissando", {"type": "start"})
    ET.SubElement(ET.SubElement(notations, "articulations"), "accent")     # the drawing's to set
    ET.SubElement(ET.SubElement(second, "notations"), "slur", {"type": "stop", "number": "1"})
    new = [sung("101"), sung("141")]
    drawing_check._carry_to_new_notes([(n.get("default-x"), drawing_check._carried(n)) for n in (first, second)], new)
    assert [[c.tag for c in n.find("notations")] for n in new] == [["slur", "glissando"], ["slur"]]
    assert [s.get("type") for n in new for s in n.iter("slur")] == ["start", "stop"]


def test_divisions_restated_in_a_bar_count_from_the_last():
    measure = ET.fromstring('<measure><attributes><divisions>8</divisions></attributes>'
                            '<attributes><divisions>2</divisions></attributes><note/></measure>')
    assert repair._divisions(measure, {}) == 2
