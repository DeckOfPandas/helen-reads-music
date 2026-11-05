"""tools/omr/pagefix.py: a page read with the wrong number of bars, put right."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

import pagefix  # noqa: E402
import score  # noqa: E402
from musicxml_maker import attributes, bar, four_crotchets, part, score as make_score  # noqa: E402

HIDE = '<attributes><staff-details print-object="no"/></attributes>'
SHOW = '<attributes><staff-details print-object="yes"/></attributes>'


def _new_system(measure: str) -> str:
    head, rest = measure.split(">", 1)
    return f'{head}><print new-system="yes"/>{rest}'


def _with(measure: str, extra: str) -> str:
    head, rest = measure.split(">", 1)
    return f"{head}>{extra}{rest}"


def _lengths(root):
    return [[float(n) if n else None for n in row] for row in pagefix._lengths(root)]


def _sounding(root):
    return sum(score.count_part_notes(p) for p in root.findall("part"))


def test_a_bar_holding_two_is_split_and_a_crossing_note_tied():
    double = [("C4", 1), ("D4", 1), ("E4", 1), ("F4", 2), ("G4", 1), ("A4", 1), ("B4", 1)]
    root = score.ET.fromstring(make_score([("P1", "Flute", part("P1", [
        bar(four_crotchets(), 1, attributes()), bar(double, 2), bar(four_crotchets(), 3)]))]))
    notes = pagefix.fix_page(root, 4)
    assert notes == ["system 1: bar 2 held two bars (median 8 crotchets): split"]
    measures = root.find("part").findall("measure")
    assert [m.get("number") for m in measures] == ["1", "2", "3", "4"]
    assert _lengths(root) == [[4.0, 4.0, 4.0, 4.0]]
    end, start = measures[1].findall("note")[-1], measures[2].findall("note")[0]
    assert end.findtext("pitch/step") == start.findtext("pitch/step") == "F"
    assert end.find("tie").get("type") == "start" and start.find("tie").get("type") == "stop"
    assert end.findtext("type") == start.findtext("type") == "quarter"
    assert score.bad_bars(root.find("part")) == []


def test_a_bar_with_nothing_sounding_is_dropped():
    root = score.ET.fromstring(make_score([("P1", "Flute", part("P1", [
        bar(four_crotchets(), 1, attributes()), bar(four_crotchets(), 2), bar([("r", 1)], 3)]))]))
    assert pagefix.fix_page(root, 2) == ["system 1: bar 3 had nothing sounding: dropped into bar 2"]
    assert _lengths(root) == [[4.0, 4.0]]
    assert _sounding(root) == 8


def test_systems_read_one_after_another_are_folded():
    winds = part("P1", [bar(four_crotchets("C5"), 1, attributes()), bar(four_crotchets("D5"), 2),
                        _new_system(_with(bar([("r", 4)], 3), HIDE)), bar([("r", 4)], 4)])
    strings = part("P2", [_with(bar([("r", 4)], 1, attributes()), HIDE), bar([("r", 4)], 2),
                          _new_system(_with(bar(four_crotchets("E4"), 3), SHOW)), bar(four_crotchets("F4"), 4)])
    root = score.ET.fromstring(make_score([("P1", "Flute", winds), ("P2", "Violin", strings)]))
    assert pagefix.fix_page(root, 2) == ["2 systems folded into one"]
    first, second = root.findall("part")
    assert [n.findtext("pitch/step") for n in first.iter("note")] == ["C"] * 4 + ["D"] * 4
    assert [n.findtext("pitch/step") for n in second.iter("note")] == ["E"] * 4 + ["F"] * 4
    assert second.find("measure/attributes/divisions") is not None      # restated from its system
    assert [m.get("number") for m in second.findall("measure")] == ["1", "2"]


def test_a_part_playing_in_two_systems_becomes_two_parts():
    both = part("P1", [bar(four_crotchets("C5"), 1, attributes()), bar(four_crotchets("D5"), 2),
                       _new_system(bar(four_crotchets("G3"), 3)), bar(four_crotchets("A3"), 4)])
    root = score.ET.fromstring(make_score([("P1", "Voice", both)]))
    assert pagefix.fix_page(root, 2) == ["1 part(s) with notes in two systems separated",
                                         "2 systems folded into one"]
    assert [pid for pid, _ in score.part_names(root)] == ["P1", "P1-s2"]
    upper, lower = root.findall("part")
    assert [n.findtext("pitch/step") for n in upper.iter("note")] == ["C"] * 4 + ["D"] * 4
    assert [n.findtext("pitch/step") for n in lower.iter("note")] == ["G"] * 4 + ["A"] * 4


def test_a_page_it_cannot_put_right_raises():
    root = score.ET.fromstring(make_score([("P1", "Flute", part("P1", [
        bar(four_crotchets(), 1, attributes()), bar(four_crotchets(), 2)]))]))
    with pytest.raises(ValueError, match="system 1 has 2 bars, printed 4"):
        pagefix.fix_page(root, 4)
