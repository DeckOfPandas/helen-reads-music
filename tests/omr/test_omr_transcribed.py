"""tools/omr/transcribed.py: a percussion score built from a transcription by eye."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

import score  # noqa: E402
import transcribed  # noqa: E402


def spec(**part) -> dict:
    base = {"name": "Drums", "abbreviation": "Dr.",
            "kit": {"bd": ["F4", 36], "hh": ["xG5", 42], "ohh": ["oxG5", 46], "sl": ["sB4"]},
            "grooves": {"A": {"up": "hh:1/2 hh:1/2 hh:1/2 hh:1/2 hh:1/2 hh:1/2 hh:1/2 hh:1/2",
                              "down": "bd:1 r:1 bd:1 r:1"}}}
    base.update(part)
    return {"title": "Test", "bars": 4, "time": {"1": "4/4"}, "tempo": {"1": [1, 120, "Brisk"]},
            "parts": [base]}


def notes(measure):
    return [n for n in measure.findall("note") if n.find("rest") is None]


def test_bars_come_from_grooves_strings_and_whole_rests():
    root = transcribed.build(spec(bars={"1-2": "@A", "3": "sl:1 sl:1 sl:1 sl:1"}))
    measures = root.find("part").findall("measure")
    assert [len(notes(m)) for m in measures] == [10, 10, 4, 0]
    assert measures[3].find("note/rest").get("measure") == "yes"
    assert {n.findtext("voice") for n in notes(measures[0])} == {"1", "2"}
    assert score.bad_bars(root.find("part")) == []


def test_each_kit_name_plays_its_own_sound():
    """Two instruments may share a stave position on the page; the sound
    follows the name, not the position."""
    root = transcribed.build(spec(kit={"bd": ["F4", 36], "hh": ["xG5", 42], "cb": ["xG5", 56]},
                                  bars={"1": "hh:1 cb:1 hh:1 cb:1"}))
    ids = [n.find("instrument").get("id") for n in notes(root.find("part/measure"))]
    assert ids == ["P1-I42", "P1-I56", "P1-I42", "P1-I56"]
    declared = {m.get("id"): m.findtext("midi-unpitched") for m in root.iter("midi-instrument")}
    assert declared == {"P1-I42": "43", "P1-I56": "57"}          # only the sounds used; GM note + 1
    slash = transcribed.build(spec(bars={"1": "sl:4"}))
    assert notes(slash.find("part/measure"))[0].find("instrument") is None


def test_ties_across_a_kit_name_and_marks():
    root = transcribed.build(spec(bars={"1": "r:2 r:1 ohh:1~", "2": "~hh:1 r:3"},
                                  marks={"1": ["0 mf", "3 open"]}))
    first, second = root.find("part").findall("measure")[:2]
    assert notes(first)[0].find("tie").get("type") == "start"
    assert notes(second)[0].find("tie").get("type") == "stop"
    assert first.find("direction/direction-type/dynamics/mf") is not None
    assert [w.text for w in first.iter("words")] == ["Brisk", "open"]


def test_tempo_clefs_and_pitched_bars():
    root = transcribed.build(spec(clef={"2": "bass", "3": "percussion"},
                                  pitched={"2": ["Timpani", 48]},
                                  bars={"2": "pC3:2 pG2:2"}))
    measures = root.find("part").findall("measure")
    assert measures[0].findtext("direction/direction-type/metronome/per-minute") == "120"
    assert measures[0].find("direction/sound").get("tempo") == "120"
    assert measures[0].findtext("attributes/clef/sign") == "percussion"
    assert measures[1].findtext("attributes/clef/sign") == "F"
    assert measures[2].findtext("attributes/clef/sign") == "percussion"
    timpani = notes(measures[1])
    assert [n.findtext("pitch/step") for n in timpani] == ["C", "G"]
    assert {n.find("instrument").get("id") for n in timpani} == {"P1-P48"}
    assert root.find(".//midi-instrument[@id='P1-P48']/midi-program").text == "48"


def test_a_minim_beat_tempo_sounds_in_crotchets():
    built = spec(bars={})
    built["time"], built["tempo"] = {"1": "2/2"}, {"1": [2, 98]}
    root = transcribed.build(built)
    measure = root.find("part/measure")
    assert measure.findtext("direction/direction-type/metronome/beat-unit") == "half"
    assert measure.find("direction/sound").get("tempo") == "196"


@pytest.mark.parametrize("bars, message", [
    ({"1": "hh:1 hh:1 hh:1"}, "lasts 3 crotchets"),
    ({"2": "@B"}, "no groove 'B'"),
    ({"1-2": "@A", "2": "@A"}, "given twice"),
    ({"5": "@A"}, "outside 1-4"),
    ({"1": {"left": "hh:4"}}, "unknown voice"),
])
def test_a_transcription_that_does_not_fit_is_refused_with_its_bar(bars, message):
    with pytest.raises(ValueError, match=message):
        transcribed.build(spec(bars=bars))


def test_the_command_writes_a_checked_score(tmp_path, capsys):
    source = tmp_path / "transcription.json"
    source.write_text(json.dumps(spec(bars={"1-4": "@A"})))
    out = tmp_path / "out.musicxml"
    assert transcribed.main([str(source), str(out)]) == 0
    assert "bars that do not add up: 0 of 4 checked" in capsys.readouterr().out
    assert score.load(out).find("part-list/score-part/part-name").text == "Drums"
