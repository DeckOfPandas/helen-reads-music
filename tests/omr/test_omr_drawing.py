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
    drawing_check._carry_lyrics(old_lyrics(old), new)
    assert [n.findtext("lyric/text") for n in new] == [None, "un", "der", "the"]


def test_lyrics_carried_one_to_one_without_places():
    old = [sung(None, "un"), sung(None, "der")]
    new = [sung(None), sung(None)]
    drawing_check._carry_lyrics(old_lyrics(old), new)
    assert [n.findtext("lyric/text") for n in new] == ["un", "der"]


def test_divisions_restated_in_a_bar_count_from_the_last():
    measure = ET.fromstring('<measure><attributes><divisions>8</divisions></attributes>'
                            '<attributes><divisions>2</divisions></attributes><note/></measure>')
    assert repair._divisions(measure, {}) == 2
