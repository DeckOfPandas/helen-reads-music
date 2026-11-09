#!/usr/bin/env python3
"""Put right a page Audiveris read with the wrong number of bars, when the
printed count is known (book.read_page_checked).

USAGE (from the repo root):
    python3 tools/omr/pagefix.py <page.mxl> <bars printed> <out.mxl>

On a dense scan Audiveris makes two kinds of mistake with a page's bars:
  - It splits the page's one system into several, read one after another:
    the woodwinds' four bars, then everyone else's four. Each system is
    folded back onto the first, bar for bar: a part takes each bar from the
    system its staves print in.
  - It misses a bar line, making one bar of two; or sees one that isn't
    there, making two bars of one. A system one bar short has its bar that
    holds twice the usual length split in two; one bar long has the two
    neighbouring bars that together make one bar joined.

The usual bar length is the commonest among the page's bars, so no time
signature is needed (Audiveris often reads none). Each system is fixed on
its own, then folded. The page is put right only if every system comes out
with exactly the printed bars; otherwise it is left as read.

Splitting a note across the new bar line ties the two halves; a rest is
cut in two.
"""
from __future__ import annotations

import copy
import sys
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import merge
import repair
import score

ET = score.ET


@dataclass
class Event:
    """A note or rest with its chord notes, in one voice, at a time in the bar."""
    start: Fraction
    voice: str
    length: Fraction
    elements: list[ET.Element] = field(default_factory=list)


@dataclass
class Bar:
    events: list[Event]
    others: list[tuple[Fraction, ET.Element]]      # print, attributes, direction, barline...
    length: Fraction


def _divisions(state: dict) -> Fraction:
    element = state.get(("divisions", "1"))
    return Fraction(element.text) if element is not None and element.text else Fraction(1)


def parse(measure: ET.Element, divisions: Fraction) -> Bar:
    """A bar as events and other elements, times in crotchets."""
    events: list[Event] = []
    others: list[tuple[Fraction, ET.Element]] = []
    cursor = longest = Fraction(0)
    for element in measure:
        if element.tag == "attributes" and element.findtext("divisions"):
            divisions = Fraction(element.findtext("divisions"))
        if element.tag == "note":
            length = Fraction(element.findtext("duration") or 0) / divisions
            if element.find("chord") is not None and events:
                events[-1].elements.append(element)
                continue
            if element.find("grace") is not None:
                length = Fraction(0)
            events.append(Event(cursor, element.findtext("voice") or "1", length, [element]))
            cursor += length
        elif element.tag == "backup":
            cursor -= Fraction(element.findtext("duration") or 0) / divisions
        elif element.tag == "forward":
            cursor += Fraction(element.findtext("duration") or 0) / divisions
        else:
            others.append((cursor, element))
        longest = max(longest, cursor)
    return Bar(events, others, longest)


def _set_length(event: Event, length: Fraction, divisions: Fraction) -> None:
    event.length = length
    for element in event.elements:
        element.find("duration").text = str(int(length * divisions)) if (length * divisions).denominator == 1 \
            else str(float(length * divisions))
        kind = repair.NOTE_TYPES.get(length)
        if element.find("rest") is not None and element.find("rest").get("measure") == "yes":
            continue
        for tag in ("type", "dot"):
            for old in element.findall(tag):
                element.remove(old)
        if kind:
            index = next((i for i, c in enumerate(element) if c.tag in ("stem", "notehead", "staff", "beam",
                                                                      "notations", "lyric")), len(element))
            ET_type = ET.Element("type")
            ET_type.text = kind
            element.insert(index, ET_type)


def _tie(event: Event, kind: str) -> None:
    for element in event.elements:
        if element.find("rest") is not None:
            continue
        tie = ET.Element("tie", {"type": kind})
        duration = element.find("duration")
        element.insert(list(element).index(duration) + 1, tie)
        notations = element.find("notations")
        if notations is None:
            notations = ET.SubElement(element, "notations")
        ET.SubElement(notations, "tied", {"type": kind})


