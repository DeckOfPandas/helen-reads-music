"""tools/omr/textlayer.py: lyrics, chord symbols and singers' names from a PDF's text layer."""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

pymupdf = pytest.importorskip("pymupdf")

import textlayer  # noqa: E402
from vector_maker import Engraver  # noqa: E402

BARS = [50.0, 140.0, 230.0]
VOICE_XS = [65.0, 85.0, 105.0, 125.0]          # bar 1: four crotchets; bar 2: two minims
MINIM_XS = [155.0, 195.0]
HEAD = 3.5


def engrave(tmp_path: Path) -> tuple[Path, Path]:
    """One system: a voice stave over a grand stave, two bars, with words.

    Voice  | C5 C5 C5 C5 crotchets | C5 C5 minims |
           lyrics "Kil-ler a whale." | "here___" (an extender over both minims)
    above  "MAN 1:", chords C (beat 1), G7/B (beat 3) | N.C. (beat 1); "Tempo" (not a chord)
    Piano  B4 whole notes over C3 whole notes.
    """
    e = Engraver()
    voice = e.stave(100.0, BARS)
    right = e.stave(150.0, BARS)
    left = e.stave(190.0, BARS)
    e.join(voice, left)
    for stave, kind in ((voice, "treble"), (right, "treble"), (left, "bass")):
        e.clef(stave, kind)
    for x in VOICE_XS:
        e.note(voice, x, 5, stem="down")
    for x in MINIM_XS:
        e.note(voice, x, 5, kind="half", stem="down")
    for x in (90.0, 180.0):
        e.head(right, x, 4, "whole")
        e.head(left, x, 3, "whole")
    baseline = voice.bottom + 10.0
    rights = [e.text(x + HEAD / 2, baseline, syllable, centre=True)
              for x, syllable in zip(VOICE_XS, ("Kil", "ler", "a", "whale."))]
    e.line(rights[0] + 1.0, rights[0] + 4.0, baseline - 2.4)                       # Kil-ler
    here_right = e.text(MINIM_XS[0] + HEAD / 2, baseline, "here", centre=True)
    e.line(here_right + 0.5, MINIM_XS[1] + 8.0, baseline + 0.5)                   # here___
    e.text(VOICE_XS[0], voice.top - 8.0, "C")
    e.text(VOICE_XS[2], voice.top - 8.0, "G7/B")
    e.text(MINIM_XS[0], voice.top - 8.0, "N.C.")
    e.text(VOICE_XS[0], voice.top - 18.0, "MAN 1:")
    e.text(180.0, voice.top - 18.0, "Tempo")
    e.text(160.0, right.bottom + 12.0, "cresc.")                                  # between the piano staves
    piece = tmp_path / "piece"
    piece.mkdir()
    pdf = e.save(piece / "song.pdf")
    e.catalogue(pdf, piece)
    return piece, pdf


def read_score() -> ET.Element:
    """The score as read: right notes; a misread lyric, chord and word direction to be dropped."""
    first = ("<attributes><divisions>4</divisions><key><fifths>0</fifths></key>"
             "<time><beats>4</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef>"
             "</attributes>")
    note = "<note><pitch><step>C</step><octave>5</octave></pitch><duration>{d}</duration><voice>1</voice>{extra}</note>"
    junk = ("<direction><direction-type><words>X 00</words></direction-type></direction>"
            "<direction><direction-type><words>MAN 1:</words></direction-type></direction>"
            "<direction><direction-type><dynamics><f/></dynamics></direction-type></direction>")
    voice = (f'<part id="P1"><measure number="1">{first}{junk}'
             + note.format(d=4, extra="<lyric><text>Kxl</text></lyric>") + note.format(d=4, extra="") * 3
             + '</measure><measure number="2">' + note.format(d=8, extra="") * 2 + "</measure></part>")
    piano_first = first.replace("<clef>", "<staves>2</staves><clef number=\"1\">").replace(
        "</attributes>", '<clef number="2"><sign>F</sign><line>4</line></clef></attributes>')
    whole = ("<note><pitch><step>B</step><octave>4</octave></pitch><duration>16</duration><voice>1</voice>"
             "<staff>1</staff></note><backup><duration>16</duration></backup>"
             "<note><pitch><step>C</step><octave>3</octave></pitch><duration>16</duration><voice>5</voice>"
             "<staff>2</staff></note>")
    misread_chord = "<harmony><root><root-step>F</root-step></root><kind>dominant</kind></harmony>"
    cresc = "<direction><direction-type><words>cresc.</words></direction-type></direction>"
    piano = (f'<part id="P2"><measure number="1">{piano_first}{misread_chord}{whole}</measure>'
             f'<measure number="2">{cresc}{whole}</measure></part>')
    listing = "".join(f'<score-part id="{p}"><part-name>{n}</part-name></score-part>'
                      for p, n in (("P1", "Voice"), ("P2", "Piano")))
    return ET.fromstring(f'<score-partwise version="4.0"><part-list>{listing}</part-list>{voice}{piano}</score-partwise>')


