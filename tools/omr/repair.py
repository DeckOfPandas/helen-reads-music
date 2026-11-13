#!/usr/bin/env python3
"""Mechanical repairs to a merged score, from the piece's merge.json.

USAGE (from the repo root):
    python3 tools/omr/repair.py <in.mxl> <out.mxl> --config library/<piece>/merge.json

process.py runs these after the merge. The optional keys in merge.json:

  "time": "4/4"
      Audiveris can miss a time signature altogether, and a score with none
      imports into a notation program with bars of any length. This writes
      it into every part's first bar that has none, and it is what the
      whole-bar rests below are set to.

  "transpose": {"Clarinet in Bb": "Bb", "Horn in F": "F", "Piccolo": "8va"}
      Audiveris reads written pitch and marks nothing as transposing, so a
      notation program plays and shows a clarinet part a tone too high.
      This adds the <transpose> for each named part (Bb, A, F, Eb, 8va,
      8vb), and gives it its written key: a <key> that holds the concert key
      (the one most non-transposing parts carry) is moved by the interval.
      Audiveris often stamps the concert key onto every staff, even one
      printed with no key signature.

  "key_changes": {"125": 0}
      A key change Audiveris read but could not export (its log shows
      "Error visiting KeyInter ... fifths:null"): concert fifths by bar. Each
      part without a key at that bar gets one (a transposing part its written
      key, from "transpose"), and from that bar to its next key change a note
      whose flat or sharp came only from the old key is respelled in the new.

  "vocal_parts": ["Voice"]
      The parts whose lyrics are lyrics. In every other part a lyric is a
      mark Audiveris misread: dynamics, ^ and v accents (read "A", "V") and
      playing instructions (pizz., arco, bring out) are put back as marks,
      and the rest is dropped and listed. Default: ["Voice"].

  "drum_key": {"F5 x": 51}
      Extra or different drum sounds by stave position and notehead
      ("normal", "x", "slash"; add "open" for the open mark), as General
      MIDI notes. process.py gives every drum note its instrument from
      repair.DRUM_KEY plus these, and puts hands (voice 1) stems up and feet
      (voice 2) stems down.

  "combine": [{"parts": ["Violin I", "Violin II"], "name": "Violins",
               "abbreviation": "Vlns.", "program": 41,
               "labels": {"same": "unis.", "divided": "div.", "first": "1.", "second": "2."}}]
      Two parts onto one part's staves, to save staves (combine_parts).
      process.py runs it after the patches, which name the parts apart.

  "labels": {"<part>": {"abbreviation": "...", "program": 41}}
      Abbreviation and General MIDI program (1-128) for a part, where the
      table in fix_labels_and_sounds does not fit.

  "octave_clefs": false
      The score is printed with no octave clefs (no small 8 above or below a
      clef). Audiveris can read a plain treble clef as "8 below" and put the
      whole stave an octave low; this removes every clef-octave-change and
      moves the notes it governed back. score.py check flags such staves as
      below the instrument's range.

Always, when "time" is known (from the score or merge.json):
  A bar holding nothing but whole-bar rests (<rest measure="yes">) takes
  its length from the time signature. Audiveris gives such a rest the bar
  length it inferred from the other staves, so one misread note elsewhere
  makes every silent staff's bar wrong too. A staff of a multi-staff part
  with no rest in such a bar gets one: Audiveris often writes the rest on
  the top staff only.

  "drum_grooves": {"part": "Drum Set", "grooves": {"A": {
        "up": "xG5:1 xG5:1/2 xG5:1/2 xG5+C5:1 xG5:1/2 xG5:1/2",
        "down": "F4:1 r:1/2 F4:1/2 r:1 r:1"}}}
      Audiveris misses x-headed notes above the stave (hi-hats, cymbals) and
      misreads drum rhythms, but a kit part is mostly a few grooves. Each
      groove is written once, a voice as space-separated <positions>:<length>
      (positions like F4, xG5 for an x notehead, oxG5 for one with the open
      mark of an open hi-hat, cxG5 the + mark, sB4 for a rhythm slash, ~F4
      tied from the note before, joined by + for notes struck together; r
      for a rest; length in crotchets, 1/3 or 2/3 for triplets, g for a
      grace note; then > for an accent, ^ a marcato, ~ to tie on, z a roll,
      l let ring; the rest are in parse_groove_voice). "up2" is a second hands
      voice, for a page that writes one above the first. It goes into the
      bars it lists ("bars": [9, 10]), or else into every bar whose solid
      notes (kick, snare) come in the same order as the groove's. The report
      lists the bars each groove filled. Variations it cannot see (an open
      hi-hat, a crash) are not detected. The part's divisions are rescaled
      to fit the grooves, and its clefs are made percussion clefs.
      "marks": {"9": ["0 mf", "0 Lite time on Hat", "2 Fill"]} beside
      "grooves" writes dynamics and words into bars, at crotchets into the
      bar; with it, the marks Audiveris read are dropped from every bar a
      groove writes, since they belong to the notes it replaced.

  "borrow_rests": false
      Turns off borrow_rests, which process.py otherwise runs last: a short
      bar whose notes repeat a correct bar elsewhere (any part, any bar, same
      note lengths and intervals, so a transposed doubling counts) with only
      rests missing gets that bar's rests. Audiveris drops small rests, and a
      riff played in several parts shows where they go. The report names the
      bar each one copied.

Nothing here changes or adds a played note: other bars with notes in them
are left alone, for score.py check to report.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import xml.etree.ElementTree as ET
import re
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import merge
import score

# Written-to-sounding interval: (diatonic steps, semitones, octaves), and the
# change from concert key to written key in fifths.
TRANSPOSITIONS = {
    "Bb": ((-1, -2, 0), 2),
    "A": ((-2, -3, 0), 3),
    "F": ((-4, -7, 0), 1),
    "Eb": ((-5, -9, 0), 3),
    "8va": ((0, 0, 1), 0),
    "8vb": ((0, 0, -1), 0),
}
# A rest's <type> by its length in crotchets, for the lengths a rest is put back at.
NOTE_TYPES = {Fraction(4): "whole", Fraction(2): "half", Fraction(1): "quarter",
              Fraction(1, 2): "eighth", Fraction(1, 4): "16th", Fraction(1, 8): "32nd"}
# MusicXML's order for <attributes> children, up to the ones written here.
ATTRIBUTE_ORDER = ("footnote", "level", "divisions", "key", "time", "staves",
                   "part-symbol", "instruments", "clef", "staff-details", "transpose")


def _insert_in_order(attributes: ET.Element, child: ET.Element) -> None:
    rank = ATTRIBUTE_ORDER.index(child.tag)
    position = len(attributes)
    for index, existing in enumerate(attributes):
        if existing.tag in ATTRIBUTE_ORDER and ATTRIBUTE_ORDER.index(existing.tag) > rank:
            position = index
            break
    attributes.insert(position, child)


def _first_attributes(part: ET.Element) -> ET.Element | None:
    first = part.find("measure")
    if first is None:
        return None
    attributes = first.find("attributes")
    if attributes is None:
        attributes = ET.Element("attributes")
        position = 0
        while position < len(first) and first[position].tag == "print":
            position += 1
        first.insert(position, attributes)
    return attributes


def write_time(part: ET.Element, time: str) -> bool:
    if part.find(".//time") is not None:
        return False
    attributes = _first_attributes(part)
    if attributes is None:
        return False
    beats, _, beat_type = time.partition("/")
    element = ET.Element("time")
    ET.SubElement(element, "beats").text = beats
    ET.SubElement(element, "beat-type").text = beat_type
    _insert_in_order(attributes, element)
    return True


def _is_rest_only(measure: ET.Element) -> bool:
    notes = measure.findall("note")
    return bool(notes) and all(
        n.find("rest") is not None and n.find("rest").get("measure") == "yes" for n in notes)


def _whole_rests(staves: int, ticks: str) -> list[ET.Element]:
    """A whole-bar rest on each stave, with backups between."""
    out = []
    for staff in range(1, staves + 1):
        if out:
            backup = ET.Element("backup")
            ET.SubElement(backup, "duration").text = ticks
            out.append(backup)
        note = ET.Element("note")
        ET.SubElement(note, "rest", {"measure": "yes"})
        ET.SubElement(note, "duration").text = ticks
        ET.SubElement(note, "voice").text = str(1 + 4 * (staff - 1))
        if staves > 1:
            ET.SubElement(note, "staff").text = str(staff)
        out.append(note)
    return out


def fill_rest_bars(part: ET.Element) -> tuple[int, int, int]:
    """(whole-bar rests set to a full bar, rests added to empty staves, bars
    with nothing in them given a whole-bar rest). An empty bar is one whose
    instrument no fragment covered: the page lost its stave."""
    divisions = None
    expected = None
    staves = 1
    resized = added = emptied = 0
    for measure in part.findall("measure"):
        for attributes in measure.findall("attributes"):
            if attributes.findtext("divisions"):
                divisions = Fraction(attributes.findtext("divisions"))
            if attributes.findtext("staves"):
                staves = int(attributes.findtext("staves"))
            signature = score._time_signature(attributes)
            if signature is not None:
                expected = signature
        if divisions is None or expected is None:
            continue
        ticks = expected * divisions
        if ticks.denominator != 1:
            continue
        ticks = str(ticks.numerator)
        if measure.find("note") is None:
            position = len(measure)
            while position and measure[position - 1].tag == "barline":
                position -= 1
            for offset, element in enumerate(_whole_rests(staves, ticks)):
                measure.insert(position + offset, element)
            emptied += 1
            continue
        if not _is_rest_only(measure):
            continue
        rests = {n.findtext("staff") or "1": n for n in measure.findall("note")}
        for element in list(measure):
            if element.tag in ("note", "backup", "forward"):
                measure.remove(element)
        position = len(measure)
        while position and measure[position - 1].tag == "barline":
            position -= 1
        new = []
        for staff in range(1, staves + 1):
            key = str(staff)
            if key in rests:
                note = rests[key]
                if note.findtext("duration") != ticks:
                    resized += 1
            else:
                note = copy.deepcopy(next(iter(rests.values())))
                for display in note.find("rest").findall("*"):
                    note.find("rest").remove(display)
                note.attrib.pop("default-x", None)
                note.find("voice").text = str(1 + 4 * (staff - 1))
                if staves > 1:
                    staff_element = note.find("staff")
                    if staff_element is None:
                        staff_element = ET.SubElement(note, "staff")
                    staff_element.text = key
                added += 1
            note.find("duration").text = ticks
            if staves > 1 and note.find("staff") is None:
                ET.SubElement(note, "staff").text = key
            if new:
                backup = ET.Element("backup")
                ET.SubElement(backup, "duration").text = ticks
                new.append(backup)
            new.append(note)
        for offset, element in enumerate(new):
            measure.insert(position + offset, element)
    return resized, added, emptied


def fix_octave_clefs(part: ET.Element) -> tuple[int, int]:
    """Strip every clef-octave-change and move the notes it governed back.
    (clefs fixed, notes moved). For a score printed with no octave clefs:
    Audiveris sometimes reads a plain treble clef as "8 below"."""
    shift: dict[str, int] = {}
    clefs = moved = 0
    for measure in part.findall("measure"):
        for element in measure:
            if element.tag == "attributes":
                for clef in element.findall("clef"):
                    staff = clef.get("number", "1")
                    change = clef.find("clef-octave-change")
                    if change is None:
                        shift[staff] = 0
                        continue
                    shift[staff] = -int(change.text or 0)
                    clef.remove(change)
                    clefs += 1
            elif element.tag == "note":
                by = shift.get(element.findtext("staff") or "1", 0)
                if not by:
                    continue
                octave = element.find("pitch/octave")
                if octave is None:
                    octave = element.find("rest/display-octave")
                if octave is not None:
                    octave.text = str(int(octave.text) + by)
                    moved += element.find("pitch") is not None
    return clefs, moved


def fit_rest_voices(part: ET.Element) -> int:
    """In bars with notes, a voice (or staff) holding nothing but one
    whole-bar rest takes the bar's length, and a backup right after it moves
    by the same amount; a backup left dangling at the end of the bar goes.
    Audiveris gives such a rest the bar length it inferred elsewhere: a
    piano's silent left hand, a marimba's empty second voice. Returns how
    many rests it resized."""
    divisions = expected = None
    resized = 0
    for measure in part.findall("measure"):
        for attributes in measure.findall("attributes"):
            if attributes.findtext("divisions"):
                divisions = Fraction(attributes.findtext("divisions"))
            signature = score._time_signature(attributes)
            if signature is not None:
                expected = signature
        if divisions is None or expected is None or _is_rest_only(measure):
            continue
        ticks = expected * divisions
        if ticks.denominator != 1:
            continue
        notes_by_voice: dict[str, list[ET.Element]] = {}
        for note in measure.findall("note"):
            notes_by_voice.setdefault(note.findtext("voice") or "1", []).append(note)
        elements = list(measure)
        for voice, notes in notes_by_voice.items():
            if len(notes) != 1:
                continue
            rest = notes[0].find("rest")
            if rest is None or rest.get("measure") != "yes":
                continue
            old = Fraction(notes[0].findtext("duration") or 0)
            if old == ticks:
                continue
            notes[0].find("duration").text = str(ticks.numerator)
            resized += 1
            following = elements[elements.index(notes[0]) + 1:]
            nxt = next((e for e in following if e.tag in ("note", "backup", "forward")), None)
            if nxt is not None and nxt.tag == "backup":
                later = following[following.index(nxt) + 1:]
                if not any(e.tag in ("note", "forward") for e in later):
                    measure.remove(nxt)
                else:
                    nxt.find("duration").text = str(int(Fraction(nxt.findtext("duration")) + ticks - old))
    return resized


SHARPS_ORDER = "FCGDAEB"


def key_alter(fifths: int, step: str) -> int:
    """The alteration a key signature of `fifths` gives a step."""
    if fifths > 0:
        return 1 if step in SHARPS_ORDER[:fifths] else 0
    if fifths < 0:
        return -1 if step in SHARPS_ORDER[::-1][:-fifths] else 0
    return 0


def apply_key_change(part: ET.Element, bar: str, fifths: int) -> tuple[bool, int]:
    """Write a key change into `part` at `bar` unless it already has a key
    there, and respell the notes from that bar to the part's next key change:
    a note whose alteration came only from the old key (no printed accidental,
    none earlier in the bar on that step and octave) takes the new key's.
    (key written, notes respelled)."""
    measures = part.findall("measure")
    numbers = [m.get("number") for m in measures]
    if bar not in numbers:
        return False, 0
    start = numbers.index(bar)
    old = None
    for measure in measures[:start]:
        for key in measure.iter("key"):
            if key.findtext("fifths") not in (None, "null"):
                old = int(key.findtext("fifths"))
    if measures[start].find("attributes/key") is not None:
        return False, 0
    attributes = measures[start].find("attributes")
    if attributes is None:
        attributes = ET.Element("attributes")
        position = 0
        while position < len(measures[start]) and measures[start][position].tag == "print":
            position += 1
        measures[start].insert(position, attributes)
    key = ET.Element("key")
    ET.SubElement(key, "fifths").text = str(fifths)
    _insert_in_order(attributes, key)
    respelled = 0
    for measure in measures[start:]:
        if measure is not measures[start] and measure.find("attributes/key") is not None:
            break
        printed: dict[tuple[str, str], int] = {}
        for note in measure.iter("note"):
            pitch = note.find("pitch")
            if pitch is None:
                continue
            step, octave = pitch.findtext("step"), pitch.findtext("octave")
            alter = int(Fraction(pitch.findtext("alter") or 0))
            if note.find("accidental") is not None:
                printed[(step, octave)] = alter
                continue
            if (step, octave) in printed or old is None:
                continue
            if alter == key_alter(old, step) and alter != key_alter(fifths, step):
                new = key_alter(fifths, step)
                element = pitch.find("alter")
                if new == 0 and element is not None:
                    pitch.remove(element)
                elif new:
                    if element is None:
                        element = ET.Element("alter")
                        pitch.insert(1, element)
                    element.text = str(new)
                respelled += 1
    return True, respelled


def concert_fifths(root: ET.Element, transposing: set[str]) -> int | None:
    counts = Counter()
    for part in root.findall("part"):
        if part.get("id") in transposing:
            continue
        for key in part.iter("key"):
            if key.findtext("fifths") is not None:
                counts[int(key.findtext("fifths"))] += 1
    return counts.most_common(1)[0][0] if counts else None


def write_transposition(part: ET.Element, kind: str, concert: int | None) -> int:
    """Adds <transpose>; returns how many <key>s it moved to the written key."""
    (diatonic, chromatic, octave), shift = TRANSPOSITIONS[kind]
    attributes = _first_attributes(part)
    if attributes is not None and attributes.find("transpose") is None:
        element = ET.Element("transpose")
        ET.SubElement(element, "diatonic").text = str(diatonic)
        ET.SubElement(element, "chromatic").text = str(chromatic)
        if octave:
            ET.SubElement(element, "octave-change").text = str(octave)
        _insert_in_order(attributes, element)
    moved = 0
    if shift and concert is not None:
        for key in part.iter("key"):
            fifths = key.find("fifths")
            if fifths is not None and int(fifths.text) == concert:
                fifths.text = str(concert + shift)
                moved += 1
    return moved


def _single_voice_events(measure: ET.Element, divisions: Fraction) -> list[tuple] | None:
    """[("n", crotchets, lowest pitch) | ("r", crotchets, None)] for a bar in
    one voice; chord members fold into their note. None for anything else
    (backups, forwards, grace notes, unpitched notes)."""
    events = []
    for element in measure:
        if element.tag in ("backup", "forward"):
            return None
        if element.tag != "note":
            continue
        if element.find("grace") is not None or element.find("unpitched") is not None:
            return None
        length = Fraction(element.findtext("duration") or 0) / divisions
        if element.find("rest") is not None:
            events.append(("r", length, None))
        elif element.find("chord") is not None:
            if not events or events[-1][0] != "n":
                return None
            kind, held, low = events[-1]
            events[-1] = (kind, held, min(low, score.midi(element)))
        else:
            events.append(("n", length, score.midi(element)))
    return events


def _shape(events: list[tuple]) -> tuple:
    notes = [e for e in events if e[0] == "n"]
    return (tuple(e[1] for e in notes),
            tuple(b[2] - a[2] for a, b in zip(notes, notes[1:])))


def _rests_to_insert(short: list[tuple], donor: list[tuple]) -> list[tuple[int, Fraction]] | None:
    """Where `donor`'s rests go in `short`, if `short` is `donor` with only
    rests missing: [(position in short, rest length)]. Else None."""
    inserts, i = [], 0
    for event in donor:
        if i < len(short) and short[i][0] == event[0] and short[i][1] == event[1]:
            i += 1
        elif event[0] == "r":
            inserts.append((i + len(inserts), event[1]))
        else:
            return None
    return inserts if i == len(short) and inserts else None


def borrow_rests(root: ET.Element, assume: Fraction | None = None) -> list[str]:
    """Fill short bars whose notes repeat a correct bar elsewhere (any part,
    any bar: same note lengths, same intervals, so transposed doublings
    count) but with rests missing, with that bar's rests. Audiveris drops
    small rests; a riff played in several parts, or again later, shows
    where they go. Notes are never changed or added, and nothing is done
    when the matching bars disagree on where the rests go."""
    names = dict(score.part_names(root))
    good: list[tuple[str, str, list[tuple]]] = []
    short: list[tuple[str, ET.Element, list[tuple], Fraction]] = []
    for part in root.findall("part"):
        name = names.get(part.get("id"), part.get("id"))
        bad = {b.number: b for b in score.bad_bars(part, assume)}
        states = merge.attribute_states(part)
        for measure, state in zip(part.findall("measure"), states):
            divisions = _divisions(measure, state)
            if divisions is None:
                continue
            events = _single_voice_events(measure, divisions)
            if not events or not any(e[0] == "n" for e in events):
                continue
            number = measure.get("number") or "?"
            if number not in bad:
                good.append((name, number, events))
            elif bad[number].holds < bad[number].expected:
                short.append((name, measure, events, divisions))
    fixed = []
    for name, measure, events, divisions in short:
        options = {}
        for donor_name, donor_number, donor in good:
            if _shape(donor) != _shape(events):
                continue
            inserts = _rests_to_insert(events, donor)
            if inserts:
                options.setdefault(tuple(inserts), f"{donor_name} bar {donor_number}")
        if len(options) != 1:
            continue
        (inserts, source), = options.items()
        if any((length * divisions).denominator != 1 for _, length in inserts):
            continue
        template = measure.find("note")
        for position, length in inserts:
            rest = ET.Element("note")
            ET.SubElement(rest, "rest")
            ET.SubElement(rest, "duration").text = str((length * divisions).numerator)
            if template.findtext("voice"):
                ET.SubElement(rest, "voice").text = template.findtext("voice")
            if length in NOTE_TYPES:
                ET.SubElement(rest, "type").text = NOTE_TYPES[length]
            if template.findtext("staff"):
                ET.SubElement(rest, "staff").text = template.findtext("staff")
            notes = [n for n in measure.findall("note") if n.find("chord") is None]
            anchor = notes[position] if position < len(notes) else None
            index = list(measure).index(anchor) if anchor is not None else len(measure)
            measure.insert(index, rest)
        fixed.append(f"{name} bar {measure.get('number')}: {len(inserts)} rest(s) put back, as in {source}")
    return fixed


def _divisions(measure: ET.Element, state: dict) -> Fraction | None:
    # A bar can restate divisions; the last before its first note counts.
    found = None
    for element in measure:
        if element.tag == "note":
            break
        if element.tag == "attributes" and element.findtext("divisions"):
            found = Fraction(element.findtext("divisions"))
    if found is not None:
        return found
    element = state.get(("divisions", "1"))
    return Fraction(element.text) if element is not None else None


# --- drum grooves ----------------------------------------------------------------

@dataclass
class DrumHead:
    step: str
    octave: int
    cross: bool = False          # x notehead
    open: bool = False           # the open mark (an open hi-hat)
    slash: bool = False          # a rhythm slash ("play a fill")
    tie_stop: bool = False       # tied from the note before
    stopped: bool = False        # the + mark (a closed hi-hat, a muted triangle)
    pitched: bool = False        # a real pitch (timpani, xylophone), not a stave position
    alter: int = 0               # its sharp (1) or flat (-1)
    sound: int | None = None     # General MIDI drum note, when the caller names the instrument


@dataclass
class DrumEvent:
    heads: list[DrumHead]        # empty: a rest
    length: Fraction
    accent: bool = False         # >
    strong: bool = False         # ^ (marcato)
    tie_start: bool = False      # tied on to the next
    hidden: bool = False         # an invisible rest, padding a voice the page leaves short
    grace: bool = False          # a grace note (a flam's or drag's): no length of its own
    roll: bool = False           # three strokes through the stem
    let_ring: bool = False       # a tie to nothing (l.v.)
    staccato: bool = False
    cue: bool = False            # a cue note: another part's, printed small
    tremolo: bool = False        # a fingered tremolo with the next event


GROOVE_MARKS = ">^~zl.qht"
GROOVE_FLAGS = "xos~cp"


def parse_groove_voice(text: str) -> list[DrumEvent]:
    """"xG5:1 ~oxG5+C5:1/2>~ r:1" -> drum events. Before a position: x an x
    notehead, o the open mark, c the + (closed) mark, s a rhythm slash, ~
    tied from the note before, p a pitch (pF#5, pBb2) rather than a stave
    position. After the length: > an accent, ^ a marcato, ~ tied on, z a
    roll, l let ring, . staccato, q a cue note, h a hidden rest, t a
    fingered tremolo to the next note (both written at twice their
    length). r is a rest. A length of g is a grace note before the next
    note; one with a 3 under it (1/3, 2/3, 1/6) is a triplet."""
    events = []
    for token in text.split():
        heads_text, _, length = token.rpartition(":")
        if not heads_text or not length:
            raise ValueError(f"drum groove token {token!r} is not <positions>:<length>")
        marks = ""
        while length and length[-1] in GROOVE_MARKS:
            marks, length = marks + length[-1], length[:-1]
        grace = length == "g"
        try:
            event = DrumEvent([], Fraction(0) if grace else Fraction(length), ">" in marks, "^" in marks,
                              "~" in marks, hidden="h" in marks, grace=grace, roll="z" in marks,
                              let_ring="l" in marks, staccato="." in marks, cue="q" in marks,
                              tremolo="t" in marks)
            if not grace and event.length <= 0:
                raise ValueError
        except (ValueError, ZeroDivisionError):
            raise ValueError(f"drum groove token {token!r} has no length") from None
        if grace and heads_text == "r":
            raise ValueError(f"drum groove token {token!r}: a grace note cannot be a rest")
        if event.hidden and heads_text != "r":
            raise ValueError(f"drum groove token {token!r}: only a rest can be hidden")
        if heads_text != "r":
            for head in heads_text.split("+"):
                event.heads.append(_parse_head(head))
        events.append(event)
    for index, event in enumerate(events):
        if event.tremolo and (index + 1 == len(events) or events[index + 1].grace or event.grace):
            raise ValueError(f"drum groove: a tremolo (t) needs a next note, event {index + 1}")
    return events


def _parse_head(text: str) -> DrumHead:
    flags, head = "", text
    while head and head[0] in GROOVE_FLAGS:
        flags, head = flags + head[0], head[1:]
    alter = 0
    if len(head) > 2 and head[1] in "#b":
        alter, head = (1 if head[1] == "#" else -1), head[0] + head[2:]
    if len(head) < 2 or head[0] not in score.STEPS or not head[1:].isdigit() or (alter and "p" not in flags):
        raise ValueError(f"drum groove position {text!r} is not like G5, xG5, oxG5 or pF#5")
    return DrumHead(head[0], int(head[1:]), "x" in flags, "o" in flags, "s" in flags, "~" in flags,
                    "c" in flags, "p" in flags, alter)


def _span(events: list[DrumEvent]) -> Fraction:
    return sum((e.length for e in events), Fraction(0))


def _solid_sequence(measure: ET.Element) -> list[str]:
    """Stave positions of the solid-headed drum notes, in reading order: the
    kick and snare Audiveris reads, without the x-headed cymbals it misses.
    Order, not timing, since its timing is what it gets wrong."""
    hits = []
    cursor = last = Fraction(0)
    for element in measure:
        if element.tag in ("backup", "forward"):
            length = Fraction(element.findtext("duration") or 0)
            cursor += -length if element.tag == "backup" else length
            continue
        if element.tag != "note":
            continue
        onset = last if element.find("chord") is not None else cursor
        if element.find("chord") is None:
            last = cursor
            cursor += Fraction(element.findtext("duration") or 0)
        if element.find("unpitched") is not None and element.findtext("notehead") != "x":
            hits.append((onset, element.findtext("unpitched/display-step") +
                         element.findtext("unpitched/display-octave")))
    return [position for _, position in sorted(hits)]


def _groove_solid_sequence(voices: list[list[DrumEvent]]) -> list[str]:
    hits = []
    for events in voices:
        onset = Fraction(0)
        for event in events:
            hits += [(onset, f"{h.step}{h.octave}") for h in event.heads if not h.cross and not h.slash]
            onset += event.length
    return [position for _, position in sorted(hits)]


def _tuplet_ends(events: list[DrumEvent]) -> dict[int, str]:
    """Where each triplet bracket starts and stops, by event index. A bracket
    runs from a triplet length until the lengths since it add up to a plain
    value again (three triplet quavers, or a triplet crotchet and quaver)."""
    ends: dict[int, str] = {}
    start, total = None, Fraction(0)
    for index, event in enumerate(events):
        if event.grace:
            continue
        if start is None:
            if event.length.denominator % 3:
                continue
            start, total = index, Fraction(0)
        total += event.length
        if total.denominator % 3:
            ends[start], ends[index] = "start", "stop"
            start = None
    if start is not None:
        raise ValueError(f"drum groove triplet from event {start + 1} never completes a beat")
    return ends


def _drum_notes(events: list[DrumEvent], voice: str, stem: str, divisions: Fraction,
                instrument: dict[int, str] | None = None) -> list[ET.Element]:
    """The <note>s of one voice. `instrument` maps a head's General MIDI
    sound to the part's <score-instrument> id, for heads that name one."""
    notes = []
    tuplets = _tuplet_ends(events)
    for position, event in enumerate(events):
        ticks = event.length * divisions
        if ticks.denominator != 1:
            raise ValueError(f"drum groove length {event.length} does not fit divisions {divisions}")
        triplet = False
        tremolo = ("start" if event.tremolo else
                   "stop" if position and events[position - 1].tremolo else None)
        if event.grace:
            kind, dots = "eighth", 0
        else:
            try:
                kind, dots, triplet = _length_spelling(event.length * (2 if tremolo else 1))
            except ValueError:
                kind, dots = None, 0                         # padding of an odd length: no type
        for index, head in enumerate(event.heads or [None]):
            note = ET.Element("note", {"print-object": "no"} if event.hidden and head is None else {})
            if event.grace:
                ET.SubElement(note, "grace", {"slash": "yes"})
            elif event.cue:
                ET.SubElement(note, "cue")
            if index:
                ET.SubElement(note, "chord")
            if head is None:
                ET.SubElement(note, "rest")
            elif head.pitched:
                pitch = ET.SubElement(note, "pitch")
                ET.SubElement(pitch, "step").text = head.step
                if head.alter:
                    ET.SubElement(pitch, "alter").text = str(head.alter)
                ET.SubElement(pitch, "octave").text = str(head.octave)
            else:
                unpitched = ET.SubElement(note, "unpitched")
                ET.SubElement(unpitched, "display-step").text = head.step
                ET.SubElement(unpitched, "display-octave").text = str(head.octave)
            if not event.grace:
                ET.SubElement(note, "duration").text = str(ticks.numerator)
            ties = [] if head is None else (["stop"] if head.tie_stop else []) + (["start"] if event.tie_start else [])
            for kind_of_tie in ties:
                ET.SubElement(note, "tie", {"type": kind_of_tie})
            if head is not None and instrument and head.sound in instrument:
                ET.SubElement(note, "instrument", {"id": instrument[head.sound]})
            ET.SubElement(note, "voice").text = voice
            if kind:
                ET.SubElement(note, "type").text = kind
            for _ in range(dots):
                ET.SubElement(note, "dot")
            if head is not None and head.pitched and head.alter:
                ET.SubElement(note, "accidental").text = "sharp" if head.alter > 0 else "flat"
            if triplet or tremolo:
                modification = ET.SubElement(note, "time-modification")
                ET.SubElement(modification, "actual-notes").text = "3" if triplet else "2"
                ET.SubElement(modification, "normal-notes").text = "2" if triplet else "1"
            notations = ET.Element("notations")
            if index == 0 and position in tuplets:
                ET.SubElement(notations, "tuplet", {"type": tuplets[position], "bracket": "yes"})
            if head is not None:
                if not head.pitched:
                    ET.SubElement(note, "stem").text = stem
                if head.cross or head.slash:
                    ET.SubElement(note, "notehead").text = "slash" if head.slash else "x"
                for kind_of_tie in ties:
                    ET.SubElement(notations, "tied", {"type": kind_of_tie})
                if event.let_ring:
                    ET.SubElement(notations, "tied", {"type": "let-ring"})
                if head.open or head.stopped:
                    ET.SubElement(ET.SubElement(notations, "technical"), "open" if head.open else "stopped")
                if index == 0 and (event.roll or tremolo):
                    ET.SubElement(ET.SubElement(notations, "ornaments"), "tremolo",
                                  {"type": tremolo or "single"}).text = "3"
                if index == 0 and (event.accent or event.strong or event.staccato):
                    articulations = ET.SubElement(notations, "articulations")
                    if event.accent:
                        ET.SubElement(articulations, "accent")
                    if event.strong:
                        ET.SubElement(articulations, "strong-accent", {"type": "up" if stem == "up" else "down"})
                    if event.staccato:
                        ET.SubElement(articulations, "staccato")
            if len(notations):
                note.append(notations)
            notes.append(note)
    return notes


DRUM_VOICES = (("up", "1", "up"), ("down", "2", "down"), ("up2", "3", "up"))


def _write_drum_voices(measure: ET.Element, up: list[DrumEvent], down: list[DrumEvent],
                       divisions: Fraction, up2: list[DrumEvent] | None = None,
                       instrument: dict[int, str] | None = None) -> None:
    """Replace a drum bar's notes: hands in voice 1 (stems up), feet in voice
    2 (stems down), and a second hands line, where the page has one, in
    voice 3 (stems up). Any may be empty. `instrument` as in _drum_notes."""
    for element in [e for e in measure if e.tag in ("note", "backup", "forward")]:
        measure.remove(element)
    new: list[ET.Element] = []
    for events, (_, voice, stem) in zip((up, down, up2 or []), DRUM_VOICES):
        if not events:
            continue
        if new:
            back = ET.Element("backup")
            ET.SubElement(back, "duration").text = str((previous * divisions).numerator)
            new.append(back)
        new += _drum_notes(events, voice, stem, divisions, instrument)
        previous = _span(events)
    position = len(measure)
    while position and measure[position - 1].tag == "barline":
        position -= 1
    for offset, element in enumerate(new):
        measure.insert(position + offset, element)


def _wrap(time: ET.Element) -> ET.Element:
    attributes = ET.Element("attributes")
    attributes.append(copy.deepcopy(time))
    return attributes


def fill_drum_grooves(root: ET.Element, config: dict, expected: Fraction | None) -> list[str]:
    """Write whole grooves into drum bars. Audiveris misses x-headed notes
    above the stave (hi-hats, cymbals) and misreads drum rhythms, but a kit
    part is mostly a few grooves repeated. Each groove is given once, as an
    "up" voice (hands) and a "down" voice (feet); it goes into the bars it
    lists, or, with no list, every bar whose solid-headed notes (the kick
    and snare Audiveris does read) come in the same order as the groove's.
    Variations it cannot see (an open hi-hat, a crash) are not detected."""
    names = {name: pid for pid, name in score.part_names(root)}
    part_name = config.get("part", "Drum Set")
    if part_name not in names:
        raise ValueError(f"drum_grooves names no part {part_name!r}")
    part = next(p for p in root.findall("part") if p.get("id") == names[part_name])
    grooves = {}
    for name, spec in (config.get("grooves") or {}).items():
        voices = [parse_groove_voice(spec.get(key, "")) for key, _, _ in DRUM_VOICES]
        for (label, _, _), events in zip(DRUM_VOICES, voices):
            total = _span(events)
            if events and expected is not None and total != expected:
                raise ValueError(f"drum groove {name} {label} lasts {total} crotchets, the bar {expected}")
        bars = {str(b) for b in spec["bars"]} if "bars" in spec else None
        grooves[name] = (voices, bars, _groove_solid_sequence(voices))
    marks = {str(bar): [_parse_mark(text) for text in texts]
             for bar, texts in (config.get("marks") or {}).items() if not str(bar).startswith("_")}

    _fit_divisions(part, [v for voices, _, _ in grooves.values() for v in voices])
    _percussion_clefs(part)
    filled: dict[str, list[str]] = {name: [] for name in grooves}
    states = merge.attribute_states(part)
    by_number = {}
    for measure, state in zip(part.findall("measure"), states):
        number = measure.get("number") or "?"
        by_number[number] = (measure, state)
        chosen = [n for n, (_, bars, _) in grooves.items() if bars is not None and number in bars]
        if not chosen:
            sequence = _solid_sequence(measure)
            chosen = [n for n, (_, bars, solid) in grooves.items()
                      if bars is None and sequence and sequence == solid]
        if len(chosen) != 1:
            continue
        divisions = _divisions(measure, state)
        if divisions is None:
            continue
        voices = grooves[chosen[0]][0]
        own = [score._time_signature(a) for a in measure.findall("attributes")]
        own = [t for t in own if t is not None]
        in_force = own[-1] if own else (score._time_signature(_wrap(state[("time", "1")]))
                                        if ("time", "1") in state else expected)
        if in_force is not None and _span(next(v for v in voices if v)) != in_force:
            continue
        _write_drum_voices(measure, *voices[:2], divisions, voices[2])
        filled[chosen[0]].append(number)
        if marks:
            for direction in measure.findall("direction"):
                measure.remove(direction)
    report = [f"{part_name}: groove {name} written into {len(bars)} bar(s): {', '.join(bars) or 'none'}"
              for name, bars in filled.items()]
    for number, bar_marks in marks.items():
        if number not in by_number:
            raise ValueError(f"drum_grooves marks: {part_name} has no bar {number}")
        measure, state = by_number[number]
        for offset, text in bar_marks:
            _place_mark(measure, offset, text, _divisions(measure, state))
    if marks:
        report.append(f"{part_name}: marks written into {len(marks)} bar(s); the read's own marks "
                      f"dropped from every bar a groove wrote")
    return report


DYNAMICS = {"ppp", "pp", "p", "mp", "mf", "f", "ff", "fff", "sf", "sfz", "sffz", "fz", "fp", "sfp", "rf", "rfz"}


def _parse_mark(text: str) -> tuple[Fraction, str]:
    """"2 Fill" -> (2, "Fill"): crotchets into the bar, then the words or
    the dynamic."""
    offset, _, words = text.strip().partition(" ")
    try:
        at = Fraction(offset)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"drum mark {text!r} is not '<crotchets into the bar> <text>'") from None
    if not words.strip() or at < 0:
        raise ValueError(f"drum mark {text!r} is not '<crotchets into the bar> <text>'")
    return at, words.strip()


