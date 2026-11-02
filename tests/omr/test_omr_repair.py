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
from musicxml_maker import (bar, four_crotchets, part,  # noqa: E402
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