@pytest.fixture
def placed(tmp_path):
    piece, pdf = engrave(tmp_path)
    root = read_score()
    report = textlayer.place(root, piece, [pdf])
    return root, report


def bar_of(root: ET.Element, pid: str, number: str) -> ET.Element:
    part = next(p for p in root.findall("part") if p.get("id") == pid)
    return next(m for m in part.findall("measure") if m.get("number") == number)


def test_lyrics_go_on_the_notes_they_are_printed_under_with_their_hyphens(placed):
    root, report = placed
    lyrics = [[(l.findtext("syllabic"), l.findtext("text"), l.find("extend") is not None)
               for l in n.findall("lyric")] for n in bar_of(root, "P1", "1").findall("note")]
    assert lyrics == [[("begin", "Kil", False)], [("end", "ler", False)], [("single", "a", False)],
                      [("single", "whale.", False)]]
    minims = bar_of(root, "P1", "2").findall("note")
    assert [[(l.findtext("text"), l.find("extend") is not None) for l in n.findall("lyric")] for n in minims] == [
        [("here", True)], []]
    assert any(line.startswith("text layer: 5 lyric syllable(s) written") for line in report)


def test_chord_symbols_go_at_their_beat_and_replace_the_read_ones(placed):
    root, _ = placed
    bar1 = bar_of(root, "P1", "1")
    children = list(bar1)
    chords = bar1.findall("harmony")
    assert [(h.findtext("root/root-step"), h.findtext("kind"), h.findtext("bass/bass-step")) for h in chords] == [
        ("C", "major", None), ("G", "dominant", "B")]
    notes = bar1.findall("note")
    assert children.index(chords[1]) == children.index(notes[2]) - 1           # just before beat 3's note
    nc = bar_of(root, "P1", "2").find("harmony")
    assert nc.findtext("kind") == "none" and nc.find("kind").get("text") == "N.C."
    assert bar_of(root, "P2", "1").find("harmony") is None                   # the misread F7 is gone


def test_a_singers_name_is_written_above_the_voice_and_other_text_reported(placed):
    root, report = placed
    # The read's "X 00" and its own "MAN 1:" go; the page's name is written once.
    assert [w.text for w in bar_of(root, "P1", "1").iter("words")] == ["MAN 1:"]
    assert [w.text for w in bar_of(root, "P2", "2").iter("words")] == ["cresc."]       # printed there: kept
    assert bar_of(root, "P1", "1").find("direction/direction-type/dynamics/f") is not None   # drawn, kept
    assert "text layer: 1 singer name(s): MAN 1: (bar 1)" in report
    assert any("'Tempo'" in line for line in report)


@pytest.mark.parametrize("text, kind, alter, bass, degrees", [
    ("F#5", "power", 1, None, []),
    ("G/F♯", "major", 0, ("F", 1), []),
    ("F♯7sus4", "suspended-fourth", 1, None, [("add", 7, 0)]),
    ("Gmaj7♭5", "major-seventh", 0, None, [("alter", 5, -1)]),
    ("C♯m7♭5", "half-diminished", 1, None, []),
    ("B♭m6", "minor-sixth", -1, None, []),
    ("B7♯9", "dominant", 0, None, [("add", 9, 1)]),
    ("A♯dim", "diminished", 1, None, []),
    ("C5/B", "power", 0, ("B", 0), []),
])
def test_chord_symbols_parse(text, kind, alter, bass, degrees):
    chord = textlayer.parse_chord(text)
    assert (chord["kind"], chord["alter"], chord["bass"], chord["degrees"]) == (kind, alter, bass, degrees)


@pytest.mark.parametrize("text", ["GASTON", "MAN", "Moderately", "Hm7", "C7zz"])
def test_words_that_are_not_chords(text):
    assert textlayer.parse_chord(text) is None
