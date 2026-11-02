"""tools/omr/combine.py: matching a second read and filling bad bars from it."""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

import combine  # noqa: E402
import score  # noqa: E402
from musicxml_maker import (attributes, bar, four_crotchets, part,  # noqa: E402
                            score as make_score, whole_rest)

RIFF = [("D4", 0.5), ("F4", 0.5), ("B4", 0.5), ("D5", 0.5), ("r", 0.5), ("D5", 0.5), ("r", 0.5), ("F4", 0.5)]
RIFF_MISSING_RESTS = [e for e in RIFF if e[0] != "r"]


def merged(parts):
    return ET.fromstring(make_score(parts))


def test_align_matches_by_notes_even_an_octave_out():
    primary = merged([("M1", "Oboe", part("M1", [bar(RIFF, 1, attributes())])),
                      ("M2", "Bassoon", part("M2", [bar(four_crotchets("F2"), 1, attributes(clef="F", line=4))]))])
    octave_low = [(p[0] + str(int(p[1:]) - 1) if p != "r" else p, d) for p, d in
                  ((p if p == "r" else p, d) for p, d in RIFF)]
    second = merged([("P7", "01).", part("P7", [bar(octave_low, 1, attributes())]))])
    mapping, report = combine.align(second, primary, {})
    assert mapping == {"P7": "Oboe"}
    assert "P7 '01).' -> Oboe (100% of its notes match)" in report[0]


def test_unison_ties_go_by_name_then_clef():
    viola_clef = attributes(clef="C", line=3)
    cello_clef = attributes(clef="F", line=4)
    line = four_crotchets("A3")
    primary = merged([
        ("M1", "Violin I", part("M1", [bar(line, 1, attributes())])),
        ("M2", "Violin II", part("M2", [bar(line, 1, attributes())])),
        ("M3", "Viola", part("M3", [bar(line, 1, viola_clef)])),
        ("M4", "Violoncello", part("M4", [bar(line, 1, cello_clef)]))])
    second = merged([
        ("P1", "V111. II", part("P1", [bar(line, 1, attributes())])),
        ("P2", "Voice", part("P2", [bar(line, 1, cello_clef)])),
        ("P3", "Voice", part("P3", [bar(line, 1, attributes())]))])
    mapping, report = combine.align(second, primary, {"V111. II": "Violin II"})
    assert mapping == {"P1": "Violin II", "P2": "Violoncello"}
    assert any("P3 'Voice': ties between" in line for line in report)


import pytest  # noqa: E402


@pytest.mark.parametrize("fragment,instrument,agrees", [
    ("Picc.", "Piccolo", True), ("Picc.", "Oboe", False),
    ("Tpt.", "Trumpet in Bb", True), ("Trumpet in BL", "Trumpet in Bb", True),
    ("Clarinet in m", "Clarinet in Bb", True), ("Clarinet in m", "Trumpet in Bb", False),
    ("V111. II", "Violin II", True), ("V111. II", "Violin I", False), ("VIn. II", "Violin II", True),
    ("E. Bass", "Electric Bass", True), ("E Bass", "Electric Bass", True), ("E. Bass", "Contrabass", False),
    ("Cb.", "Contrabass", True), ("Bsn.", "Bassoon", True), ("Bsn.", "Electric Bass", False),
    ("Pno.", "Piano", True), ("Pno.", "Harp", False), ("Voice", "Violoncello", False),
])
def test_names_agree(fragment, instrument, agrees):
    assert combine.names_agree(fragment, instrument) is agrees


def test_doubled_instruments_are_told_apart_by_abbreviation():
    line = four_crotchets("D5")
    primary = merged([("M1", "Piccolo", part("M1", [bar(line, 1, attributes())])),
                      ("M2", "Oboe", part("M2", [bar(line, 1, attributes())]))])
    second = merged([("P1", "Ob.", part("P1", [bar(line, 1, attributes())])),
                     ("P2", "Picc.", part("P2", [bar(line, 1, attributes())]))])
    mapping, _ = combine.align(second, primary, {})
    assert mapping == {"P1": "Oboe", "P2": "Piccolo"}