def build(number: str, events: list[Event], others: list[tuple[Fraction, ET.Element]],
          divisions: Fraction) -> ET.Element:
    """A measure from events and other elements: voice by voice, with
    backups between voices and forwards over gaps."""
    measure = ET.Element("measure", {"number": number})
    head = [e for t, e in others if e.tag in ("print", "attributes") or
            (e.tag == "barline" and e.get("location") == "left")]
    tail = [e for t, e in others if e.tag == "barline" and e.get("location") != "left"]
    placed = [(t, e) for t, e in others if e not in head and e not in tail]
    measure.extend(head)
    voices: list[str] = []
    for event in events:
        if event.voice not in voices:
            voices.append(event.voice)
    unit = lambda length: str(int(length * divisions))  # noqa: E731
    for index, voice in enumerate(voices):
        cursor = Fraction(0)
        mine = sorted((e for e in events if e.voice == voice), key=lambda e: e.start)
        for event in mine:
            if index == 0:
                while placed and placed[0][0] <= event.start:
                    measure.append(placed.pop(0)[1])
            if event.start > cursor:
                forward = ET.SubElement(measure, "forward")
                ET.SubElement(forward, "duration").text = unit(event.start - cursor)
                ET.SubElement(forward, "voice").text = voice
            measure.extend(event.elements)
            cursor = event.start + event.length
        if index == 0:
            measure.extend(e for _, e in placed)
            placed = []
        if index < len(voices) - 1 and cursor:
            backup = ET.SubElement(measure, "backup")
            ET.SubElement(backup, "duration").text = unit(cursor)
    measure.extend(e for _, e in placed)
    measure.extend(tail)
    return measure


def split(measure: ET.Element, at: Fraction, divisions: Fraction) -> tuple[ET.Element, ET.Element]:
    """Two bars from one, cut at `at` crotchets."""
    bar = parse(copy.deepcopy(measure), divisions)
    first, second = [], []
    for event in bar.events:
        end = event.start + event.length
        if end <= at or (event.length == 0 and event.start < at):
            first.append(event)
        elif event.start >= at:
            event.start -= at
            second.append(event)
        else:                                    # crosses the new bar line
            rest = event.elements[0].find("rest") is not None
            if rest and event.elements[0].find("rest").get("measure") == "yes":
                late = Event(Fraction(0), event.voice, end - at, copy.deepcopy(event.elements))
                _set_length(event, at - event.start, divisions)
                _set_length(late, late.length, divisions)
            else:
                late = Event(Fraction(0), event.voice, end - at, copy.deepcopy(event.elements))
                for element in late.elements:
                    for lyric in element.findall("lyric"):
                        element.remove(lyric)
                _set_length(event, at - event.start, divisions)
                _set_length(late, late.length, divisions)
                if not rest:
                    _tie(event, "start")
                    _tie(late, "stop")
            first.append(event)
            second.append(late)
    early = [(t, e) for t, e in bar.others if t < at or e.tag in ("print", "attributes")]
    late_others = [(t - at, e) for t, e in bar.others if (t, e) not in early]
    number = measure.get("number") or ""
    return build(number, first, early, divisions), build(number + "b", second, late_others, divisions)


def join(one: ET.Element, two: ET.Element, at: Fraction, divisions_one: Fraction,
         divisions_two: Fraction) -> ET.Element:
    """One bar from two, the second's events starting at `at` crotchets."""
    first = parse(copy.deepcopy(one), divisions_one)
    second = parse(copy.deepcopy(two), divisions_two)
    if divisions_two != divisions_one:
        for event in second.events:
            for element in event.elements:
                element.find("duration").text = str(int(event.length * divisions_one))
    for event in second.events:
        event.start += at
    others = first.others + [(t + at, e) for t, e in second.others if e.tag not in ("print",)]
    return build(one.get("number") or "", first.events + second.events, others, divisions_one)


def _printed(part: ET.Element) -> list[bool]:
    state, shown = True, []
    for measure in part.findall("measure"):
        for details in measure.iter("staff-details"):
            state = details.get("print-object", "yes") != "no"
        shown.append(state)
    return shown


def systems(root: ET.Element) -> list[range]:
    """Bar index ranges, split where any part starts a new system or page."""
    parts = root.findall("part")
    count = len(parts[0].findall("measure"))
    starts = [0] + [i for i in range(1, count) if any(
        p.findall("measure")[i].find("print[@new-system='yes']") is not None or
        p.findall("measure")[i].find("print[@new-page='yes']") is not None for p in parts)]
    return [range(a, b) for a, b in zip(starts, starts[1:] + [count])]


def _lengths(root: ET.Element) -> list[list[Fraction | None]]:
    """For each part, each bar's length in crotchets, None where it holds nothing."""
    out = []
    for part in root.findall("part"):
        states = merge.attribute_states(part)
        row = []
        for measure, state in zip(part.findall("measure"), states):
            bar = parse(measure, _divisions(merge._end_state(state, measure)))
            # A whole-bar rest's length is Audiveris's guess at the bar: not evidence.
            ends = [e.start + e.length for e in bar.events
                    if e.elements[0].find("rest[@measure='yes']") is None]
            row.append(max(ends) if ends else None)
        out.append(row)
    return out


def _sounds(measure: ET.Element) -> bool:
    return any(n.find("rest") is None for n in measure.findall("note"))


