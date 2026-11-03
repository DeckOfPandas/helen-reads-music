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
      mark of an open hi-hat, joined by + for notes struck together; r for
      a rest; length in crotchets). It goes into the
      bars it lists ("bars": [9, 10]), or else into every bar whose solid
      notes (kick, snare) come in the same order as the groove's. The report
      lists the bars each groove filled. Variations it cannot see (an open
      hi-hat, a crash) are not detected.

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
from collections import Counter
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


def fill_rest_bars(part: ET.Element) -> tuple[int, int]:
    """(whole-bar rests set to a full bar, rests added to empty staves)."""
    divisions = None
    expected = None
    staves = 1
    resized = added = 0
    for measure in part.findall("measure"):
        for attributes in measure.findall("attributes"):
            if attributes.findtext("divisions"):
                divisions = Fraction(attributes.findtext("divisions"))
            if attributes.findtext("staves"):
                staves = int(attributes.findtext("staves"))
            signature = score._time_signature(attributes)
            if signature is not None:
                expected = signature
        if divisions is None or expected is None or not _is_rest_only(measure):
            continue
        ticks = expected * divisions
        if ticks.denominator != 1:
            continue
        ticks = str(ticks.numerator)
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
    return resized, added


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
    for attributes in measure.findall("attributes"):
        if attributes.findtext("divisions"):
            return Fraction(attributes.findtext("divisions"))
    element = state.get(("divisions", "1"))
    return Fraction(element.text) if element is not None else None


# --- drum grooves ----------------------------------------------------------------

def parse_groove_voice(text: str) -> list[tuple[list[tuple[str, int, bool, bool]], Fraction]]:
    """"xG5:1 oxG5+C5:1/2 r:1" -> [([(step, octave, x-notehead, open), ...], crotchets)].
    A position's leading x is an x notehead, o the open mark (an open
    hi-hat); an empty list of positions is a rest."""
    events = []
    for token in text.split():
        heads, _, length = token.rpartition(":")
        if not heads or not length:
            raise ValueError(f"drum groove token {token!r} is not <positions>:<length>")
        positions = []
        if heads != "r":
            for head in heads.split("+"):
                flags = ""
                while head and head[0] in "xo":
                    flags, head = flags + head[0], head[1:]
                if len(head) < 2 or head[0] not in score.STEPS or not head[1:].isdigit():
                    raise ValueError(f"drum groove position {head!r} is not like G5, xG5 or oxG5")
                positions.append((head[0], int(head[1:]), "x" in flags, "o" in flags))
        events.append((positions, Fraction(length)))
    return events


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


def _groove_solid_sequence(voices: list[list[tuple]]) -> list[str]:
    hits = []
    for events in voices:
        onset = Fraction(0)
        for positions, length in events:
            hits += [(onset, f"{step}{octave}") for step, octave, cross, _ in positions if not cross]
            onset += length
    return [position for _, position in sorted(hits)]


def _drum_notes(events: list[tuple], voice: str, stem: str, divisions: Fraction) -> list[ET.Element]:
    notes = []
    for positions, length in events:
        ticks = length * divisions
        if ticks.denominator != 1:
            raise ValueError(f"drum groove length {length} does not fit divisions {divisions}")
        heads = positions or [None]
        for index, head in enumerate(heads):
            note = ET.Element("note")
            if index:
                ET.SubElement(note, "chord")
            if head is None:
                ET.SubElement(note, "rest")
            else:
                unpitched = ET.SubElement(note, "unpitched")
                ET.SubElement(unpitched, "display-step").text = head[0]
                ET.SubElement(unpitched, "display-octave").text = str(head[1])
            ET.SubElement(note, "duration").text = str(ticks.numerator)
            ET.SubElement(note, "voice").text = voice
            if length in NOTE_TYPES:
                ET.SubElement(note, "type").text = NOTE_TYPES[length]
            if head is not None:
                ET.SubElement(note, "stem").text = stem
                if head[2]:
                    ET.SubElement(note, "notehead").text = "x"
                if head[3]:
                    technical = ET.SubElement(ET.SubElement(note, "notations"), "technical")
                    ET.SubElement(technical, "open")
            notes.append(note)
    return notes


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
        up, down = parse_groove_voice(spec.get("up", "")), parse_groove_voice(spec.get("down", ""))
        for label, events in (("up", up), ("down", down)):
            total = sum(length for _, length in events)
            if events and expected is not None and total != expected:
                raise ValueError(f"drum groove {name} {label} lasts {total} crotchets, the bar {expected}")
        bars = {str(b) for b in spec["bars"]} if "bars" in spec else None
        grooves[name] = (up, down, bars, _groove_solid_sequence([up, down]))

    filled: dict[str, list[str]] = {name: [] for name in grooves}
    states = merge.attribute_states(part)
    for measure, state in zip(part.findall("measure"), states):
        number = measure.get("number") or "?"
        chosen = [n for n, (_, _, bars, _) in grooves.items() if bars is not None and number in bars]
        if not chosen:
            sequence = _solid_sequence(measure)
            chosen = [n for n, (_, _, bars, solid) in grooves.items()
                      if bars is None and sequence and sequence == solid]
        if len(chosen) != 1:
            continue
        divisions = _divisions(measure, state)
        if divisions is None:
            continue
        up, down, _, _ = grooves[chosen[0]]
        own = [score._time_signature(a) for a in measure.findall("attributes")]
        own = [t for t in own if t is not None]
        in_force = own[-1] if own else (score._time_signature(_wrap(state[("time", "1")]))
                                        if ("time", "1") in state else expected)
        if in_force is not None and sum(l for _, l in up) != in_force:
            continue
        for element in [e for e in measure if e.tag in ("note", "backup", "forward")]:
            measure.remove(element)
        position = len(measure)
        while position and measure[position - 1].tag == "barline":
            position -= 1
        new = _drum_notes(up, "1", "up", divisions)
        if down:
            backup = ET.Element("backup")
            ET.SubElement(backup, "duration").text = str((sum(l for _, l in up) * divisions).numerator)
            new.append(backup)
            new += _drum_notes(down, "2", "down", divisions)
        for offset, element in enumerate(new):
            measure.insert(position + offset, element)
        filled[chosen[0]].append(number)
    return [f"{part_name}: groove {name} written into {len(bars)} bar(s): {', '.join(bars) or 'none'}"
            for name, bars in filled.items()]


def repair(root: ET.Element, config: dict) -> list[str]:
    report = []
    names = dict(score.part_names(root))
    by_name = {name: pid for pid, name in names.items()}
    parts = {p.get("id"): p for p in root.findall("part")}

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

    if config.get("drum_grooves"):
        expected = score.parse_time(time) if time else None
        report += fill_drum_grooves(root, config["drum_grooves"], expected)

    resized = added = 0
    for part in parts.values():
        r, a = fill_rest_bars(part)
        resized += r
        added += a
    report.append(f"whole-bar rests: {resized} set to a full bar, {added} added to empty staves")
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