def _place_mark(measure: ET.Element, offset: Fraction, text: str, divisions: Fraction | None) -> None:
    """A dynamic below the stave, or words above it, at `offset` crotchets:
    just before the first note that starts there, or with an <offset> from
    the bar's start when none does."""
    direction = ET.Element("direction", {"placement": "below" if text in DYNAMICS else "above"})
    kind = ET.SubElement(direction, "direction-type")
    if text in DYNAMICS:
        ET.SubElement(ET.SubElement(kind, "dynamics"), text)
    else:
        ET.SubElement(kind, "words").text = text
    cursor, anchor = Fraction(0), None
    for element in measure:
        if element.tag in ("backup", "forward"):
            length = Fraction(element.findtext("duration") or 0)
            cursor += -length if element.tag == "backup" else length
        elif element.tag == "note" and element.find("chord") is None:
            if anchor is None and divisions and cursor == offset * divisions:
                anchor = element
            cursor += Fraction(element.findtext("duration") or 0)
    if anchor is None:
        anchor = next((e for e in measure if e.tag in ("note", "backup", "forward")), None)
        if offset and divisions:
            ticks = offset * divisions
            ET.SubElement(direction, "offset").text = str(int(ticks)) if ticks.denominator == 1 else str(float(ticks))
    measure.insert(list(measure).index(anchor) if anchor is not None else len(measure), direction)