def typical(root: ET.Element) -> Fraction | None:
    """The commonest length among bars with notes in them. A whole-bar rest's
    length is Audiveris's guess at the bar, so it doesn't count."""
    found = Counter(n for part, row in zip(root.findall("part"), _lengths(root))
                    for n, measure in zip(row, part.findall("measure")) if n and _sounds(measure))
    return found.most_common(1)[0][0] if found else None


def _rebuild(root: ET.Element, columns: list) -> None:
    """Set every part's bars to `columns`: per part, a list of measures."""
    for part, measures in zip(root.findall("part"), columns):
        for old in part.findall("measure"):
            part.remove(old)
        part.extend(measures)


def _split_bar(root: ET.Element, index: int, at: Fraction) -> None:
    columns = []
    for part in root.findall("part"):
        measures = part.findall("measure")
        states = merge.attribute_states(part)
        divisions = _divisions(merge._end_state(states[index], measures[index]))
        one, two = split(measures[index], at, divisions)
        columns.append(measures[:index] + [one, two] + measures[index + 1:])
    _rebuild(root, columns)


def _join_bars(root: ET.Element, index: int, at: Fraction) -> None:
    columns = []
    for part in root.findall("part"):
        measures = part.findall("measure")
        states = merge.attribute_states(part)
        d1 = _divisions(merge._end_state(states[index], measures[index]))
        d2 = _divisions(merge._end_state(states[index + 1], measures[index + 1]))
        joined = join(measures[index], measures[index + 1], at, d1, d2)
        columns.append(measures[:index] + [joined] + measures[index + 2:])
    _rebuild(root, columns)


def _drop_bar(root: ET.Element, index: int, into: int) -> None:
    """Take out a bar with nothing sounding in it: its rests go; its print,
    attributes and directions move to bar `into`, a neighbour."""
    columns = []
    for part in root.findall("part"):
        measures = part.findall("measure")
        gone, keep = measures[index], copy.deepcopy(measures[into])
        moved = [copy.deepcopy(c) for c in gone if c.tag in ("print", "attributes", "direction")]
        if into > index:                        # the next bar: before its notes
            head = [c for c in moved if c.tag in ("print", "attributes")]
            for position, child in enumerate(head):
                keep.insert(position, child)
            first_note = next((i for i, c in enumerate(keep) if c.tag == "note"), len(keep))
            for offset, child in enumerate(c for c in moved if c.tag == "direction"):
                keep.insert(first_note + offset, child)
        else:                                   # the bar before: at its end
            keep.extend(c for c in moved if c.tag in ("direction", "attributes"))
        columns.append([keep if i == into else m for i, m in enumerate(measures) if i != index])
    _rebuild(root, columns)


