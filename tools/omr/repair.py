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

Always, when "time" is known (from the score or merge.json):
  A bar holding nothing but whole-bar rests (<rest measure="yes">) takes
  its length from the time signature. Audiveris gives such a rest the bar
  length it inferred from the other staves, so one misread note elsewhere
  makes every silent staff's bar wrong too. A staff of a multi-staff part
  with no rest in such a bar gets one: Audiveris often writes the rest on
  the top staff only.

Nothing here changes a played note: bars with notes in them are left alone,
for score.py check to report.
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


def repair(root: ET.Element, config: dict) -> list[str]:
    report = []
    names = dict(score.part_names(root))
    by_name = {name: pid for pid, name in names.items()}
    parts = {p.get("id"): p for p in root.findall("part")}

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
