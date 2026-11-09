"""tools/omr/merge.py: rejoining the parts Audiveris split one instrument into."""
from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

import merge  # noqa: E402
import score  # noqa: E402
from musicxml_maker import (attributes, bar, four_crotchets, part,  # noqa: E402
                            score as make_score, whole_rest, write_mxl)


def fragmented(second_divisions: int = 4) -> ET.Element:
    """Flute read as "Fl." (bars 1-2) and "Flute" (bars 3-4), plus an Oboe."""
    fl = part("P1", [bar(four_crotchets("C5"), 1, attributes()), bar(four_crotchets("D5"), 2),
                     bar(whole_rest(), 3), bar(whole_rest(), 4)])
    flute = part("P2", [
        bar(whole_rest(), 1, attributes(divisions=second_divisions), divisions=second_divisions),
        bar(whole_rest(), 2, divisions=second_divisions),
        bar(four_crotchets("E5"), 3, divisions=second_divisions),
        bar(four_crotchets("F5"), 4, divisions=second_divisions)])
    oboe = part("P3", [bar(four_crotchets("A4"), n, attributes() if n == 1 else "") for n in (1, 2, 3, 4)])
    return ET.fromstring(make_score([("P1", "Fl.", fl), ("P2", "Flute", flute), ("P3", "Ob.", oboe)]))


def _pitches(part_element):
    return [n.findtext("pitch/step") + n.findtext("pitch/octave")
            for n in part_element.iter("note") if n.find("rest") is None]


def test_fragments_become_one_part_in_order():
    root = fragmented()
    ok, report = merge.merge(root, ["Flute", "Oboe"], {"Fl.": "Flute", "Flute": "Flute", "Ob.": "Oboe"})
    assert ok, report
    assert [name for _, name in score.part_names(root)] == ["Flute", "Oboe"]
    flute = root.findall("part")[0]
    assert _pitches(flute) == ["C5"] * 4 + ["D5"] * 4 + ["E5"] * 4 + ["F5"] * 4
    assert report[-1] == "notes in 32, out 32: conserved"


def test_a_grand_stave_read_as_two_parts_is_stacked():
    upper = part("P1", [bar(four_crotchets("C5"), 1, attributes()), bar(four_crotchets("D5"), 2)])
    lower = part("P2", [bar([("C3", 2), ("G2", 2)], 1, attributes(clef="F", divisions=8), divisions=8),
                        bar(whole_rest(), 2, divisions=8)])
    root = ET.fromstring(make_score([("P1", "Piano", upper), ("P2", "Piano", lower)]))
    report = merge.stack_staves(root, [["P1", "P2"]])
    assert report == ["P2 put on P1 as its stave 2: 2 notes"]
    assert [pid for pid, _ in score.part_names(root)] == ["P1"]
    piano = root.find("part")
    first = piano.find("measure")
    assert first.findtext("attributes/staves") == "2"
    assert [(c.get("number"), c.findtext("sign")) for c in first.findall("attributes/clef")] == \
        [("1", "G"), ("2", "F")]
    low = [n for n in first.findall("note") if n.findtext("staff") == "2"]
    assert [(n.findtext("pitch/step"), n.findtext("duration"), n.findtext("voice")) for n in low] == \
        [("C", "8", "5"), ("G", "8", "5")]                 # durations in the upper part's divisions
    assert first.find("backup/duration").text == "16"
    assert score.bad_bars(piano) == []
    with pytest.raises(ValueError, match="already has more than one stave"):
        merge.stack_staves(root, [["P1", "P1"]])


def test_a_part_read_from_text_is_dropped(tmp_path):
    root = fragmented()
    assert merge.drop_parts(root, ["P3"]) == ['dropped P3: 16 notes (merge.json "drop")']
    assert [pid for pid, _ in score.part_names(root)] == ["P1", "P2"]
    with pytest.raises(ValueError, match="drop names no part 'P9'"):
        merge.drop_parts(root, ["P9"])


def test_a_bar_from_another_fragment_brings_its_divisions():
    root = fragmented(second_divisions=2)
    ok, report = merge.merge(root, ["Flute", "Oboe"], {"Fl.": "Flute", "Flute": "Flute", "Ob.": "Oboe"})
    assert ok, report
    flute = root.findall("part")[0]
    bar3 = flute.findall("measure")[2]
    assert bar3.findtext("attributes/divisions") == "2"
    assert score.bad_bars(flute) == []


def test_without_restating_the_bars_would_be_misread():
    # The defect restating fixes: P2's bars read against P1's divisions.
    root = fragmented(second_divisions=2)
    p1, p2 = root.findall("part")[:2]
    spliced = ET.Element("part")
    spliced.extend(p1.findall("measure")[:2] + p2.findall("measure")[2:])
    assert [b.number for b in score.bad_bars(spliced)] == ["3", "4"]


def test_a_clef_change_between_fragments_is_restated():
    treble = part("P1", [bar(four_crotchets(), 1, attributes()), bar(whole_rest(), 2)])
    bass = part("P2", [bar(whole_rest(), 1, attributes(clef="F", line=4)), bar(four_crotchets("C3"), 2)])
    root = ET.fromstring(make_score([("P1", "Vc.", treble), ("P2", "Cello", bass)]))
    ok, _ = merge.merge(root, ["Cello"], {"Vc.": "Cello", "Cello": "Cello"})
    assert ok
    assert root.find("part").findall("measure")[1].findtext("attributes/clef/sign") == "F"