def _median(values: list[Fraction]) -> Fraction | None:
    values = sorted(values)
    return values[len(values) // 2] if values else None


def _sounding_lengths(root: ET.Element) -> list[Fraction | None]:
    """Each bar's median length over the parts with notes (not just rests) in it.
    On a poor scan single parts' rhythms are often wrong; the median is not."""
    rows = [(row, part.findall("measure")) for part, row in zip(root.findall("part"), _lengths(root))]
    count = len(rows[0][0]) if rows else 0
    return [_median([row[i] for row, measures in rows if row[i] and _sounds(measures[i])])
            for i in range(count)]


def _fix_system(root: ET.Element, bars: range, want: int, usual: Fraction) -> tuple[int, list[str]]:
    """Split or join within one system until it has `want` bars. (bars now, notes)."""
    notes = []
    medians = _sounding_lengths(root)
    count = len(bars)
    if count == want - 1:
        # A bar is missing, so the longest bar past the usual length is the
        # likeliest two: a poor read may have lost some of either half.
        candidates = [i for i in bars if medians[i] and medians[i] > usual]
        if candidates:
            best = max(candidates, key=lambda i: (medians[i], -abs(medians[i] - 2 * usual)))
            _split_bar(root, best, usual)
            notes.append(f"bar {best + 1} held two bars (median {float(medians[best]):g} crotchets): split")
            count += 1
    elif count == want + 1:
        silent = [i for i in bars if medians[i] is None]
        candidates = [i for i in bars[:-1] if medians[i] and medians[i + 1]
                      and medians[i] < usual and medians[i + 1] < usual]
        if silent:
            # Nothing sounds in it anywhere: a bar line seen where there is none.
            gone = silent[-1]
            into = gone - 1 if gone > bars[0] else gone + 1
            _drop_bar(root, gone, into)
            notes.append(f"bar {gone + 1} had nothing sounding: dropped into bar {into + 1}")
            count -= 1
        elif candidates:
            best = min(candidates, key=lambda i: abs(medians[i] + medians[i + 1] - usual))
            _join_bars(root, best, medians[best])
            notes.append(f"bars {best + 1} and {best + 2} made one bar "
                         f"(medians {float(medians[best]):g} + {float(medians[best + 1]):g}): joined")
            count -= 1
    return count, notes


def _emptied(measure: ET.Element) -> ET.Element:
    """The bar with its notes and directions taken out: attributes and print stay."""
    bar = copy.deepcopy(measure)
    for child in list(bar):
        if child.tag not in ("attributes", "print"):
            bar.remove(child)
    return bar


def _separate(root: ET.Element, ranges: list[range]) -> int:
    """A part with notes in more than one system is staves Audiveris took
    for one part: give each system's notes their own part. Count made."""
    part_list = root.find("part-list")
    made = 0
    for part in list(root.findall("part")):
        measures = part.findall("measure")
        playing = [s for s, r in enumerate(ranges) if any(_sounds(measures[i]) for i in r)]
        for system in playing[1:]:
            pid = part.get("id")
            new = f"{pid}-s{system + 1}"
            clone = copy.deepcopy(part)
            clone.set("id", new)
            for i, measure in enumerate(clone.findall("measure")):
                if i not in ranges[system]:
                    clone.insert(list(clone).index(measure), _emptied(measure))
                    clone.remove(measure)
            for i in ranges[system]:
                part.insert(list(part).index(measures[i]), _emptied(measures[i]))
                part.remove(measures[i])
            entry = copy.deepcopy(next(sp for sp in part_list.iter("score-part") if sp.get("id") == pid))
            for element in [*entry.iter(), *clone.iter("instrument")]:     # iter() includes entry
                ident = element.get("id")
                if ident == pid:
                    element.set("id", new)
                elif ident and ident.startswith(pid + "-"):
                    element.set("id", new + ident[len(pid):])
            original = next(sp for sp in part_list if sp.get("id") == pid)
            part_list.insert(list(part_list).index(original) + 1, entry)
            root.insert(list(root).index(part) + 1, clone)
            made += 1
            measures = part.findall("measure")
    return made


def _fold(root: ET.Element, ranges: list[range]) -> None:
    """Each part takes the first system's bars from the system its notes
    are in (else the first system its staves print in, else the first)."""
    width = len(ranges[0])
    columns = []
    for part in root.findall("part"):
        measures = part.findall("measure")
        shown = _printed(part)
        states = merge.attribute_states(part)
        system = next((s for s, r in enumerate(ranges) if any(_sounds(measures[i]) for i in r)),
                      next((s for s, r in enumerate(ranges)
                            if any(shown[i] and measures[i].find("note") is not None for i in r)), 0))
        if system == 0:
            columns.append([measures[i] for i in ranges[0]])
            continue
        kept = []
        for k in range(width):
            source = ranges[system][k]
            # From another system: restate everything in force there
            # (divisions, clef, key...) at the page's first bar.
            bar = merge._restate(measures[source], states[source], {}) if k == 0 \
                else copy.deepcopy(measures[source])
            for printed in bar.findall("print"):
                bar.remove(printed)
            bar.set("number", measures[ranges[0][k]].get("number") or str(k + 1))
            kept.append(bar)
        columns.append(kept)
    _rebuild(root, columns)


def fix_page(root: ET.Element, want: int) -> list[str]:
    """Put the page right in place; notes say what was done. Raises
    ValueError, leaving `root` unchanged in meaning (the caller keeps its
    own copy), when it cannot."""
    usual = typical(root)
    if usual is None:
        raise ValueError("no bars with notes on the page")
    notes = []
    ranges = systems(root)
    for index in reversed(range(len(ranges))):   # later systems first: indices before stay put
        bars = systems(root)[index]
        count, done = _fix_system(root, bars, want, usual)
        notes.extend(f"system {index + 1}: {n}" for n in done)
        if count != want:
            raise ValueError(f"system {index + 1} has {count} bars, printed {want}")
    ranges = systems(root)
    if len(ranges) > 1:
        made = _separate(root, ranges)
        if made:
            notes.append(f"{made} part(s) with notes in two systems separated")
        _fold(root, ranges)
        notes.append(f"{len(ranges)} systems folded into one")
    count = len(root.find("part").findall("measure"))
    if count != want:
        raise ValueError(f"{count} bars after fixing, printed {want}")
    for part in root.findall("part"):
        for number, measure in enumerate(part.findall("measure"), start=1):
            measure.set("number", str(number))
    return notes


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    root = score.load(argv[0])
    try:
        notes = fix_page(root, int(argv[1]))
    except ValueError as err:
        print(f"not fixed: {err}")
        return 1
    score.save(root, Path(argv[2]))
    print("\n".join(notes))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