def _fit_divisions(part: ET.Element, voices: list[list[DrumEvent]]) -> None:
    """Rescale `part` so every length in `voices` is a whole number of
    divisions: a semiquaver needs 4 to the crotchet, a triplet 3."""
    import math
    need = math.lcm(1, *(e.length.denominator for events in voices for e in events))
    have = {int(Fraction(d.text)) for d in part.iter("divisions") if d.text}
    if any(value % need for value in have):
        unify_divisions([part], also=need)


def _percussion_clefs(part: ET.Element) -> None:
    """A kit stave has only a percussion clef: Audiveris reads it on a scan
    as treble or alto now and then, and can leave the first bar with none."""
    for clef in part.iter("clef"):
        for child in [c for c in clef if c.tag != "sign"]:      # line, clef-octave-change
            clef.remove(child)
        sign = clef.find("sign")
        if sign is None:
            sign = ET.SubElement(clef, "sign")
        sign.text = "percussion"
    first = part.find("measure")
    if first is None or first.find("attributes/clef") is not None:
        return
    attributes = first.find("attributes")
    if attributes is None:
        attributes = ET.Element("attributes")
        first.insert(next((i for i, e in enumerate(first) if e.tag != "print"), len(first)), attributes)
    clef = ET.Element("clef")
    ET.SubElement(clef, "sign").text = "percussion"
    before = ("divisions", "key", "time", "staves", "part-symbol", "instruments")
    index = max((i + 1 for i, e in enumerate(attributes) if e.tag in before), default=0)
    attributes.insert(index, clef)