def test_a_silent_bar_comes_from_a_fragment_that_wrote_its_rest():
    # P2 plays bar 1, then has nothing at all in bar 2; P1 wrote the rest.
    p1 = part("P1", [bar(whole_rest(), 1, attributes()), bar(whole_rest(), 2)])
    p2 = part("P2", [bar(four_crotchets(), 1, attributes()), '<measure number="2"></measure>'])
    root = ET.fromstring(make_score([("P1", "Cl.", p1), ("P2", "Clarinet", p2)]))
    ok, _ = merge.merge(root, ["Clarinet"], {"Cl.": "Clarinet", "Clarinet": "Clarinet"})
    assert ok
    bar2 = root.find("part").findall("measure")[1]
    assert bar2.find("note/rest") is not None
    assert score.bad_bars(root.find("part")) == []


def test_a_conflict_refuses_and_says_where():
    a = part("P1", [bar(four_crotchets(), 1, attributes())])
    b = part("P2", [bar(four_crotchets("E4"), 1, attributes())])
    root = ET.fromstring(make_score([("P1", "Voice", a), ("P2", "Voice", b)]))
    ok, report = merge.merge(root, ["Voice"], {"Voice": "Voice"})
    assert not ok
    assert any("Voice bar #1" in line for line in report)
    assert "MISMATCH" in report[-1]


def test_a_part_id_overrides_its_shared_name():
    # Two different staves both read as "Voice": the ids tell them apart.
    marimba = part("P1", [bar(four_crotchets("D4"), 1, attributes()), bar(whole_rest(), 2)])
    voice = part("P2", [bar(whole_rest(), 1, attributes()), bar(whole_rest(), 2)])
    marimba2 = part("P3", [bar(whole_rest(), 1, attributes()), bar(four_crotchets("F4"), 2)])
    root = ET.fromstring(make_score([("P1", "Voice", marimba), ("P2", "Voice", voice), ("P3", "Mar.", marimba2)]))
    ok, report = merge.merge(root, ["Marimba", "Voice"], {"P1": "Marimba", "Mar.": "Marimba", "Voice": "Voice"})
    assert ok, report
    assert [name for _, name in score.part_names(root)] == ["Marimba", "Voice"]
    assert _pitches(root.findall("part")[0]) == ["D4"] * 4 + ["F4"] * 4


def test_names_not_in_the_table_stay_as_they_were():
    root = fragmented()
    ok, report = merge.merge(root, ["Flute"], {"Fl.": "Flute", "Flute": "Flute", "Ob.": None})
    assert ok, report
    assert [name for _, name in score.part_names(root)] == ["Flute", "Ob."]


def test_an_alias_to_an_unlisted_instrument_is_refused():
    ok, report = merge.merge(fragmented(), ["Flute"], {"Fl.": "Flute", "Ob.": "Oboe"})
    assert not ok and "Oboe" in report[0]


def test_every_instrument_a_note_names_is_declared():
    root = fragmented()
    for p in root.findall("part"):
        for n in p.iter("note"):
            ET.SubElement(n, "instrument", {"id": f"{p.get('id')}-I1"})
    ok, _ = merge.merge(root, ["Flute", "Oboe"], {"Fl.": "Flute", "Flute": "Flute", "Ob.": "Oboe"})
    assert ok
    declared = {i.get("id") for i in root.iter("score-instrument")}
    used = {i.get("id") for p in root.findall("part") for i in p.iter("instrument")}
    assert used and used <= declared


def test_merge_file_writes_only_when_conserved(tmp_path):
    source = write_mxl(tmp_path / "in.mxl", ET.tostring(fragmented(), encoding="unicode"))
    config = tmp_path / "merge.json"
    config.write_text(json.dumps({"canonical_order": ["Flute", "Oboe"],
                                  "aliases": {"Fl.": "Flute", "Flute": "Flute", "Ob.": "Oboe"}}))
    out = tmp_path / "out.mxl"
    assert merge.main([str(source), str(out), "--config", str(config)]) == 0
    assert [n for _, n in score.part_names(score.load(out))] == ["Flute", "Oboe"]

    a = part("P1", [bar(four_crotchets(), 1, attributes())])
    clash = write_mxl(tmp_path / "clash.mxl", make_score([("P1", "V", a), ("P2", "V", a.replace("P1", "P2"))]))
    config.write_text(json.dumps({"canonical_order": ["V"], "aliases": {"V": "V"}}))
    refused = tmp_path / "refused.mxl"
    assert merge.main([str(clash), str(refused), "--config", str(config)]) == 1
    assert not refused.exists()


@pytest.mark.parametrize("missing", [{"canonical_order": []}, {"aliases": {}}])
def test_a_config_missing_a_key_fails_loudly(tmp_path, missing):
    source = write_mxl(tmp_path / "in.mxl", ET.tostring(fragmented(), encoding="unicode"))
    config = tmp_path / "merge.json"
    config.write_text(json.dumps(missing))
    with pytest.raises(KeyError):
        merge.merge_file(source, tmp_path / "out.mxl", config)
