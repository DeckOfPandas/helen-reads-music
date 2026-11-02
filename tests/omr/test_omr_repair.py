"""tools/omr/repair.py: time signature, transposition, whole-bar rests."""
from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

import repair  # noqa: E402
import score  # noqa: E402
from musicxml_maker import (attributes, bar, four_crotchets, part,  # noqa: E402
                            score as make_score, write_mxl)


def untimed(fifths: int = -2, staves: int = 1) -> str:
    """First-bar attributes the way Audiveris writes them when it misses the time."""
    extra = f"<staves>{staves}</staves>" if staves > 1 else ""
    return (f"<attributes><divisions>4</divisions><key><fifths>{fifths}</fifths></key>{extra}"
            "<clef><sign>G</sign><line>2</line></clef></attributes>")


def measure_rest(ticks: int, staff: int | None = None) -> str:
    staff_xml = f"<staff>{staff}</staff>" if staff else ""
    return (f'<note><rest measure="yes"><display-step>D</display-step><display-octave>5</display-octave>'
            f"</rest><duration>{ticks}</duration><voice>1</voice>{staff_xml}</note>")


def rest_bar(number: int, ticks: int, attributes: str = "", staff: int | None = None) -> str:
    return f'<measure number="{number}">{attributes}{measure_rest(ticks, staff)}</measure>'


def build(parts):
    return ET.fromstring(make_score(parts))


def test_time_is_written_once_in_order():
    root = build([("P1", "Flute", part("P1", [bar(four_crotchets(), 1, untimed())]))])
    report = repair.repair(root, {"time": "4/4"})
    attributes = root.find("part/measure/attributes")
    assert [c.tag for c in attributes] == ["divisions", "key", "time", "clef"]
    assert report[0] == "time 4/4 written into 1 part(s)"
    assert repair.repair(root, {"time": "4/4"})[0] == "time 4/4 written into 0 part(s)"


def test_a_whole_bar_rest_takes_the_bar_length():
    # 18 ticks = 4.5 crotchets: the length Audiveris inferred from another staff.
    root = build([("P1", "Bsn.", part("P1", [rest_bar(1, 18, untimed())]))])
    report = repair.repair(root, {"time": "4/4"})
    assert root.find("part/measure/note/duration").text == "16"
    assert report[-1] == "whole-bar rests: 1 set to a full bar, 0 added to empty staves"
    assert score.bad_bars(root.find("part")) == []


def test_an_empty_lower_staff_gets_its_rest():
    root = build([("P1", "Piano", part("P1", [rest_bar(1, 16, untimed(staves=2), staff=1)]))])
    report = repair.repair(root, {"time": "4/4"})
    measure = root.find("part/measure")
    assert [e.tag for e in measure if e.tag in ("note", "backup")] == ["note", "backup", "note"]
    lower = measure.findall("note")[1]
    assert lower.findtext("staff") == "2" and lower.findtext("duration") == "16"
    assert lower.find("rest").get("measure") == "yes" and lower.findtext("voice") == "5"
    assert lower.find("rest/display-step") is None
    assert report[-1] == "whole-bar rests: 0 set to a full bar, 1 added to empty staves"
    assert score.bad_bars(root.find("part")) == []


def test_bars_with_notes_are_never_touched():
    short = bar([("C4", 1)] * 3, 1, untimed())
    root = build([("P1", "Flute", part("P1", [short]))])
    before = [n.findtext("duration") for n in root.iter("note")]
    repair.repair(root, {"time": "4/4"})
    assert [n.findtext("duration") for n in root.iter("note")] == before
    assert [b.number for b in score.bad_bars(root.find("part"))] == ["1"]


def test_rests_are_left_alone_without_a_time_signature():
    root = build([("P1", "Bsn.", part("P1", [rest_bar(1, 18, untimed())]))])
    repair.repair(root, {})
    assert root.find("part/measure/note/duration").text == "18"