# --- drum voicing ----------------------------------------------------------------

# A drum note's sound by its stave position and notehead, after the standard
# (PAS) drum key, which this kind of score follows: General MIDI note numbers.
DRUM_KEY = {
    ("F4", "normal"): (36, "Bass Drum"),
    ("D4", "x"): (44, "Pedal Hi-Hat"),
    ("C5", "normal"): (38, "Snare"),
    ("G5", "x"): (42, "Closed Hi-Hat"),
    ("G5", "x", "open"): (46, "Open Hi-Hat"),
    ("A5", "x"): (49, "Crash Cymbal"),
    ("C6", "x"): (49, "Crash Cymbal"),           # some engravers put it on the second ledger
    ("F5", "x"): (51, "Ride Cymbal"),
    ("F5", "normal"): (50, "High Tom"),
    ("E5", "normal"): (50, "High Tom"),
    ("D5", "normal"): (48, "Mid Tom"),
    ("B4", "normal"): (45, "Low Tom"),
    ("A4", "normal"): (43, "Floor Tom"),
    ("G4", "normal"): (41, "Low Floor Tom"),
}
FEET = {36, 44}
# General MIDI percussion names, for sounds merge.json "drum_key" adds by number.
GM_DRUMS = {
    35: "Acoustic Bass Drum", 36: "Bass Drum", 37: "Side Stick", 38: "Snare", 39: "Hand Clap",
    40: "Electric Snare", 41: "Low Floor Tom", 42: "Closed Hi-Hat", 43: "Floor Tom", 44: "Pedal Hi-Hat",
    45: "Low Tom", 46: "Open Hi-Hat", 47: "Low-Mid Tom", 48: "Mid Tom", 49: "Crash Cymbal", 50: "High Tom",
    51: "Ride Cymbal", 52: "Chinese Cymbal", 53: "Ride Bell", 54: "Tambourine", 55: "Splash Cymbal",
    56: "Cowbell", 57: "Crash Cymbal 2", 58: "Vibraslap", 59: "Ride Cymbal 2", 60: "Hi Bongo",
    61: "Low Bongo", 62: "Mute Hi Conga", 63: "Open Hi Conga", 64: "Low Conga", 65: "High Timbale",
    66: "Low Timbale", 67: "High Agogo", 68: "Low Agogo", 69: "Cabasa", 70: "Maracas", 71: "Short Whistle",
    72: "Long Whistle", 73: "Short Guiro", 74: "Long Guiro", 75: "Claves", 76: "Hi Wood Block",
    77: "Low Wood Block", 78: "Mute Cuica", 79: "Open Cuica", 80: "Mute Triangle", 81: "Open Triangle",
}