def test_drums_match_by_stave_position():
    drums = ('<measure number="1">' + attributes() + "".join(
        f"<note><unpitched><display-step>{s}</display-step><display-octave>{o}</display-octave></unpitched>"
        f"<duration>4</duration><voice>1</voice></note>" for s, o in (("F", 4), ("C", 5), ("F", 4), ("C", 5)))
        + "</measure>")
    primary = merged([("M1", "Drum Set", f'<part id="M1">{drums}</part>')])
    second = merged([("P1", "Dr.", f'<part id="P1">{drums}</part>')])
    mapping, _ = combine.align(second, primary, {})
    assert mapping == {"P1": "Drum Set"}


def test_a_silent_bar_from_an_unmatched_fragment_never_replaces_notes():
    # Our bar 1 is short but has notes; the second read's is a whole rest
    # because its fragment was never matched.
    primary = merged([("M1", "Violin I", part("M1", [bar([("C5", 1)] * 3, 1, attributes())]))])
    second = merged([("M1", "Violin I", part("M1", [bar(whole_rest(), 1, attributes())]))])
    report = combine.fill(primary, second)
    assert report[0] == "bars taken from the second read: 0"
    assert "DISAGREE" not in report[1]
    assert score.count_notes(primary.find("part/measure")) == 3


def test_a_fragment_matching_nothing_is_left_out():
    primary = merged([("M1", "Oboe", part("M1", [bar(four_crotchets("C5"), 1, attributes())]))])
    second = merged([("P1", "Oboe", part("P1", [bar(four_crotchets("F#4"), 1, attributes())]))])
    mapping, report = combine.align(second, primary, {})
    assert mapping == {} and "matches nothing" in report[0]


def test_fill_takes_only_bars_the_second_read_got_right():
    good = [bar(RIFF, 1, attributes()), bar(four_crotchets(), 2), bar([("C4", 1)] * 3, 3)]
    primary = merged([("M1", "Oboe", part("M1", [bar(RIFF_MISSING_RESTS, 1, attributes()),
                                                 bar(four_crotchets(), 2), bar([("C4", 1)] * 3, 3)]))])
    second = merged([("M1", "Oboe", part("M1", good))])
    report = combine.fill(primary, second)
    assert report[0] == "bars taken from the second read: 1"
    assert "took bar(s) 1 from the second read; neither read adds up in bar(s) 3" in report[1]
    oboe = primary.find("part")
    assert [b.number for b in score.bad_bars(oboe)] == ["3"]
    assert len(oboe.findall("measure/note")) == 8 + 4 + 3


def test_a_bar_from_the_second_read_brings_its_divisions_and_the_next_gets_ours_back():
    primary = merged([("M1", "Oboe", part("M1", [bar([("C4", 1)] * 3, 1, attributes()),
                                                 bar(four_crotchets(), 2)]))])
    second = merged([("M1", "Oboe", part("M1", [bar(four_crotchets(), 1, attributes(divisions=2), divisions=2),
                                                bar(whole_rest(), 2, divisions=2)]))])
    combine.fill(primary, second)
    bars = primary.find("part").findall("measure")
    assert bars[0].findtext("attributes/divisions") == "2"
    assert bars[1].findtext("attributes/divisions") == "4"
    assert score.bad_bars(primary.find("part")) == []


def test_bars_both_reads_got_right_but_differently_are_listed():
    primary = merged([("M1", "Oboe", part("M1", [bar(four_crotchets("C5"), 1, attributes())]))])
    second = merged([("M1", "Oboe", part("M1", [bar(four_crotchets("D5"), 1, attributes())]))])
    report = combine.fill(primary, second)
    assert "reads DISAGREE in bar(s) 1" in report[1]


def test_the_same_rhythm_at_different_divisions_agrees():
    primary = merged([("M1", "Oboe", part("M1", [bar(four_crotchets("C5"), 1, attributes()),
                                                 bar(four_crotchets("D5"), 2)]))])
    second = merged([("M1", "Oboe", part("M1", [
        bar(four_crotchets("C5"), 1, attributes(divisions=8), divisions=8),
        bar(four_crotchets("D5"), 2, divisions=8)]))])
    assert "DISAGREE" not in combine.fill(primary, second)[1]


def test_a_same_named_leftover_fragment_does_not_stand_in_for_the_instrument():
    primary = merged([("M1", "Oboe", part("M1", [bar([("C4", 1)] * 3, 1, attributes())]))])
    second = merged([("M1", "Oboe", part("M1", [bar(four_crotchets(), 1, attributes())])),
                     ("P9", "Oboe", part("P9", [bar([("C4", 1)] * 2, 1, attributes())]))])
    assert combine.fill(primary, second)[0] == "bars taken from the second read: 1"