@pytest.mark.parametrize("kind,diatonic,chromatic,octave,fifths", [
    ("Bb", "-1", "-2", None, "0"),
    ("F", "-4", "-7", None, "-1"),
    ("Eb", "-5", "-9", None, "1"),
    ("8va", "0", "0", "1", "-2"),
])
def test_transposition_and_written_key(kind, diatonic, chromatic, octave, fifths):
    # Concert B-flat major (-2) on the flute and oboe; Audiveris stamped it on
    # the transposing part too.
    parts = [(pid, name, part(pid, [bar(four_crotchets(), 1, untimed(-2))]))
             for pid, name in (("P1", "Flute"), ("P2", "Oboe"), ("P3", "Clarinet"))]
    root = build(parts)
    repair.repair(root, {"transpose": {"Clarinet": kind}})
    clarinet = root.findall("part")[2]
    transpose = clarinet.find("measure/attributes/transpose")
    assert transpose.findtext("diatonic") == diatonic and transpose.findtext("chromatic") == chromatic
    assert transpose.findtext("octave-change") == octave
    assert clarinet.findtext("measure/attributes/key/fifths") == fifths
    assert root.findall("part")[0].findtext("measure/attributes/key/fifths") == "-2"


def test_a_key_already_written_is_not_moved_twice():
    parts = [("P1", "Flute", part("P1", [bar(four_crotchets(), 1, untimed(-2))])),
             ("P2", "Horn", part("P2", [bar(four_crotchets(), 1, untimed(-1))]))]
    root = build(parts)
    repair.repair(root, {"transpose": {"Horn": "F"}})
    assert root.findall("part")[1].findtext("measure/attributes/key/fifths") == "-1"


def octave_low(clef_octave: str = "<clef-octave-change>-1</clef-octave-change>") -> str:
    return ("<attributes><divisions>4</divisions><key><fifths>0</fifths></key>"
            "<time><beats>4</beats><beat-type>4</beat-type></time>"
            f"<clef><sign>G</sign><line>2</line>{clef_octave}</clef></attributes>")


def test_a_false_octave_clef_is_removed_and_its_notes_moved_back():
    # The oboe's opening D4 read as D3 under a "treble 8 below" clef.
    plain_clef = "<attributes><clef><sign>G</sign><line>2</line></clef></attributes>"
    bars = [bar([("D3", 1), ("F3", 1), ("r", 2)], 1, octave_low()),
            bar(four_crotchets("D3"), 2),
            bar(four_crotchets("D4"), 3, plain_clef)]
    root = build([("P1", "Oboe", part("P1", bars))])
    assert score.below_range(root.find("part"), "Oboe") == ["1", "2"]
    report = repair.repair(root, {"octave_clefs": False})
    # Bars 1-2 move (2 + 4 notes); bar 3's plain clef ends the shift.
    assert report[0] == "octave clefs: 1 '8 below/above' marking(s) removed, 6 note(s) moved back"
    octaves = [n.findtext("pitch/octave") for n in root.iter("note") if n.find("pitch") is not None]
    assert octaves == ["4", "4"] + ["4"] * 4 + ["4"] * 4
    assert root.find(".//clef-octave-change") is None
    assert score.below_range(root.find("part"), "Oboe") == []


def test_octave_clefs_are_kept_unless_the_table_says_there_are_none():
    root = build([("P1", "Tenor", part("P1", [bar(four_crotchets("D3"), 1, octave_low())]))])
    repair.repair(root, {})
    assert root.find(".//clef-octave-change") is not None


def test_range_check_names_the_bars():
    bars = [bar(four_crotchets("G3"), 1, octave_low("")), bar(four_crotchets("F3"), 2)]
    root = build([("P1", "Violin II", part("P1", bars)), ("P2", "Marimba", part("P2", bars))])
    first, second = root.findall("part")
    assert score.below_range(first, "Violin II") == ["2"]
    assert score.below_range(second, "Marimba") == []       # not range-checked
    assert score.lowest_written("Contrabass") == 40 and score.lowest_written("Violoncello") == 36