def _drum_key(note: ET.Element) -> tuple:
    position = note.findtext("unpitched/display-step") + note.findtext("unpitched/display-octave")
    head = note.findtext("notehead") or "normal"
    opened = note.find("notations/technical/open") is not None
    return (position, head, "open") if opened else (position, head)


def voice_drum_parts(root: ET.Element, extra_key: dict | None = None) -> list[str]:
    """For every part with unpitched notes: give each note the instrument its
    stave position and notehead stand for (DRUM_KEY, plus merge.json
    "drum_key" entries like {"F5 x": 51}), so a notation program plays and
    places it as that drum; declare exactly the instruments used, numbered
    as MusicXML wants (midi-unpitched is the General MIDI note + 1); and
    point voice 1's stems up and voice 2's down. Rhythm slashes get no
    instrument. Returns report lines, naming any note the key does not
    cover."""
    key = dict(DRUM_KEY)
    for spec, number in (extra_key or {}).items():
        if spec.startswith("_"):
            continue
        parts_of_spec = spec.split()
        key[tuple(parts_of_spec) if len(parts_of_spec) > 1 else (parts_of_spec[0], "normal")] = \
            (int(number), GM_DRUMS.get(int(number), spec))
    report = []
    names = dict(score.part_names(root))
    score_parts = {sp.get("id"): sp for sp in root.iter("score-part")}
    for part in root.findall("part"):
        if part.find(".//unpitched") is None:
            continue
        pid = part.get("id")
        used: dict[int, str] = {}
        unknown = Counter()
        assigned = 0
        for note in part.iter("note"):
            voice = note.findtext("voice") or "1"
            if note.find("unpitched") is None:
                continue
            stem = note.find("stem")
            if voice in ("1", "2"):
                if stem is None:
                    stem = ET.Element("stem")
                    _place(note, stem)
                stem.text = "up" if voice == "1" else "down"
            for old in note.findall("instrument"):
                note.remove(old)
            if (note.findtext("notehead") or "") == "slash":
                continue
            found = key.get(_drum_key(note)) or key.get(_drum_key(note)[:2])
            if found is None:
                unknown[" ".join(_drum_key(note))] += 1
                continue
            number, label = found
            used[number] = label
            _place(note, ET.Element("instrument", {"id": f"{pid}-I{number}"}))
            assigned += 1
        score_part = score_parts[pid]
        for old in score_part.findall("score-instrument") + score_part.findall("midi-instrument"):
            score_part.remove(old)
        position = max((i + 1 for i, child in enumerate(score_part)
                        if child.tag in ("part-name", "part-name-display", "part-abbreviation",
                                         "part-abbreviation-display", "group")), default=len(score_part))
        new = []
        for number in sorted(used):
            instrument = ET.Element("score-instrument", {"id": f"{pid}-I{number}"})
            ET.SubElement(instrument, "instrument-name").text = used[number]
            new.append(instrument)
        for number in sorted(used):
            midi = ET.Element("midi-instrument", {"id": f"{pid}-I{number}"})
            ET.SubElement(midi, "midi-channel").text = "10"
            ET.SubElement(midi, "midi-unpitched").text = str(number + 1)
            new.append(midi)
        for offset, element in enumerate(new):
            score_part.insert(position + offset, element)
        line = (f"{names.get(pid, pid)}: {assigned} drum note(s) given their instrument "
                f"({', '.join(used[n] for n in sorted(used))}); voice 1 stems up, voice 2 down")
        if unknown:
            line += f"; NOT IN THE DRUM KEY: {dict(unknown)}"
        report.append(line)
    return report


# --- two parts on one stave ------------------------------------------------------

def _staff_streams(measure: ET.Element, staff: int, staves: int) -> list[list[ET.Element]]:
    """The notes and forwards on one staff of a bar, one list per voice, in order."""
    streams: dict[str, list[ET.Element]] = {}
    voice = None
    for element in measure:
        if element.tag not in ("note", "forward"):
            continue
        on = int(element.findtext("staff") or 1) if staves > 1 else 1
        if element.tag == "note" and on != staff:
            continue
        voice = element.findtext("voice") or voice or "1"
        if element.tag == "forward" and staves > 1 and element.findtext("staff") and on != staff:
            continue
        streams.setdefault(voice, []).append(element)
    return list(streams.values())


def _stream_signature(streams: list[list[ET.Element]], divisions: Fraction) -> list:
    """Per stream, its chords in time order, each (sorted pitches, length):
    the order a chord's notes are written in does not matter."""
    out = []
    for stream in streams:
        chords: list[tuple[list, Fraction]] = []
        for e in stream:
            what = "r" if e.find("rest") is not None else score.midi(e) if e.tag == "note" else "fwd"
            if e.find("chord") is not None and chords:
                chords[-1][0].append(what)
            else:
                chords.append(([what], Fraction(e.findtext("duration") or 0) / divisions))
        out.append([(sorted(map(str, pitches)), length) for pitches, length in chords])
    return out


def _silent(streams: list[list[ET.Element]]) -> bool:
    return all(e.tag != "note" or e.find("rest") is not None for stream in streams for e in stream)


def _stream_span(stream: list[ET.Element]) -> Fraction:
    return sum((Fraction(e.findtext("duration") or 0) for e in stream
                if e.find("chord") is None and e.find("grace") is None), Fraction(0))


def _copy_stream(stream: list[ET.Element], voice: str, staff: int | None, staves: int,
                 scale: Fraction, stem: str | None) -> list[ET.Element]:
    out = []
    for element in stream:
        element = copy.deepcopy(element)
        duration = element.find("duration")
        if duration is not None:
            ticks = Fraction(duration.text) * scale
            if ticks.denominator != 1:
                raise ValueError("the two parts count time in incompatible divisions")
            duration.text = str(ticks.numerator)
        target = element.find("voice")
        if target is None:
            target = ET.Element("voice")
            if element.tag == "note":
                _place(element, target)
            else:
                element.append(target)
        target.text = voice
        if element.tag == "note":
            if staves > 1:
                on = element.find("staff")
                if on is None:
                    on = ET.Element("staff")
                    _place(element, on)
                on.text = str(staff)
            if stem and element.find("rest") is None:
                old = element.find("stem")
                if old is None:
                    old = ET.Element("stem")
                    _place(element, old)
                old.text = stem
        out.append(element)
    return out


def unify_divisions(parts: list[ET.Element], also: int = 1) -> int:
    """Rewrite `parts` to count time in one divisions value, the least common
    multiple of every one they use and `also`, scaling every duration to it.
    Returns it."""
    import math
    values = {int(Fraction(d.text)) for p in parts for d in p.iter("divisions") if d.text}
    target = math.lcm(also, *values) if values else also
    for part in parts:
        current = Fraction(target)
        for measure in part.findall("measure"):
            for element in measure:
                if element.tag == "attributes":
                    divisions = element.find("divisions")
                    if divisions is not None and divisions.text:
                        current = Fraction(divisions.text)
                        divisions.text = str(target)
                    continue
                duration = element.find("duration")
                if duration is not None and duration.text:
                    value = Fraction(duration.text) * target / current
                    duration.text = str(int(value)) if value.denominator == 1 else str(float(value))
    return target


