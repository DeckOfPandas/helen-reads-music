#!/usr/bin/env python3
"""Merge the parts Audiveris split one instrument into, from a table you fill in.

USAGE (from the repo root):
    python3 tools/omr/merge.py <in.mxl> <out.mxl> --config <merge.json>

Audiveris reads each page of a book mostly on its own. A staff hidden on one
page, or a name OCR reads differently from page to page ("Fl.", "F1.",
"Flute"), gives it no way to see two staves are one instrument, so it exports
each as its own part: 19 instruments over 20 pages can come out as hundreds of
parts, most of them rests. Which names are one instrument is a judgement, so
it is never guessed: you write the table.

Config (score.py names lists the raw names):
{
  "canonical_order": ["Flute", "Oboe", ...],          output parts, score order
  "aliases": {"Fl.": "Flute", "F1.": "Flute", "Flute": "Flute", "Ob.": "Oboe"}
}
A name not in aliases, or mapped to null, stays its own part; nothing is
dropped. A key may also be a part id from names.txt ("P23": "Marimba"): an
id wins over its name, for the names Audiveris gives to several different
staves (an unread name comes out as "Voice").

Rules:
- Each bar of an instrument comes from the one fragment with notes in it.
  If two fragments both have notes in the same bar, that is a CONFLICT: the
  table is wrong for that name (typically several different vocal staves
  all read as "Voice"). Take the name out of the table.
- When a bar comes from a different fragment than the bar before it, that
  fragment's clef, key, time, divisions, staves and transposition are
  re-stated at the start of the bar. A bar's durations and pitches only mean
  something against those.
- The count of sounding notes must come out exactly as it went in, or
  nothing is written and the exit status is 1.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import score

# MusicXML's order for the children of <attributes> that carry over.
CARRIED = ("divisions", "key", "time", "staves", "clef", "transpose")


def _key(element: ET.Element) -> tuple[str, str]:
    return element.tag, element.get("number", "1")


def attribute_states(part: ET.Element) -> list[dict[tuple[str, str], ET.Element]]:
    """For each bar, the attributes in force at its start."""
    state: dict[tuple[str, str], ET.Element] = {}
    states = []
    for measure in part.findall("measure"):
        states.append(dict(state))
        for attributes in measure.findall("attributes"):
            for child in attributes:
                if child.tag in CARRIED:
                    state[_key(child)] = child
    return states


def _end_state(start: dict, measure: ET.Element) -> dict:
    state = dict(start)
    for attributes in measure.findall("attributes"):
        for child in attributes:
            if child.tag in CARRIED:
                state[_key(child)] = child
    return state


def _restate(measure: ET.Element, wanted: dict, current: dict) -> ET.Element:
    """A copy of `measure` that first sets every attribute in `wanted` that
    `current` lacks or holds differently."""
    changed = [element for key, element in wanted.items()
               if key not in current or ET.tostring(current[key]) != ET.tostring(element)]
    measure = copy.deepcopy(measure)
    if not changed:
        return measure
    attributes = ET.Element("attributes")
    for element in sorted(changed, key=lambda e: (CARRIED.index(e.tag), e.get("number", "1"))):
        attributes.append(copy.deepcopy(element))
    position = 0
    while position < len(measure) and measure[position].tag == "print":
        position += 1
    measure.insert(position, attributes)
    return measure


def _instrument_map(fragment: ET.Element, template: ET.Element) -> dict[str, str | None]:
    """A fragment's score-instrument ids onto the template's, by position:
    notes name their own part's instruments (one per drum, on a drum kit),
    and the merged part declares only the template's. None where the
    template has fewer."""
    ours = [i.get("id") for i in fragment.iter("score-instrument")]
    theirs = [i.get("id") for i in template.iter("score-instrument")]
    return {i: (theirs[n] if n < len(theirs) else None) for n, i in enumerate(ours)}


def _renumber_instruments(measure: ET.Element, mapping: dict[str, str | None]) -> None:
    for note in measure.iter("note"):
        for instrument in note.findall("instrument"):
            target = mapping.get(instrument.get("id"))
            if target is None:
                note.remove(instrument)
            else:
                instrument.set("id", target)


def merge(root: ET.Element, canonical_order: list[str],
          aliases: dict[str, str | None]) -> tuple[bool, list[str]]:
    """Merge in place. (True if every note is accounted for, report lines.)
    On False, `root` is left part-merged and must not be written."""
    report: list[str] = []
    names = score.part_names(root)
    parts = {p.get("id"): p for p in root.findall("part")}
    part_list = root.find("part-list")
    score_parts = {sp.get("id"): sp for sp in part_list.iter("score-part")}

    unknown = sorted({c for c in aliases.values() if c is not None} - set(canonical_order))
    if unknown:
        return False, [f"aliases name instruments missing from canonical_order: {unknown}"]

    groups: dict[str, list[str]] = {c: [] for c in canonical_order}
    unmatched = []
    for pid, name in names:
        canon = aliases[pid] if pid in aliases else aliases.get(name)
        (groups[canon] if canon else unmatched).append(pid)

    notes_in = sum(score.count_part_notes(p) for p in parts.values())
    notes_out = 0
    conflicts = []
    new_part_list = ET.Element("part-list")
    new_parts = []

    for canon in canonical_order:
        pids = groups[canon]
        if not pids:
            report.append(f"(no parts named for '{canon}')")
            continue
        bars = {pid: parts[pid].findall("measure") for pid in pids}
        states = {pid: attribute_states(parts[pid]) for pid in pids}
        instrument_ids = {pid: _instrument_map(score_parts[pid], score_parts[pids[0]]) for pid in pids}
        length = max(len(b) for b in bars.values())
        merged = []
        current: dict = {}
        previous = None
        for index in range(length):
            present = [pid for pid in pids if index < len(bars[pid])]
            sounding = [pid for pid in present if score.count_notes(bars[pid][index])]
            if len(sounding) > 1:
                conflicts.append((canon, index + 1,
                                  {pid: score.count_notes(bars[pid][index]) for pid in sounding}))
            source = sounding[0] if sounding else (previous if previous in present else present[0])
            measure = bars[source][index]
            wanted = states[source][index]
            if source != previous:
                bar = _restate(measure, wanted, current)
                current = _end_state({**current, **wanted}, measure)
            else:
                bar = copy.deepcopy(measure)
                current = _end_state(current, measure)
            _renumber_instruments(bar, instrument_ids[source])
            merged.append(bar)
            previous = source

        new_id = "M-" + "".join(c if c.isalnum() else "_" for c in canon)
        score_part = copy.deepcopy(score_parts[pids[0]])
        score_part.set("id", new_id)
        name_element = score_part.find("part-name")
        if name_element is None:
            name_element = ET.SubElement(score_part, "part-name")
        name_element.text = canon
        for element in score_part.iter():
            if element.tag in ("score-instrument", "midi-instrument") and element.get("id"):
                element.set("id", f"{new_id}-{element.get('id')}")
        new_part_list.append(score_part)
        new_part = ET.Element("part", {"id": new_id})
        new_part.extend(merged)
        for instrument in new_part.iter("instrument"):
            instrument.set("id", f"{new_id}-{instrument.get('id')}")
        new_parts.append(new_part)
        got = sum(score.count_notes(m) for m in merged)
        notes_out += got
        report.append(f"{canon}: {len(pids)} part(s) {pids} -> {got} notes")

    if conflicts:
        report.append("CONFLICTS (two parts with notes in the same bar: the table is wrong for this name):")
        for canon, bar, counts in conflicts:
            report.append(f"  {canon} bar #{bar}: notes per part {counts}")
    if unmatched:
        report.append(f"left as they were (not in the table): {[(p, dict(names)[p]) for p in unmatched]}")
    for pid in unmatched:
        new_part_list.append(score_parts[pid])
        new_parts.append(parts[pid])
        notes_out += score.count_part_notes(parts[pid])

    ok = notes_in == notes_out
    report.append(f"notes in {notes_in}, out {notes_out}: "
                  f"{'conserved' if ok else 'MISMATCH, nothing written'}")
    if ok:
        index = list(root).index(part_list)
        for child in [c for c in root if c.tag in ("part-list", "part")]:
            root.remove(child)
        root.insert(index, new_part_list)
        for offset, part in enumerate(new_parts, start=1):
            root.insert(index + offset, part)
    return ok, report


def merge_file(source: Path, out: Path, config: Path) -> tuple[bool, list[str]]:
    table = json.loads(config.read_text(encoding="utf-8"))
    root = score.load(source)
    ok, report = merge(root, table["canonical_order"], table["aliases"])
    if ok:
        score.save(root, out, like=source)
    return ok, report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    ok, report = merge_file(args.input, args.output, args.config)
    print("\n".join(report))
    if not ok:
        print(f"NOT WRITTEN: {args.output}. Fix the table (usually: remove a name in a conflict).",
              file=sys.stderr)
        return 1
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