OBOE_RIFF = [("C4", 0.5), ("E4", 0.5), ("G4", 0.5), ("C5", 0.5), ("r", 0.5), ("C5", 0.5), ("r", 0.5), ("E4", 0.5)]
# The same figure a fifth up (the helper writes naturals only), first rest dropped.
TRUMPET_SHORT = [("G4", 0.5), ("B4", 0.5), ("D5", 0.5), ("G5", 0.5), ("G5", 0.5), ("r", 0.5), ("B4", 0.5)]


def _events(part_element):
    out = []
    for n in part_element.iter("note"):
        out.append("r" if n.find("rest") is not None else n.findtext("pitch/step") + n.findtext("pitch/octave"))
    return out


def test_a_short_bar_gets_the_rests_of_a_matching_bar_in_another_part():
    root = build([("P1", "Oboe", part("P1", [bar(OBOE_RIFF, 1, attributes())])),
                  ("P2", "Trumpet", part("P2", [bar(TRUMPET_SHORT, 1, attributes())]))])
    fixed = repair.borrow_rests(root)
    assert fixed == ["Trumpet bar 1: 1 rest(s) put back, as in Oboe bar 1"]
    trumpet = root.findall("part")[1]
    assert _events(trumpet) == ["G4", "B4", "D5", "G5", "r", "G5", "r", "B4"]
    assert score.bad_bars(trumpet) == []
    added = [n for n in trumpet.iter("note") if n.find("rest") is not None][0]
    assert [c.tag for c in added] == ["rest", "duration", "voice", "type"] and added.findtext("type") == "eighth"


def test_no_rests_are_borrowed_when_the_notes_differ():
    other = [("G4", 0.5), ("C5", 0.5), ("D5", 0.5), ("G5", 0.5), ("G5", 0.5), ("r", 0.5), ("B4", 0.5)]
    root = build([("P1", "Oboe", part("P1", [bar(OBOE_RIFF, 1, attributes())])),
                  ("P2", "Trumpet", part("P2", [bar(other, 1, attributes())]))])
    assert repair.borrow_rests(root) == []


def test_no_rests_are_borrowed_when_matching_bars_disagree_where():
    # Same notes as the oboe's, rests in different places: which to copy is unknown.
    elsewhere = [("C4", 0.5), ("r", 0.5), ("E4", 0.5), ("G4", 0.5), ("C5", 0.5), ("C5", 0.5), ("r", 0.5), ("E4", 0.5)]
    root = build([("P1", "Oboe", part("P1", [bar(OBOE_RIFF, 1, attributes()), bar(elsewhere, 2)])),
                  ("P2", "Trumpet", part("P2", [bar(TRUMPET_SHORT, 1, attributes())]))])
    assert repair.borrow_rests(root) == []
    assert _events(root.findall("part")[1]).count("r") == 1


@pytest.mark.parametrize("config", [{"transpose": {"Nobody": "Bb"}}, {"transpose": {"Flute": "H"}},
                                    {"time": "four"}])
def test_bad_config_is_refused(config):
    root = build([("P1", "Flute", part("P1", [bar(four_crotchets(), 1, untimed())]))])
    with pytest.raises(ValueError):
        repair.repair(root, config)


def test_cli(tmp_path):
    source = write_mxl(tmp_path / "in.mxl", make_score([("P1", "Bsn.", part("P1", [rest_bar(1, 18, untimed())]))]))
    config = tmp_path / "merge.json"
    config.write_text(json.dumps({"time": "4/4"}))
    out = tmp_path / "out.mxl"
    assert repair.main([str(source), str(out), "--config", str(config)]) == 0
    assert score.load(out).find("part/measure/note/duration").text == "16"