def combine_parts(root: ET.Element, spec: dict) -> list[str]:
    """Put two parts on one part's staves: {"parts": [A, B], "name": ...,
    "abbreviation": ..., "labels": {"same": "unis.", "divided": "div.",
    "first": "Hp.", "second": "Pno."}}. Bar by bar and staff by staff:
    the same notes are written once; if one part rests, the other's notes
    are written; if both play different notes, A goes in the upper voice
    (stems up) and B in a lower one (stems down). A label, where given, is
    written as text where the state changes. B's part is removed."""
    first, second = spec["parts"]
    previous: list[ET.Element] = []
    names = {name: pid for pid, name in score.part_names(root)}
    for name in (first, second):
        if name not in names:
            raise ValueError(f"combine names no part {name!r}")
        sharing = [pid for pid, other in score.part_names(root) if other == name]
        if len(sharing) > 1:
            raise ValueError(f"combine: {len(sharing)} parts are named {name!r} ({', '.join(sharing)}): "
                             "place or drop the stray one in merge.json")
    parts = {p.get("id"): p for p in root.findall("part")}
    a, b = parts[names[first]], parts[names[second]]
    unify_divisions([a, b])          # else a duration may not copy across exactly
    a_states, b_states = merge.attribute_states(a), merge.attribute_states(b)
    b_bars = {m.get("number"): (m, s) for m, s in zip(b.findall("measure"), b_states)}
    labels = spec.get("labels") or {}
    counts = Counter()
    last_state = None
    for measure, a_state in zip(a.findall("measure"), a_states):
        number = measure.get("number")
        if number not in b_bars:
            continue
        b_measure, b_state = b_bars[number]
        a_div, b_div = _divisions(measure, a_state), _divisions(b_measure, b_state)
        # A bar with no divisions in force holds nothing to scale: use the other's.
        a_div, b_div = a_div or b_div or Fraction(1), b_div or a_div or Fraction(1)
        staves = int(measure.findtext("attributes/staves") or (a_state[("staves", "1")].text
                     if ("staves", "1") in a_state else 1))
        streams: list[list[ET.Element]] = []          # each starts at the bar's start
        states = []
        try:
            for staff in range(1, staves + 1):
                mine = _staff_streams(measure, staff, staves)
                theirs = _staff_streams(b_measure, staff, staves)
                base = 1 + 4 * (staff - 1)
                if _stream_signature(mine, a_div) == _stream_signature(theirs, b_div) or _silent(theirs):
                    chosen = [(s, None, Fraction(1)) for s in mine]
                    states.append("silent" if _silent(mine) and _silent(theirs) else
                                  "first" if _silent(theirs) else "same")
                elif _silent(mine):
                    chosen = [(s, None, a_div / b_div) for s in theirs]
                    states.append("second")
                else:
                    chosen = ([(s, "up", Fraction(1)) for s in mine]
                              + [(s, "down", a_div / b_div) for s in theirs])
                    states.append("divided")
                for index, (stream, stem, scale) in enumerate(chosen):
                    streams.append(_copy_stream(stream, str(base + index), staff, staves, scale, stem))
        except ValueError as err:
            raise ValueError(f"combine {first} + {second} bar {number}: {err}") from err
        played = [s for s in states if s != "silent"]
        if "divided" in played or ("first" in played and "second" in played):
            state = "divided"
        elif "same" in played:                          # both play on some staff
            state = "same"
        elif "second" in played:
            state = "second"
        elif "first" in played:
            state = "first"
        else:
            state = "same" if played else "silent"
        counts[state] += 1
        new: list[ET.Element] = []
        for stream in streams:
            if new:
                backup = ET.Element("backup")
                ET.SubElement(backup, "duration").text = str(int(_stream_span(previous)))
                new.append(backup)
            new += stream
            previous = stream
        for element in [e for e in measure if e.tag in ("note", "backup", "forward")]:
            measure.remove(element)
        position = len(measure)
        while position and measure[position - 1].tag == "barline":
            position -= 1
        for offset, element in enumerate(new):
            measure.insert(position + offset, element)
        if state in ("second", "divided"):            # B's dynamics and instructions come too
            start = 0
            while start < len(measure) and measure[start].tag in ("print", "attributes"):
                start += 1
            for direction in b_measure.findall("direction"):
                if direction.find(".//dynamics") is not None or direction.find(".//words") is not None:
                    measure.insert(start, copy.deepcopy(direction))
        if state != "silent" and state != last_state:
            text = labels.get(state)
            if text and new:
                direction = ET.Element("direction", {"placement": "above"})
                ET.SubElement(ET.SubElement(direction, "direction-type"), "words").text = text
                measure.insert(list(measure).index(new[0]), direction)
            last_state = state
    root.remove(b)
    part_list = root.find("part-list")
    for sp in list(part_list):
        if sp.tag == "score-part" and sp.get("id") == b.get("id"):
            part_list.remove(sp)
        elif sp.tag == "score-part" and sp.get("id") == a.get("id"):
            sp.find("part-name").text = spec.get("name", first)
            abbreviation = sp.find("part-abbreviation")
            if abbreviation is None:
                abbreviation = ET.Element("part-abbreviation")
                sp.insert(list(sp).index(sp.find("part-name")) + 1, abbreviation)
            abbreviation.text = spec.get("abbreviation", spec.get("name", first))
    return [f"{first} + {second} -> {spec.get('name', first)}: {counts['same']} bar(s) the same, "
            f"{counts['first']} only {first}, {counts['second']} only {second}, "
            f"{counts['divided']} in two voices"]


# --- labels and playback sounds -----------------------------------------------

# Abbreviation and General MIDI program (1-128, as MusicXML counts) by a word
# in the part name, first match wins.
INSTRUMENT_TABLE = (
    ("piccolo", "Picc.", 73), ("flute", "Fl.", 74), ("oboe", "Ob.", 69), ("clarinet", "Cl.", 72),
    ("bassoon", "Bsn.", 71), ("horn", "Hn.", 61), ("trumpet", "Tpt.", 57), ("trombone", "Tbn.", 58),
    ("tuba", "Tba.", 59), ("alto sax", "A. Sax.", 66), ("tenor sax", "T. Sax.", 67),
    ("marimba", "Mar.", 13), ("vibraphone", "Vib.", 12), ("harp", "Hp.", 47), ("piano", "Pno.", 1),
    ("guitar", "Gtr.", 25), ("electric bass", "E. Bass", 34), ("bass guitar", "B. Gtr.", 34),
    ("voice", "Voice", 54), ("violins", "Vlns.", 41), ("violin", "Vln.", 41), ("viola", "Vla.", 42),
    ("violoncello", "Vc.", 43), ("cello", "Vc.", 43), ("contrabass", "Cb.", 44), ("double bass", "Cb.", 44),
)


def fix_labels_and_sounds(root: ET.Element, overrides: dict | None = None) -> list[str]:
    """Give each pitched part an abbreviation and a playback sound that fit
    its name. The merge copies them from the first fragment, which Audiveris
    named however it read it (a double bass abbreviated "Voice", everything
    played as "Voice Oohs"). Drum parts keep the instruments voice_drum_parts
    gave them. merge.json "labels": {"<part>": {"abbreviation": ..,
    "program": ..}} overrides the table."""
    overrides = overrides or {}
    fixed = []
    for sp in root.iter("score-part"):
        pid, name = sp.get("id"), (sp.findtext("part-name") or "")
        part = next(p for p in root.findall("part") if p.get("id") == pid)
        if part.find(".//unpitched") is not None:
            continue
        lowered = name.lower()
        row = next((r for r in INSTRUMENT_TABLE if r[0] in lowered), None)
        wanted = overrides.get(name, {})
        abbreviation = wanted.get("abbreviation") or (row[1] if row else None)
        program = wanted.get("program") or (row[2] if row else None)
        numeral = name.split()[-1] if name.split() and name.split()[-1] in ("I", "II", "III") else None
        if numeral and abbreviation and not wanted.get("abbreviation"):
            abbreviation = f"{abbreviation} {numeral}"             # Vln. I, Vln. II
        if abbreviation:
            element = sp.find("part-abbreviation")
            if element is None:
                element = ET.Element("part-abbreviation")
                sp.insert(list(sp).index(sp.find("part-name")) + 1, element)
            element.text = abbreviation
        if program:
            for old in sp.findall("score-instrument") + sp.findall("midi-instrument"):
                sp.remove(old)
            instrument = ET.Element("score-instrument", {"id": f"{pid}-I1"})
            ET.SubElement(instrument, "instrument-name").text = name
            midi = ET.Element("midi-instrument", {"id": f"{pid}-I1"})
            ET.SubElement(midi, "midi-program").text = str(program)
            position = list(sp).index(sp.find("part-abbreviation")) + 1
            sp.insert(position, instrument)
            sp.insert(position + 1, midi)
            for note in part.iter("note"):
                for old in note.findall("instrument"):
                    old.set("id", f"{pid}-I1")
        fixed.append(f"{name} ({abbreviation}, program {program})")
    return [f"labels and playback sounds: {'; '.join(fixed)}"]


# --- marks Audiveris read as lyrics --------------------------------------------

DYNAMICS = ("ppp", "pp", "mp", "mf", "fff", "ff", "fp", "sfz", "sf", "p", "f")
WORDS = {"pizz": "pizz.", "plzz": "pizz.", "arco": "arco", "areo": "arco", "espr": "espr.",
         "solo": "solo", "nasal": "nasal", "dirty": "dirty", "nasty": "nasty", "molto": "molto",
         "sostenuto": "sostenuto", "bring": "bring out", "fill": "fill", "ad": "ad lib."}
SKIP_AFTER = {"bring": "out", "ad": "lib"}


def _direction(note: ET.Element, measure: ET.Element, dynamic: str | None = None,
               words: str | None = None) -> None:
    direction = ET.Element("direction", {"placement": "below" if dynamic else "above"})
    kind = ET.SubElement(direction, "direction-type")
    if dynamic:
        ET.SubElement(ET.SubElement(kind, "dynamics"), dynamic)
    else:
        ET.SubElement(kind, "words").text = words
    for tag in ("voice", "staff"):
        if note.findtext(tag):
            ET.SubElement(direction, tag).text = note.findtext(tag)
    measure.insert(list(measure).index(note), direction)


