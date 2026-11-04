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
from musicxml_maker import (attributes, bar, four_crotchets, part, whole_rest,  # noqa: E402
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
    assert "time 4/4 written into 1 part(s)" in report
    assert "time 4/4 written into 0 part(s)" in repair.repair(root, {"time": "4/4"})


def test_a_whole_bar_rest_takes_the_bar_length():
    # 18 ticks = 4.5 crotchets: the length Audiveris inferred from another staff.
    root = build([("P1", "Bsn.", part("P1", [rest_bar(1, 18, untimed())]))])
    report = repair.repair(root, {"time": "4/4"})
    assert root.find("part/measure/note/duration").text == "16"
    assert report[-1] == ("whole-bar rests: 1 set to a full bar, 0 added to empty staves, "
                          "0 fitted to the bar beside notes in another voice")
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
    assert report[-1] == ("whole-bar rests: 0 set to a full bar, 1 added to empty staves, "
                          "0 fitted to the bar beside notes in another voice")
    assert score.bad_bars(root.find("part")) == []


def test_a_silent_lower_staff_rest_beside_notes_takes_the_bar_length():
    # Piano: notes on top, the left hand's whole-bar rest read at 4.5 crotchets.
    measure = ('<measure number="1">' + untimed(staves=2)
               + "".join(f"<note><pitch><step>C</step><octave>5</octave></pitch><duration>4</duration>"
                         f"<voice>1</voice><staff>1</staff></note>" for _ in range(4))
               + "<backup><duration>16</duration></backup>" + measure_rest(18, staff=2).replace(
                   "<voice>1</voice>", "<voice>5</voice>")
               + "<backup><duration>18</duration></backup></measure>")
    root = build([("P1", "Piano", f'<part id="P1">{measure}</part>')])
    report = repair.repair(root, {"time": "4/4"})
    assert report[-1].endswith("1 fitted to the bar beside notes in another voice")
    bar = root.find("part/measure")
    assert [n.findtext("duration") for n in bar.findall("note")][-1] == "16"
    assert [e.tag for e in bar][-1] == "note"                 # the dangling backup is gone
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
    assert "octave clefs: 1 '8 below/above' marking(s) removed, 6 note(s) moved back" in report
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


GROOVE = {"up": "xG5:1 xG5:1/2 xG5:1/2 xG5+C5:1 xG5:1/2 xG5:1/2",
          "down": "F4:1 r:1/2 F4:1/2 r:1 r:1"}


def drum_bar(number: int, hits: list[tuple], first: bool = False) -> str:
    """A bar the way Audiveris reads a kit: kick line in voice 1 (the second
    kick a crotchet, not a quaver: 4.5 crotchets), snare alone in voice 2."""
    attrs = ("<attributes><divisions>2</divisions><time><beats>4</beats><beat-type>4</beat-type></time>"
             "<clef><sign>percussion</sign></clef></attributes>") if first else ""
    notes = ""
    for step, octave, ticks, voice in hits:
        if step == "back":
            notes += f"<backup><duration>{ticks}</duration></backup>"
        elif step == "fwd":
            notes += f"<forward><duration>{ticks}</duration><voice>{voice}</voice></forward>"
        elif step == "r":
            notes += f"<note><rest/><duration>{ticks}</duration><voice>{voice}</voice></note>"
        else:
            notes += (f"<note><unpitched><display-step>{step}</display-step><display-octave>{octave}"
                      f"</display-octave></unpitched><duration>{ticks}</duration><voice>{voice}</voice></note>")
    return f'<measure number="{number}">{attrs}{notes}</measure>'


MISREAD_GROOVE = [("F", 4, 2, 1), ("r", 0, 1, 1), ("F", 4, 2, 1), ("r", 0, 2, 1), ("r", 0, 2, 1),
                  ("back", 0, 9, 0), ("fwd", 0, 5, 2), ("C", 5, 2, 2)]
KICK_ONLY = [("F", 4, 2, 1), ("r", 0, 6, 1)]


def test_a_drum_groove_fills_the_bars_whose_kick_and_snare_match():
    drums = f'<part id="P1">{drum_bar(1, MISREAD_GROOVE, True)}{drum_bar(2, KICK_ONLY)}{drum_bar(3, MISREAD_GROOVE)}</part>'
    root = build([("P1", "Drum Set", drums)])
    assert [b.number for b in score.bad_bars(root.find("part"))] == ["1", "3"]
    report = repair.repair(root, {"drum_grooves": {"grooves": {"A": GROOVE}}})
    assert "Drum Set: groove A written into 2 bar(s): 1, 3" in report
    part_element = root.find("part")
    assert [b.number for b in score.bad_bars(part_element)] == []
    bar1 = part_element.findall("measure")[0]
    assert bar1.find("attributes/clef/sign").text == "percussion"
    hats = [n for n in bar1.findall("note") if n.findtext("notehead") == "x"]
    assert len(hats) == 6 and all(n.findtext("unpitched/display-step") == "G" for n in hats)
    snare = [n for n in bar1.findall("note") if n.find("chord") is not None]
    assert [n.findtext("unpitched/display-step") for n in snare] == ["C"]
    kicks = [n for n in bar1.findall("note") if n.findtext("voice") == "2" and n.find("unpitched") is not None]
    assert [n.findtext("duration") for n in kicks] == ["2", "1"]
    # The kick-only bar does not match and is left as read.
    assert len(part_element.findall("measure")[1].findall("note")) == 2


def test_a_groove_goes_into_listed_bars_whatever_they_hold():
    drums = f'<part id="P1">{drum_bar(1, KICK_ONLY, True)}{drum_bar(2, KICK_ONLY)}</part>'
    root = build([("P1", "Drum Set", drums)])
    report = repair.repair(root, {"drum_grooves": {"grooves": {"A": {**GROOVE, "bars": [2]}}}})
    assert "Drum Set: groove A written into 1 bar(s): 2" in report


def test_a_groove_that_does_not_fill_the_bar_is_refused():
    drums = f'<part id="P1">{drum_bar(1, MISREAD_GROOVE, True)}</part>'
    root = build([("P1", "Drum Set", drums)])
    with pytest.raises(ValueError, match="lasts 7/2"):
        repair.repair(root, {"time": "4/4", "drum_grooves": {"grooves": {"A": {
            "up": "xG5:1 xG5:1/2 xG5+C5:1 xG5:1/2 xG5:1/2", "down": GROOVE["down"]}}}})


def test_a_groove_never_goes_into_a_bar_of_another_length():
    three_four = ('<measure number="2"><attributes><time><beats>3</beats><beat-type>4</beat-type></time>'
                  "</attributes>" + drum_bar(9, MISREAD_GROOVE)[len('<measure number="9">'):])
    drums = f'<part id="P1">{drum_bar(1, KICK_ONLY, True)}{three_four}</part>'
    root = build([("P1", "Drum Set", drums)])
    report = repair.repair(root, {"drum_grooves": {"grooves": {"A": GROOVE}}})
    assert "Drum Set: groove A written into 0 bar(s): none" in report


def test_an_open_hi_hat_gets_the_open_mark_and_listed_bars_win_over_matching():
    open_groove = {"up": "xG5:1 xG5:1/2 xG5:1/2 xG5+C5:1 oxG5:1", "down": GROOVE["down"], "bars": [2]}
    drums = f'<part id="P1">{drum_bar(1, MISREAD_GROOVE, True)}{drum_bar(2, MISREAD_GROOVE)}</part>'
    root = build([("P1", "Drum Set", drums)])
    report = repair.repair(root, {"drum_grooves": {"grooves": {"A": GROOVE, "A open": open_groove}}})
    assert "Drum Set: groove A written into 1 bar(s): 1" in report
    assert "Drum Set: groove A open written into 1 bar(s): 2" in report
    bar2 = root.find("part").findall("measure")[1]
    opened = [n for n in bar2.findall("note") if n.find("notations/technical/open") is not None]
    assert len(opened) == 1 and opened[0].findtext("notehead") == "x" and opened[0].findtext("type") == "quarter"
    assert score.bad_bars(root.find("part")) == []


def test_drum_tokens_carry_accents_slashes_and_ties():
    events = repair.parse_groove_voice("xG5+C5:1^ r:1/2 B4:1/2>~ ~B4:1 sB4:1")
    assert events[0].strong and [h.cross for h in events[0].heads] == [True, False]
    assert events[2].accent and events[2].tie_start and events[3].heads[0].tie_stop
    assert events[4].heads[0].slash
    root = build([("P1", "Drum Set", f'<part id="P1">{drum_bar(1, KICK_ONLY, True)}</part>')])
    repair.apply_patches(root, {"Drum Set": {"1": {"up": "B4:1> r:1/2 B4:1/2~ ~B4:1 F5:1>"}}})
    notes = root.find("part/measure").findall("note")
    assert notes[0].find("notations/articulations/accent") is not None
    assert notes[2].find("tie").get("type") == "start" and notes[3].find("tie").get("type") == "stop"
    assert notes[2].find("notations/tied").get("type") == "start"
    assert {n.findtext("stem") for n in notes if n.find("unpitched") is not None} == {"up"}


def test_a_drum_bar_with_only_a_lower_voice():
    root = build([("P1", "Drum Set", f'<part id="P1">{drum_bar(1, KICK_ONLY, True)}</part>')])
    repair.apply_patches(root, {"Drum Set": {"1": {"down": "r:2 D5:1/2 D5:1/2 B4:1"}}})
    m = root.find("part/measure")
    assert m.find("backup") is None
    assert {n.findtext("voice") for n in m.findall("note")} == {"2"}
    assert score.bad_bars(root.find("part")) == []


def test_slash_bars():
    root = build([("P1", "Drum Set", f'<part id="P1">{drum_bar(1, KICK_ONLY, True)}</part>')])
    repair.apply_patches(root, {"Drum Set": {"1": {"up": "sB4:1 sB4:1 sB4:1 sB4:1"}}})
    assert [n.findtext("notehead") for n in root.iter("note")] == ["slash"] * 4


def test_every_drum_note_gets_its_instrument_and_voices_their_stems():
    drums = f'<part id="P1">{drum_bar(1, MISREAD_GROOVE, True)}</part>'
    root = build([("P1", "Drum Set", drums)])
    repair.repair(root, {"drum_grooves": {"grooves": {"A": {
        "up": "xG5:1 xG5:1/2 xG5:1/2 xG5+C5:1 xG5:1/2 oxG5:1/2",
        "down": "F4+xD4:1 r:1/2 F4:1/2 r:1 r:1"}}}})
    report = repair.voice_drum_parts(root)
    assert "NOT IN THE DRUM KEY" not in report[0]
    notes = [n for n in root.iter("note") if n.find("unpitched") is not None]
    by_sound = {}
    for n in notes:
        by_sound.setdefault(n.find("instrument").get("id"), set()).add(n.findtext("stem"))
    assert by_sound == {"P1-I42": {"up"}, "P1-I38": {"up"}, "P1-I46": {"up"},
                        "P1-I36": {"down"}, "P1-I44": {"down"}}
    declared = {i.get("id"): i.findtext("instrument-name") for i in root.iter("score-instrument")}
    assert declared == {"P1-I36": "Bass Drum", "P1-I38": "Snare", "P1-I42": "Closed Hi-Hat",
                        "P1-I44": "Pedal Hi-Hat", "P1-I46": "Open Hi-Hat"}
    midi = {m.get("id"): m.findtext("midi-unpitched") for m in root.iter("midi-instrument")}
    assert midi["P1-I36"] == "37" and midi["P1-I42"] == "43"        # General MIDI note + 1
    sp = root.find("part-list/score-part")
    assert [c.tag for c in sp] == ["part-name"] + ["score-instrument"] * 5 + ["midi-instrument"] * 5


def test_a_drum_note_outside_the_key_is_named_and_the_key_can_grow():
    bar = ('<measure number="1"><attributes><divisions>1</divisions><time><beats>4</beats>'
           "<beat-type>4</beat-type></time></attributes>"
           "<note><unpitched><display-step>E</display-step><display-octave>4</display-octave></unpitched>"
           "<duration>4</duration><voice>1</voice><notehead>x</notehead></note></measure>")
    root = build([("P1", "Drum Set", f'<part id="P1">{bar}</part>')])
    assert "NOT IN THE DRUM KEY: {'E4 x': 1}" in repair.voice_drum_parts(root)[0]
    repair.voice_drum_parts(root, {"E4 x": 54})
    assert root.find(".//note/instrument").get("id") == "P1-I54"


def _with_lyric(text, chord_member=False):
    events = [("C5", 1), ("E5+", 1), ("D5", 1), ("C5", 1)] if chord_member else [("C5", 1)] * 4
    root = build([_timed_part("P1", "Trumpet", events)])
    target = root.findall(".//note")[1 if chord_member else 0]
    lyric = ET.SubElement(target, "lyric")
    ET.SubElement(lyric, "text").text = text
    return root


@pytest.mark.parametrize("text,dynamic", [("f", "f"), ("Mf", "mf"), ('"f', "f"), ("P", "p"), ("Wf", "f")])
def test_a_dynamic_read_as_a_lyric_becomes_a_dynamic(text, dynamic):
    root = _with_lyric(text)
    report = repair.marks_from_lyrics(root, ["Voice"])
    assert root.find(".//lyric") is None
    assert root.find(f".//direction/direction-type/dynamics/{dynamic}") is not None
    assert report[0].startswith("marks read as lyrics: 1 dynamic(s)")


@pytest.mark.parametrize("text,mark", [("A", "strong-accent"), (">A", "strong-accent"), ("V", "strong-accent"),
                                       (">", "accent")])
def test_an_accent_read_as_a_lyric_becomes_an_accent(text, mark):
    root = _with_lyric(text)
    repair.marks_from_lyrics(root, ["Voice"])
    assert root.find(f".//note/notations/articulations/{mark}") is not None


def test_instructions_become_text_and_noise_is_listed():
    root = _with_lyric("p1zz.")
    repair.marks_from_lyrics(root, ["Voice"])
    assert root.findtext(".//direction/direction-type/words") == "pizz."
    root = _with_lyric("fdirty")
    repair.marks_from_lyrics(root, ["Voice"])
    assert root.find(".//dynamics/f") is not None and root.findtext(".//words") == "dirty"
    root = _with_lyric("[q")
    report = repair.marks_from_lyrics(root, ["Voice"])
    assert "lyrics dropped as unreadable (1): Trumpet 1 '[q'" in report[1]


def test_a_mark_on_a_chord_note_goes_before_the_chord():
    root = _with_lyric("f", chord_member=True)
    repair.marks_from_lyrics(root, ["Voice"])
    tags = [e.tag for e in root.find("part/measure")]
    assert tags.index("direction") < tags.index("note")
    notes = root.find("part/measure").findall("note")
    assert notes[1].find("chord") is not None
    assert list(root.find("part/measure")).index(notes[1]) == list(root.find("part/measure")).index(notes[0]) + 1


def test_the_voice_keeps_its_lyrics():
    root = build([_timed_part("P1", "Voice", [("C5", 1)] * 4)])
    lyric = ET.SubElement(root.find(".//note"), "lyric")
    ET.SubElement(lyric, "text").text = "f"
    repair.marks_from_lyrics(root, ["Voice"])
    assert root.findtext(".//lyric/text") == "f"


def _violins(bars_one, bars_two):
    return build([("P1", "Violin I", part("P1", bars_one)), ("P2", "Violin II", part("P2", bars_two))])


VIOLIN_LABELS = {"same": "unis.", "divided": "div.", "first": "1.", "second": "2."}


def test_combining_two_parts_onto_one_stave():
    one = [bar(four_crotchets("C5"), 1, attributes()), bar(four_crotchets("E5"), 2),
           bar(four_crotchets("G5"), 3), bar(whole_rest(), 4)]
    two = [bar(four_crotchets("C5"), 1, attributes()), bar(four_crotchets("C5"), 2),
           bar(whole_rest(), 3), bar(four_crotchets("D5"), 4)]
    root = _violins(one, two)
    report = repair.combine_parts(root, {"parts": ["Violin I", "Violin II"], "name": "Violins",
                                         "abbreviation": "Vlns.", "labels": VIOLIN_LABELS})
    assert report == ["Violin I + Violin II -> Violins: 1 bar(s) the same, 1 only Violin I, "
                      "1 only Violin II, 1 in two voices"]
    assert [n for _, n in score.part_names(root)] == ["Violins"]
    assert root.findtext("part-list/score-part/part-abbreviation") == "Vlns."
    bars = root.find("part").findall("measure")
    voices = [sorted({n.findtext("voice") for n in m.findall("note")}) for m in bars]
    assert voices == [["1"], ["1", "2"], ["1"], ["1"]]
    stems = {n.findtext("voice"): n.findtext("stem") for n in bars[1].findall("note")}
    assert stems == {"1": "up", "2": "down"}
    assert bars[1].find("backup/duration").text == "16"
    assert [n.findtext("pitch/step") for n in bars[3].findall("note")] == ["D"] * 4
    words = [m.findtext("direction/direction-type/words") for m in bars]
    assert words == ["unis.", "div.", "1.", "2."]
    assert score.bad_bars(root.find("part")) == []


def test_three_parts_combine_onto_one_stave_in_two_steps():
    def line(*pitches):
        return [bar(four_crotchets(p), n, attributes() if n == 1 else "") if p else bar(whole_rest(), n,
                attributes() if n == 1 else "") for n, p in enumerate(pitches, start=1)]
    root = build([("P1", "Voice 1", part("P1", line("C5", "E5", "G5"))),
                  ("P2", "Voice 2", part("P2", line(None, "C5", "E5"))),
                  ("P3", "Voice 3", part("P3", line(None, None, "C5")))])
    repair.combine_parts(root, {"parts": ["Voice 1", "Voice 2"], "name": "Voice"})
    repair.combine_parts(root, {"parts": ["Voice", "Voice 3"], "name": "Voice"})
    assert [n for _, n in score.part_names(root)] == ["Voice"]
    bars = root.find("part").findall("measure")
    voices = [sorted({n.findtext("voice") for n in m.findall("note")}) for m in bars]
    assert voices == [["1"], ["1", "2"], ["1", "2", "3"]]
    assert sum(score.count_notes(m) for m in bars) == 4 + 8 + 12
    assert score.bad_bars(root.find("part")) == []


def test_a_label_is_written_only_where_the_state_changes():
    same = [bar(four_crotchets("C5"), n, attributes() if n == 1 else "") for n in (1, 2, 3)]
    root = _violins(same, [bar(four_crotchets("C5"), n, attributes() if n == 1 else "") for n in (1, 2, 3)])
    repair.combine_parts(root, {"parts": ["Violin I", "Violin II"], "labels": VIOLIN_LABELS})
    assert [m.findtext("direction/direction-type/words") for m in root.find("part").findall("measure")] == \
        ["unis.", None, None]


def test_combining_two_grand_staves_staff_by_staff():
    def grand(rh, lh):
        return ('<measure number="1">' + untimed(staves=2).replace("</attributes>",
                "<time><beats>4</beats><beat-type>4</beat-type></time></attributes>")
                + "".join(f"<note><pitch><step>{s}</step><octave>5</octave></pitch><duration>4</duration>"
                          f"<voice>1</voice><staff>1</staff></note>" for s in rh)
                + "<backup><duration>16</duration></backup>"
                + ("".join(f"<note><pitch><step>{s}</step><octave>3</octave></pitch><duration>4</duration>"
                           f"<voice>5</voice><staff>2</staff></note>" for s in lh) if lh else
                   '<note><rest measure="yes"/><duration>16</duration><voice>5</voice><staff>2</staff></note>')
                + "</measure>")
    root = build([("P1", "Harp", f'<part id="P1">{grand("CDEF", None)}</part>'),
                  ("P2", "Piano", f'<part id="P2">{grand("CDEF", "GABC")}</part>')])
    report = repair.combine_parts(root, {"parts": ["Harp", "Piano"], "name": "Harp & Piano"})
    m = root.find("part/measure")
    assert [n.findtext("staff") for n in m.findall("note")] == ["1"] * 4 + ["2"] * 4
    assert [n.findtext("pitch/step") for n in m.findall("note")][4:] == ["G", "A", "B", "C"]
    assert "1 bar(s) the same" in report[0]
    assert score.bad_bars(root.find("part")) == []


def test_labels_and_sounds_follow_the_part_name():
    root = build([("P1", "Contrabass", part("P1", [bar(four_crotchets("C3"), 1, attributes())])),
                  ("P2", "Violin II", part("P2", [bar(four_crotchets("C5"), 1, attributes())]))])
    sp = root.find("part-list/score-part")
    ET.SubElement(sp, "part-abbreviation").text = "Voice"
    report = repair.fix_labels_and_sounds(root, {})
    abbreviations = [s.findtext("part-abbreviation") for s in root.iter("score-part")]
    assert abbreviations == ["Cb.", "Vln. II"]
    assert [m.findtext("midi-program") for m in root.iter("midi-instrument")] == ["44", "41"]
    assert [i.findtext("instrument-name") for i in root.iter("score-instrument")] == ["Contrabass", "Violin II"]
    assert report[0].startswith("labels and playback sounds: Contrabass (Cb., program 44)")


@pytest.mark.parametrize("token", ["xG5", "Q5:1", "xG:1", "oxQ5:1"])
def test_bad_groove_tokens_are_refused(token):
    with pytest.raises(ValueError):
        repair.parse_groove_voice(token)


def _timed_part(pid, name, events, extra=""):
    return (pid, name, part(pid, [bar(events, 1, attributes() + extra)]))


def _durations(measure, voice="1"):
    return [(n.findtext("pitch/step") or "r", n.findtext("duration"), n.findtext("type"), len(n.findall("dot")))
            for n in measure.findall("note") if (n.findtext("voice") or "1") == voice]


def test_a_patch_edit_puts_back_missing_rests_and_keeps_the_notes():
    # Piccolo bar 44: crotchet rest, two quavers, rest, quaver, (missing rest), quaver.
    events = [("r", 1), ("D5", 0.5), ("D5", 0.5), ("r", 0.5), ("F5", 0.5), ("D5", 0.5)]
    root = build([_timed_part("P1", "Piccolo", events)])
    note = root.find(".//note[2]")
    ET.SubElement(note, "notations").append(ET.Element("staccato"))
    report = repair.apply_patches(root, {"Piccolo": {"1": "1 1/2 1/2 1/2 1/2 +r:1/2 1/2"}})
    assert report == ["Piccolo bar 1: patched"]
    measure = root.find("part/measure")
    assert [d[0] for d in _durations(measure)] == ["r", "D", "D", "r", "F", "r", "D"]
    assert measure.find("note[2]/notations/staccato") is not None      # kept
    assert score.bad_bars(root.find("part")) == []


def test_a_patch_edit_fixes_a_dotted_misread_and_drops_stray_gaps():
    # Trumpet bar 3: C5 read dotted, then a forward, rest dropped.
    measure = ('<measure number="1">' + attributes()
               + "".join(f"<note><pitch><step>{s}</step><octave>{o}</octave></pitch><duration>{d}</duration>"
                         f"<voice>1</voice><type>eighth</type>{'<dot/>' if d == 3 else ''}</note>"
                         for s, o, d in (("C", 4, 2), ("E", 4, 2), ("G", 4, 2), ("C", 5, 3)))
               + "<forward><duration>1</duration></forward>"
               + "".join(f"<note><pitch><step>{s}</step><octave>{o}</octave></pitch><duration>2</duration>"
                         f"<voice>1</voice><type>eighth</type></note>" if s != "r" else
                         "<note><rest/><duration>2</duration><voice>1</voice><type>eighth</type></note>"
                         for s, o in (("C", 5), ("r", 0), ("D", 4)))
               + "</measure>")
    root = build([("P1", "Trumpet", f'<part id="P1">{measure}</part>')])
    repair.apply_patches(root, {"Trumpet": {"1": "1/2 1/2 1/2 1/2 +r:1/2 1/2 1/2 1/2"}})
    m = root.find("part/measure")
    assert m.find("forward") is None
    assert [d[:2] for d in _durations(m)] == [("C", "2"), ("E", "2"), ("G", "2"), ("C", "2"), ("r", "2"),
                                              ("C", "2"), ("r", "2"), ("D", "2")]
    assert all(d[3] == 0 for d in _durations(m))                        # the stray dot is gone


def test_a_patch_inserts_a_tied_note_with_its_lyric():
    events = [("r", 4)]
    root = build([_timed_part("P1", "Voice", events)])
    repair.apply_patches(root, {"Voice": {"1": "2 +G5:2~@Yeah,"}})
    notes = root.find("part/measure").findall("note")
    assert [n.findtext("duration") for n in notes] == ["8", "8"]
    g = notes[1]
    assert g.findtext("pitch/step") == "G" and g.findtext("pitch/octave") == "5"
    assert g.find("tie").get("type") == "start" and g.find("notations/tied").get("type") == "start"
    assert g.findtext("lyric/text") == "Yeah,"
    assert [c.tag for c in g] == ["pitch", "duration", "tie", "voice", "type", "notations", "lyric"]


def test_a_patch_can_tie_from_the_bar_before_and_spell_accidentals():
    root = build([_timed_part("P1", "Voice", [("E5", 0.5), ("D5", 0.5), ("C5", 1)])])
    repair.apply_patches(root, {"Voice": {"1": "+~G5:2 1/2 1/2 1"}})
    first = root.find("part/measure/note")
    assert first.find("tie").get("type") == "stop"
    root = build([_timed_part("P1", "Oboe", [("C5", 4)])])
    repair.apply_patches(root, {"Oboe": {"1": {"notes": "Bb4:1 F#5:1 Bn4:1 r:1"}}})
    alters = [n.findtext("pitch/alter") for n in root.iter("note") if n.find("pitch") is not None]
    assert alters == ["-1", "1", None]


def test_a_patch_replaces_a_tangled_bar_and_its_other_voices():
    bars = ('<measure number="1">' + attributes()
            + "<note><pitch><step>A</step><octave>4</octave></pitch><duration>8</duration><voice>1</voice></note>"
            + "<note><pitch><step>F</step><octave>5</octave></pitch><duration>8</duration><voice>1</voice></note>"
            + "<backup><duration>16</duration></backup>"
            + "<note><rest/><duration>8</duration><voice>3</voice></note></measure>")
    root = build([("P1", "Marimba", f'<part id="P1">{bars}</part>')])
    repair.apply_patches(root, {"Marimba": {"1": {"notes": "A4+C5+F5:2 A4+C5:1 r:1"}}})
    m = root.find("part/measure")
    assert {n.findtext("voice") for n in m.findall("note")} == {"1"}
    assert m.find("backup") is None
    assert len([n for n in m.findall("note") if n.find("chord") is not None]) == 3
    assert score.bad_bars(root.find("part")) == []


def test_a_patch_can_re_pitch_one_note_and_keep_its_marks():
    root = build([_timed_part("P1", "Trombone", [("C4", 1), ("E4", 1), ("G4", 2)])])
    second = root.findall(".//note")[1]
    ET.SubElement(second.find("pitch"), "alter").text = "-1"
    ET.SubElement(second, "notations").append(ET.Element("accent"))
    repair.apply_patches(root, {"Trombone": {"1": "1 =En4:1 2"}})
    note = root.findall(".//note")[1]
    assert note.findtext("pitch/alter") is None and note.findtext("pitch/step") == "E"
    assert note.find("notations/accent") is not None


def test_a_fingered_tremolo_is_written_at_full_value_lasting_half():
    root = build([_timed_part("P1", "Marimba", [("C5", 4)])])
    repair.apply_patches(root, {"Marimba": {"1": {"notes": "F4+Bb4/D5:2 F4+Bb4:1 r:1"}}})
    notes = root.find("part/measure").findall("note")
    tremolo = notes[:3]
    assert [n.findtext("type") for n in tremolo] == ["half"] * 3
    assert [n.findtext("duration") for n in tremolo] == ["4", "4", "4"]            # a crotchet each
    assert [n.findtext("time-modification/actual-notes") for n in tremolo] == ["2"] * 3
    assert tremolo[0].find("notations/ornaments/tremolo").get("type") == "start"
    assert tremolo[1].find("chord") is not None                                    # Bb4 on the F4
    assert tremolo[2].find("notations/ornaments/tremolo").get("type") == "stop"
    assert tremolo[2].findtext("pitch/step") == "D"
    assert score.bad_bars(root.find("part")) == []
    root = build([_timed_part("P1", "Marimba", [("C5", 4)])])
    repair.apply_patches(root, {"Marimba": {"1": {"notes": "Bb4+D5/F5:4"}}})
    assert [n.findtext("type") for n in root.iter("note")] == ["whole"] * 3


def test_replacing_one_voice_of_a_piano_keeps_the_other_staff():
    measure = ('<measure number="1">' + attributes()
               + "".join(f"<note><pitch><step>D</step><octave>6</octave></pitch><duration>4</duration>"
                         f"<voice>1</voice><staff>1</staff></note>" for _ in range(4))
               + "<backup><duration>16</duration></backup>"
               + '<note><rest measure="yes"/><duration>16</duration><voice>5</voice><staff>2</staff></note>'
               + "</measure>")
    root = build([("P1", "Piano", f'<part id="P1">{measure}</part>')])
    repair.apply_patches(root, {"Piano": {"1": {"voice": "1", "notes": "D6:1/2 r:1/2 Bb5:1/2 r:1/2 Bb5:2"}}})
    m = root.find("part/measure")
    assert [e.tag for e in m if e.tag in ("note", "backup")] == ["note"] * 5 + ["backup", "note"]
    assert m.find("backup/duration").text == "16"
    assert m.findall("note")[-1].findtext("staff") == "2"
    assert score.bad_bars(root.find("part")) == []


def test_shortening_a_whole_bar_rest_makes_it_an_ordinary_rest():
    measure = ('<measure number="1">' + attributes()
               + '<note><rest measure="yes"/><duration>16</duration><voice>1</voice></note></measure>')
    root = build([("P1", "Voice", f'<part id="P1">{measure}</part>')])
    repair.apply_patches(root, {"Voice": {"1": "2 +G5:2~@Yeah,"}})
    rest = root.find("part/measure/note")
    assert rest.find("rest").get("measure") is None and rest.findtext("type") == "half"


def _pitched(step, octave, alter=None, printed=False):
    alter_xml = f"<alter>{alter}</alter>" if alter is not None else ""
    accidental = "<accidental>natural</accidental>" if printed else ""
    return (f"<note><pitch><step>{step}</step>{alter_xml}<octave>{octave}</octave></pitch>"
            f"<duration>4</duration><voice>1</voice>{accidental}</note>")


def test_a_lost_key_change_is_written_and_the_old_keys_flats_respelled():
    # B-flat major until bar 2, where the score goes to C major but the key
    # was not exported: bar 2's E-flats came from the old key.
    flat_key = attributes(fifths=-2)
    bar1 = f'<measure number="1">{flat_key}' + _pitched("E", 4, -1) * 4 + "</measure>"
    bar2 = ('<measure number="2">' + _pitched("E", 4, -1) + _pitched("E", 4, -1)
            + _pitched("B", 4, -1) + _pitched("G", 4) + "</measure>")
    bar3 = '<measure number="3">' + _pitched("E", 4) * 4 + "</measure>"
    oboe = f'<part id="P1">{bar1}{bar2}{bar3}</part>'
    clarinet_bar2 = '<measure number="2"><attributes><key><fifths>2</fifths></key></attributes>' + _pitched("F", 5, 1) * 4 + "</measure>"
    clarinet = f'<part id="P2"><measure number="1">{attributes(fifths=0)}{_pitched("F", 5) * 4}</measure>{clarinet_bar2}</part>'
    root = build([("P1", "Oboe", oboe), ("P2", "Clarinet", clarinet)])
    report = repair.repair(root, {"transpose": {"Clarinet": "Bb"}, "key_changes": {"2": 0}})
    assert "key change at bar 2 (concert 0): written into 1 part(s), 3 note(s) respelled from the old key" in report
    oboe_bars = root.findall("part")[0].findall("measure")
    assert oboe_bars[1].findtext("attributes/key/fifths") == "0"
    assert [n.findtext("pitch/alter") for n in oboe_bars[1].iter("note")] == [None, None, None, None]
    assert [n.findtext("pitch/alter") for n in oboe_bars[0].iter("note")] == ["-1"] * 4     # before: kept
    clarinet_bar = root.findall("part")[1].findall("measure")[1]
    assert clarinet_bar.findtext("attributes/key/fifths") == "2"                          # read right: kept
    assert [n.findtext("pitch/alter") for n in clarinet_bar.iter("note")] == ["1"] * 4


def test_a_printed_accidental_and_what_it_governs_are_not_respelled():
    flat_key = attributes(fifths=-2)
    bar1 = f'<measure number="1">{flat_key}' + _pitched("C", 5) * 4 + "</measure>"
    bar2 = ('<measure number="2">' + _pitched("E", 5, -1, printed=True)
            + _pitched("E", 5, -1) + _pitched("E", 4, -1) + _pitched("C", 5) + "</measure>")
    root = build([("P1", "Oboe", f'<part id="P1">{bar1}{bar2}</part>')])
    repair.repair(root, {"key_changes": {"2": 0}})
    alters = [n.findtext("pitch/alter") for n in root.findall("part/measure")[1].iter("note")]
    assert alters == ["-1", "-1", None, None]       # E5s printed flat; E4 from the old key


@pytest.mark.parametrize("fifths,step,alter", [(-2, "B", -1), (-2, "E", -1), (-2, "A", 0),
                                                (2, "F", 1), (2, "C", 1), (2, "G", 0), (0, "B", 0)])
def test_key_alter(fifths, step, alter):
    assert repair.key_alter(fifths, step) == alter


def test_a_drum_patch_and_a_patch_that_does_not_fill_the_bar():
    drums = f'<part id="P1">{drum_bar(1, KICK_ONLY, True)}</part>'
    root = build([("P1", "Drum Set", drums)])
    repair.apply_patches(root, {"Drum Set": {"1": {"up": "D5:1 D5:1 B4:1 xA5:1", "down": "r:2 r:1 F4:1"}}})
    assert len(root.find("part/measure").findall("note")) == 7
    root = build([_timed_part("P1", "Oboe", [("C5", 1)] * 4)])
    with pytest.raises(ValueError, match="lasts 7/2"):
        repair.apply_patches(root, {"Oboe": {"1": "1 1 1 1/2"}})
    with pytest.raises(ValueError, match="covers 3 of the voice's 4"):
        repair.apply_patches(build([_timed_part("P1", "Oboe", [("C5", 1)] * 4)]), {"Oboe": {"1": "1 1 2"}})


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