def marks_from_lyrics(root: ET.Element, vocal: list[str]) -> list[str]:
    """In every part but the vocal ones, turn lyrics that are really marks
    Audiveris misread into the marks: a dynamic ("f", "Mf", "\\"f") into a
    dynamic, "A" or "V" (how it reads ^ and v) into a marcato, ">" into an
    accent, a playing instruction (pizz., arco, bring out, nasty...) into a
    text direction. Every lyric in those parts goes; the ones that were
    none of these are listed, so nothing vanishes unseen."""
    made = Counter()
    dropped = []
    names = dict(score.part_names(root))
    for part in root.findall("part"):
        name = names.get(part.get("id"), "")
        if name in vocal:
            continue
        for measure in part.findall("measure"):
            skip_next = None
            anchor = None
            for note in [n for n in measure.findall("note")]:
                if note.find("chord") is None or anchor is None:
                    anchor = note                 # a direction goes before the chord, not inside it
                for lyric in note.findall("lyric"):
                    text = lyric.findtext("text") or ""
                    note.remove(lyric)
                    letters = re.sub(r"[^a-z]", "", text.lower().replace("1", "i"))
                    if skip_next and letters == skip_next:
                        skip_next = None
                        continue
                    word = next((w for w in WORDS if letters.startswith(w)), None)
                    dynamic, rest = None, letters
                    if word is None:                         # "fdirty": a dynamic, then a word
                        stripped = letters.lstrip("jw")      # OCR noise before a dynamic
                        dynamic = next((d for d in DYNAMICS if stripped.startswith(d)), None)
                        rest = stripped[len(dynamic):] if dynamic else letters
                        word = next((w for w in WORDS if rest.startswith(w)), None) if dynamic else None
                    if dynamic and (not rest or word or rest in DYNAMICS):
                        _direction(anchor, measure, dynamic=dynamic)
                        made["dynamic"] += 1
                    if word:
                        _direction(anchor, measure, words=WORDS[word])
                        made["text"] += 1
                        skip_next = SKIP_AFTER.get(word)
                    elif not dynamic and letters in ("a", "v", "ha", "aa") or (not letters and ">" in text):
                        articulations = note.find("notations/articulations")
                        if articulations is None:
                            notations = note.find("notations")
                            if notations is None:
                                notations = ET.Element("notations")
                                _place(note, notations)
                            articulations = ET.SubElement(notations, "articulations")
                        ET.SubElement(articulations, "accent" if not letters else "strong-accent")
                        made["accent"] += 1
                    elif not (dynamic and (not rest or rest in DYNAMICS)):
                        dropped.append(f"{name} {measure.get('number')} {text!r}")
    report = [f"marks read as lyrics: {made['dynamic']} dynamic(s), {made['accent']} accent(s), "
              f"{made['text']} text direction(s) put back"]
    if dropped:
        report.append(f"  lyrics dropped as unreadable ({len(dropped)}): {'; '.join(dropped)}")
    return report


# --- hand patches ----------------------------------------------------------------

ALTERS = {"b": -1, "#": 1, "n": 0}
NOTE_CHILD_ORDER = ("grace", "cue", "chord", "pitch", "unpitched", "rest", "duration", "tie", "instrument",
                    "voice", "type", "dot", "accidental", "time-modification", "stem", "notehead",
                    "staff", "beam", "notations", "lyric")


def _length_spelling(length: Fraction) -> tuple[str, int, bool]:
    """(type, dots, triplet) that spell `length` crotchets."""
    if length in NOTE_TYPES:
        return NOTE_TYPES[length], 0, False
    if length * 2 / 3 in NOTE_TYPES:
        return NOTE_TYPES[length * 2 / 3], 1, False
    if length * 4 / 7 in NOTE_TYPES:
        return NOTE_TYPES[length * 4 / 7], 2, False
    if length * 3 / 2 in NOTE_TYPES:
        return NOTE_TYPES[length * 3 / 2], 0, True
    raise ValueError(f"no note value lasts {length} crotchets")


def _place(note: ET.Element, child: ET.Element) -> None:
    """Insert `child` among `note`'s children in MusicXML's order."""
    rank = NOTE_CHILD_ORDER.index(child.tag)
    for index, existing in enumerate(note):
        if existing.tag in NOTE_CHILD_ORDER and NOTE_CHILD_ORDER.index(existing.tag) > rank:
            note.insert(index, child)
            return
    note.append(child)


def _set_length(note: ET.Element, length: Fraction, divisions: Fraction) -> None:
    ticks = length * divisions
    if ticks.denominator != 1:
        raise ValueError(f"{length} crotchets is not a whole number of divisions ({divisions})")
    kind, dots, triplet = _length_spelling(length)
    rest = note.find("rest")
    if rest is not None and rest.get("measure") == "yes" and note.findtext("duration") != str(ticks.numerator):
        del rest.attrib["measure"]               # no longer the whole bar
    note.find("duration").text = str(ticks.numerator)
    for tag in ("type", "dot", "time-modification"):
        for old in note.findall(tag):
            note.remove(old)
    if rest is None or rest.get("measure") != "yes":
        _place(note, _text_element("type", kind))
    for _ in range(dots):
        _place(note, ET.Element("dot"))
    if triplet:
        modification = ET.Element("time-modification")
        ET.SubElement(modification, "actual-notes").text = "3"
        ET.SubElement(modification, "normal-notes").text = "2"
        _place(note, modification)


def _text_element(tag: str, text: str) -> ET.Element:
    element = ET.Element(tag)
    element.text = text
    return element


def _parse_pitched(token: str) -> tuple[list[tuple], Fraction, bool, str | None]:
    """"~xBb4+D5:1/2~@Yeah," -> ([(step, alter, octave, x, tie-stop)], length, tie-start, lyric)."""
    lyric = None
    if "@" in token:
        token, lyric = token.split("@", 1)
    tie_start = token.endswith("~")
    token = token.rstrip("~")
    heads, _, length = token.rpartition(":")
    if not heads or not length:
        raise ValueError(f"patch token {token!r} is not <pitches>:<length>")
    if heads == "r":
        return [], Fraction(length), False, None
    pitches = []
    for head in heads.split("+"):
        stop = head.startswith("~")
        head = head.lstrip("~")
        cross = head.startswith("x")
        head = head.lstrip("x")
        if len(head) < 2 or head[0] not in score.STEPS:
            raise ValueError(f"patch pitch {head!r} is not like Bb4, F#5, xB4 or ~G5")
        alter = ALTERS.get(head[1], None) if len(head) > 2 else None
        octave = head[2:] if alter is not None else head[1:]
        if not octave.isdigit():
            raise ValueError(f"patch pitch {head!r} has no octave")
        pitches.append((head[0], alter or 0, int(octave), cross, stop))
    return pitches, Fraction(length), tie_start, lyric


def _build_tremolo(token: str, voice: str, staff: str | None, divisions: Fraction) -> list[ET.Element]:
    """"F4+Bb4/D5:2": a fingered tremolo, the first chord alternating with the
    second for 2 crotchets. Each is written at the full value (here a minim)
    but lasts half of it (time-modification 2:1), joined by a three-stroke
    tremolo, as notation programs write it."""
    left, _, rest = token.partition("/")
    right, _, length = rest.rpartition(":")
    total = Fraction(length)
    kind, dots, triplet = _length_spelling(total)
    if dots or triplet:
        raise ValueError(f"a tremolo of {total} crotchets is not a plain note value")
    notes = []
    for chord, tremolo_type in ((left, "start"), (right, "stop")):
        built = _build_notes(f"{chord}:{total / 2}", voice, staff, divisions)
        for note in built:
            for old in note.findall("type") + note.findall("dot") + note.findall("time-modification"):
                note.remove(old)
            _place(note, _text_element("type", kind))
            modification = ET.Element("time-modification")
            ET.SubElement(modification, "actual-notes").text = "2"
            ET.SubElement(modification, "normal-notes").text = "1"
            _place(note, modification)
        notations = built[0].find("notations")
        if notations is None:
            notations = ET.Element("notations")
            _place(built[0], notations)
        tremolo = ET.SubElement(ET.SubElement(notations, "ornaments"), "tremolo", {"type": tremolo_type})
        tremolo.text = "3"
        notes += built
    return notes


def _build_notes(token: str, voice: str, staff: str | None, divisions: Fraction) -> list[ET.Element]:
    if "/" in token.split(":")[0]:
        return _build_tremolo(token, voice, staff, divisions)
    heads, _, length = token.split("@")[0].rstrip("~").rpartition(":")
    if length == "g":
        # A grace note (an acciaccatura, slashed quaver) takes no time: it has no duration.
        notes = _build_notes(token.replace(":g", ":1/2", 1), voice, staff, divisions)
        for note in notes:
            note.remove(note.find("duration"))
            _place(note, ET.Element("grace", {"slash": "yes"}))
        return notes
    pitches, length, tie_start, lyric = _parse_pitched(token)
    notes = []
    for index, head in enumerate(pitches or [None]):
        note = ET.Element("note")
        if index:
            note.append(ET.Element("chord"))
        if head is None:
            note.append(ET.Element("rest"))
        else:
            step, alter, octave, cross, stop = head
            pitch = ET.SubElement(note, "pitch")
            ET.SubElement(pitch, "step").text = step
            if alter:
                ET.SubElement(pitch, "alter").text = str(alter)
            ET.SubElement(pitch, "octave").text = str(octave)
        note.append(_text_element("duration", "0"))
        _place(note, _text_element("voice", voice))
        if staff:
            _place(note, _text_element("staff", staff))
        _set_length(note, length, divisions)
        if head is not None:
            ties = [kind for kind, wanted in (("stop", head[4]), ("start", tie_start)) if wanted]
            for kind in ties:
                _place(note, ET.Element("tie", {"type": kind}))
            if head[3]:
                _place(note, _text_element("notehead", "x"))
            if ties:
                notations = ET.Element("notations")
                for kind in ties:
                    ET.SubElement(notations, "tied", {"type": kind})
                _place(note, notations)
            if lyric and index == 0:
                text = ET.Element("lyric", {"number": "1"})
                ET.SubElement(text, "syllabic").text = "single"
                ET.SubElement(text, "text").text = lyric
                _place(note, text)
        notes.append(note)
    return notes


def _voice_events(measure: ET.Element, voice: str) -> list[list[ET.Element]]:
    """A voice's notes as events: a note with the chord notes stacked on it, or a rest."""
    events: list[list[ET.Element]] = []
    for note in measure.findall("note"):
        if (note.findtext("voice") or "1") != voice:
            continue
        if note.find("chord") is not None and events:
            events[-1].append(note)
        else:
            events.append([note])
    return events


def _voice_span(measure: ET.Element, voice: str, divisions: Fraction) -> Fraction:
    return sum(Fraction(group[0].findtext("duration") or 0) / divisions
               for group in _voice_events(measure, voice))


def _fix_following_backup(measure: ET.Element, voice: str, divisions: Fraction) -> None:
    """The backup after a voice's last note returns to the bar's start."""
    elements = list(measure)
    last = max((i for i, e in enumerate(elements)
                if e.tag == "note" and (e.findtext("voice") or "1") == voice), default=None)
    if last is None:
        return
    for element in elements[last + 1:]:
        if element.tag == "note":
            return
        if element.tag == "backup":
            span = _voice_span(measure, voice, divisions) * divisions
            element.find("duration").text = str(span.numerator)
            return


def _edit_voice(measure: ET.Element, voice: str, staff: str | None, tokens: list[str],
                divisions: Fraction) -> None:
    events = _voice_events(measure, voice)
    # Gaps Audiveris left inside this voice go: the edit says every length.
    first = list(measure).index(events[0][0]) if events else 0
    last_note = list(measure).index(events[-1][-1]) if events else len(measure)
    for element in list(measure)[first:last_note]:
        if element.tag == "forward":
            measure.remove(element)
    position = 0
    for token in tokens:
        if token == "-":
            if position >= len(events):
                raise ValueError("patch '-' runs past the bar's notes")
            for note in events.pop(position):
                measure.remove(note)
        elif token.startswith("="):
            if position >= len(events) or len(events[position]) != 1:
                raise ValueError(f"patch {token!r} needs a single note to re-pitch")
            pitches, length, _, _ = _parse_pitched(token[1:])
            note = events[position][0]
            if len(pitches) != 1 or note.find("pitch") is None:
                raise ValueError(f"patch {token!r} re-pitches one note to one pitch")
            step, alter, octave, _, _ = pitches[0]
            pitch = note.find("pitch")
            for child in list(pitch):
                pitch.remove(child)
            ET.SubElement(pitch, "step").text = step
            if alter:
                ET.SubElement(pitch, "alter").text = str(alter)
            ET.SubElement(pitch, "octave").text = str(octave)
            _set_length(note, length, divisions)
            position += 1
        elif token.startswith("+"):
            new = _build_notes(token[1:], voice, staff, divisions)
            if position < len(events):
                anchor = list(measure).index(events[position][0])
            else:
                anchor = (list(measure).index(events[-1][-1]) + 1) if events else len(measure)
            for offset, note in enumerate(new):
                measure.insert(anchor + offset, note)
            events.insert(position, new)
            position += 1
        else:
            if position >= len(events):
                raise ValueError(f"patch length {token!r} runs past the bar's notes")
            for note in events[position]:
                _set_length(note, Fraction(token), divisions)
            position += 1
    if position != len(events):
        raise ValueError(f"patch covers {position} of the voice's {len(events)} notes and rests")


def _replace_voice(measure: ET.Element, voice: str, staff: str | None, text: str,
                   divisions: Fraction, others: bool) -> None:
    """Replace a voice's notes where they stood. With `others`, every other
    voice (and every backup and forward) goes too."""
    new = [n for token in text.split() for n in _build_notes(token, voice, staff, divisions)]
    mine = [e for e in measure if e.tag == "note" and (e.findtext("voice") or "1") == voice]
    if others or not mine:
        for element in [e for e in measure if e.tag in ("note", "backup", "forward")]:
            measure.remove(element)
        position = len(measure)
        while position and measure[position - 1].tag == "barline":
            position -= 1
    else:
        position = list(measure).index(mine[0])
        span = list(measure)[position:list(measure).index(mine[-1]) + 1]
        for element in span:
            if element.tag == "forward" or element in mine:
                measure.remove(element)
    for offset, note in enumerate(new):
        measure.insert(position + offset, note)
    if not others:
        _fix_following_backup(measure, voice, divisions)


def apply_patches(root: ET.Element, patches: dict, assume: Fraction | None = None) -> list[str]:
    """Hand corrections to single bars, from merge.json "patches":
        {"<part>": {"<bar>": <patch>}}
    where <patch> is
        "1/2 +r:1/2 =E4:1 1/2 -"    an edit of the bar's first voice: each length
                                    keeps the next note or rest and sets its
                                    length; "=E4:1" also re-pitches it; "+..."
                                    inserts a rest or note; "-" deletes; every
                                    note and rest must be covered
        {"voice": "5", "staff": "2", "edit": "..."}   the same for another voice
        {"notes": "D6:1/2 r:1/2 Bb5:2"}  the voice's notes replaced outright, the
                                    other voices dropped
        {"up": "...", "down": "...", "up2": "..."}  a drum bar replaced, as a drum groove
        [<patch>, <patch>]          several, in order: one voice each
    Pitches as Bb4, F#5, Bn4; x before the step for an x notehead; ~ before it
    for a tie from the note before, ~ after the length to tie on; a length
    of g for a grace note (slashed, before the next note: "+C5:g"); @text after
    for a lyric; A/B:len for a fingered tremolo (chord A alternating with
    chord B for len crotchets). Applied last, to the finished score; a
    patched voice must fill its bar exactly."""
    names = {name: pid for pid, name in score.part_names(root)}
    report = []
    for part_name, bars in patches.items():
        if part_name.startswith("_"):
            continue
        if part_name not in names:
            raise ValueError(f"patches name no part {part_name!r}")
        part = next(p for p in root.findall("part") if p.get("id") == names[part_name])
        states = merge.attribute_states(part)
        by_number = {m.get("number"): (m, s) for m, s in zip(part.findall("measure"), states)}
        for number, patch_list in bars.items():
            if number.startswith("_"):
                continue
            if number not in by_number:
                raise ValueError(f"{part_name} has no bar {number}")
            measure, state = by_number[number]
            for patch in patch_list if isinstance(patch_list, list) else [patch_list]:
                _apply_patch(part, measure, state, patch, f"{part_name} bar {number}", assume)
            report.append(f"{part_name} bar {number}: patched")
    return report


def _apply_patch(part: ET.Element, measure: ET.Element, state: dict, patch, where: str,
                 assume: Fraction | None) -> None:
    divisions = _divisions(measure, state)
    if isinstance(patch, str):
        patch = {"edit": patch}
    first = measure.find("note")
    voice = patch.get("voice") or (first.findtext("voice") if first is not None else None) or "1"
    staff = patch.get("staff") or (first.findtext("staff") if first is not None else None)
    drum = any(key in patch for key, _, _ in DRUM_VOICES)
    try:
        if drum:
            voices = [parse_groove_voice(patch.get(key, "")) for key, _, _ in DRUM_VOICES]
            _fit_divisions(part, voices)
            divisions = _divisions(measure, state)
            _write_drum_voices(measure, *voices[:2], divisions, voices[2])
        elif "notes" in patch:
            _replace_voice(measure, voice, staff, patch["notes"], divisions, others=not patch.get("voice"))
        else:
            _edit_voice(measure, voice, staff, patch["edit"].split(), divisions)
            _fix_following_backup(measure, voice, divisions)
    except ValueError as err:
        raise ValueError(f"patch {where}: {err}") from err
    expected = None
    for attributes in measure.findall("attributes"):
        expected = score._time_signature(attributes) or expected
    if expected is None and ("time", "1") in state:
        expected = score._time_signature(_wrap(state[("time", "1")]))
    expected = expected or assume
    for check_voice in {n.findtext("voice") or "1" for n in measure.findall("note")}:
        if drum or "notes" in patch or check_voice == voice:
            span = _voice_span(measure, check_voice, divisions)
            if expected is not None and span != expected:
                raise ValueError(f"patch {where}: voice {check_voice} lasts {span} crotchets, the bar {expected}")


def repair(root: ET.Element, config: dict) -> list[str]:
    report = []
    names = dict(score.part_names(root))
    by_name = {name: pid for pid, name in names.items()}
    parts = {p.get("id"): p for p in root.findall("part")}

    report += marks_from_lyrics(root, config.get("vocal_parts", ["Voice"]))

    if config.get("octave_clefs") is False:
        fixed = moved = 0
        for part in parts.values():
            c, m = fix_octave_clefs(part)
            fixed += c
            moved += m
        report.append(f"octave clefs: {fixed} '8 below/above' marking(s) removed, {moved} note(s) moved back")

    time = config.get("time")
    if time:
        score.parse_time(time)
        written = sum(write_time(p, time) for p in parts.values())
        report.append(f"time {time} written into {written} part(s)")

    transpose = config.get("transpose") or {}
    unknown = sorted(set(transpose) - set(by_name))
    bad_kind = sorted({k for k in transpose.values() if k not in TRANSPOSITIONS})
    if unknown or bad_kind:
        raise ValueError(f"transpose names no such part {unknown} or kind {bad_kind} "
                         f"(kinds: {', '.join(TRANSPOSITIONS)})")
    concert = concert_fifths(root, {by_name[n] for n in transpose})
    for name, kind in transpose.items():
        moved = write_transposition(parts[by_name[name]], kind, concert)
        report.append(f"{name}: transposing in {kind}" + (f"; {moved} key(s) moved to the written key" if moved else ""))

    for bar, concert in (config.get("key_changes") or {}).items():
        if bar.startswith("_"):
            continue
        written = respelled = 0
        for name, pid in by_name.items():
            if parts[pid].find(".//unpitched") is not None:
                continue                                       # a drum part has no key
            kind = transpose.get(name)
            fifths = int(concert) + (TRANSPOSITIONS[kind][1] if kind else 0)
            w, r = apply_key_change(parts[pid], str(bar), fifths)
            written += w
            respelled += r
        report.append(f"key change at bar {bar} (concert {concert}): written into {written} part(s), "
                      f"{respelled} note(s) respelled from the old key")

    if config.get("drum_grooves"):
        expected = score.parse_time(time) if time else None
        report += fill_drum_grooves(root, config["drum_grooves"], expected)

    resized = added = emptied = 0
    for part in parts.values():
        r, a, e = fill_rest_bars(part)
        resized += r
        added += a
        emptied += e
    fitted = sum(fit_rest_voices(part) for part in parts.values())
    report.append(f"whole-bar rests: {resized} set to a full bar, {added} added to empty staves, "
                  f"{emptied} written into bars with nothing in them, "
                  f"{fitted} fitted to the bar beside notes in another voice")
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    root = score.load(args.input)
    try:
        report = repair(root, json.loads(args.config.read_text(encoding="utf-8")))
    except ValueError as err:
        print(f"repair.py: {err}", file=sys.stderr)
        return 1
    score.save(root, args.output, like=args.input)
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
