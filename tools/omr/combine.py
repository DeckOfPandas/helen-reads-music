#!/usr/bin/env python3
"""Fill a merged score's bad bars from a second read of the same pages.

process.py runs this when a piece has two Audiveris reads (see its --help).
Each read gets some bars right that the other gets wrong, so after the
primary read is merged and repaired:

1. align: each fragment of the second read is matched to one of the
   primary's merged instruments by its notes (the pitch class and onset of
   every note, so an octave misread still matches), over the bars where it
   has notes; a drum's notes by their position on the stave. A tie
   (instruments in unison) goes to the instrument the table maps the name
   to, then to the one the name abbreviates ("Picc.", "V111. II"), then to
   the one with the same clef.
   A fragment matching nothing well, or still tied, is left out and named.
   No table needs writing for the second read.
2. The second read is merged with that alignment and repaired with the same
   merge.json settings as the primary.
3. Bar by bar, for each instrument: a bar that does not add up in the
   primary is taken from the second read if it adds up there. A bar the
   second read has silent where the primary has notes is never taken (the
   second read failed to match that fragment, it did not hear a rest). Bars that add
   up in both reads but hold different notes are listed, since one of them
   is wrong; bars neither read got right are listed too.

A bar taken from the second read re-states its clef, key, time and
divisions, as a merge does, so it reads the same in its new place.
"""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from collections import Counter
from fractions import Fraction

import merge
import score

MIN_MATCH = 0.5          # share of a fragment's notes that must match an instrument
TIE = 0.05               # scores closer than this are a tie


def _events(part: ET.Element, only_bars: set[int] | None = None) -> Counter:
    """(bar index, onset in crotchets, pitch class) for every sounding note."""
    events = Counter()
    divisions = None
    for index, measure in enumerate(part.findall("measure")):
        for attributes in measure.findall("attributes"):
            if attributes.findtext("divisions"):
                divisions = Fraction(attributes.findtext("divisions"))
        if divisions is None or (only_bars is not None and index not in only_bars):
            continue
        cursor = last_onset = Fraction(0)
        for element in measure:
            if element.tag not in ("note", "backup", "forward"):
                continue
            duration = Fraction(element.findtext("duration") or 0) / divisions
            if element.tag == "backup":
                cursor -= duration
                continue
            if element.tag == "forward":
                cursor += duration
                continue
            if element.find("chord") is not None:
                onset = last_onset                 # stacked on the note before
            else:
                onset = last_onset = cursor
                if element.find("grace") is None:
                    cursor += duration
            pitch = _pitch(element)
            if pitch is not None:
                events[(index, onset, pitch % 12)] += 1
    return events


def _pitch(note: ET.Element) -> int | None:
    """MIDI pitch, or for an unpitched (drum) note its display position."""
    pitch = score.midi(note)
    if pitch is None and note.find("unpitched") is not None:
        step = note.findtext("unpitched/display-step")
        octave = note.findtext("unpitched/display-octave")
        if step in score.STEPS and octave and octave.isdigit():
            pitch = 12 * (int(octave) + 1) + score.STEPS[step]
    return pitch


def _letters(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalpha())


def names_agree(fragment: str, instrument: str) -> bool:
    """Whether a printed (often abbreviated, often misread) name can stand
    for the instrument: the letters of its first word, in order, within the
    instrument's name, starting with the same letter; and a trailing I/II,
    if either has one, the same. Only the first word, as OCR mangles the
    rest ("Clarinet in m"). "Picc." ~ Piccolo, "Tpt." ~ Trumpet in Bb,
    "V111. II" ~ Violin II, "E. Bass" ~ Electric Bass."""
    words, target = fragment.split(), instrument.split()
    numeral = lambda ws: ws[-1].lower() if len(ws) > 1 and ws[-1].upper() in ("I", "II", "III") else None
    if numeral(words) != numeral(target):
        return False
    body = _letters(words[0]) if words else ""
    if len(body) == 1 and len(words) > 1:           # "E. Bass": an initial and a word
        body += _letters(words[1])
    whole = _letters(" ".join(target[:-1] if numeral(target) else target))
    if not body or body[0] != whole[0]:
        return False
    rest = iter(whole)
    return all(letter in rest for letter in body)


def _sounding_bars(part: ET.Element) -> set[int]:
    return {i for i, m in enumerate(part.findall("measure")) if score.count_notes(m)}


def _first_clef(part: ET.Element) -> str | None:
    clef = part.find(".//clef/sign")
    return clef.text if clef is not None else None


def align(second: ET.Element, primary: ET.Element, aliases: dict) -> tuple[dict[str, str], list[str]]:
    """{second-read part id: instrument}, and report lines."""
    instruments = {name: p for (pid, name), p in
                   zip(score.part_names(primary), primary.findall("part"))}
    report = []
    mapping = {}
    for (pid, name), part in zip(score.part_names(second), second.findall("part")):
        bars = _sounding_bars(part)
        if not bars:
            continue
        mine = _events(part, bars)
        total = sum(mine.values())
        scores = {}
        for instrument, theirs_part in instruments.items():
            theirs = _events(theirs_part, bars)
            scores[instrument] = sum((mine & theirs).values()) / total if total else 0
        best = max(scores.values())
        if best < MIN_MATCH:
            report.append(f"  {pid} '{name}': matches nothing (best {best:.0%}), left out")
            continue
        tied = [i for i, s in scores.items() if best - s <= TIE]
        if len(tied) > 1:
            by_table = [i for i in tied if aliases.get(name) == i]
            by_name = [i for i in tied if names_agree(name, i)]
            by_clef = [i for i in tied if _first_clef(instruments[i]) == _first_clef(part)]
            for narrowed in (by_table, by_name, by_clef):
                if len(narrowed) == 1:
                    tied = narrowed
                    break
        if len(tied) > 1:
            report.append(f"  {pid} '{name}': ties between {tied} ({best:.0%}), left out")
            continue
        mapping[pid] = tied[0]
        report.append(f"  {pid} '{name}' -> {tied[0]} ({scores[tied[0]]:.0%} of its notes match)")
    return mapping, report


def _signature(measure: ET.Element, state: dict) -> list[tuple]:
    """Pitches and lengths in crotchets: two reads may count different ticks
    per crotchet (divisions) for the same rhythm."""
    divisions = None
    for attributes in measure.findall("attributes"):
        if attributes.findtext("divisions"):
            divisions = Fraction(attributes.findtext("divisions"))
    if divisions is None and ("divisions", "1") in state:
        divisions = Fraction(state[("divisions", "1")].text)
    divisions = divisions or Fraction(1)
    return [(_pitch(n), Fraction(n.findtext("duration") or 0) / divisions, n.find("chord") is not None)
            for n in measure.findall("note") if n.find("rest") is None]


def _bad_indices(part: ET.Element, assume) -> set[int]:
    numbers = {b.number for b in score.bad_bars(part, assume)}
    return {i for i, m in enumerate(part.findall("measure")) if (m.get("number") or "?") in numbers}


def fill(primary: ET.Element, second: ET.Element, assume=None) -> list[str]:
    """Replace primary bars that do not add up with second-read bars that do."""
    # First part of each name: merged instruments come before any fragment
    # left unmatched, which can carry the same name.
    seconds = {}
    for (_, name), p in zip(score.part_names(second), second.findall("part")):
        seconds.setdefault(name, p)
    report = []
    taken_total = 0
    for (_, name), part in zip(score.part_names(primary), primary.findall("part")):
        other = seconds.get(name)
        if other is None:
            continue
        mine, theirs = part.findall("measure"), other.findall("measure")
        if len(mine) != len(theirs):
            report.append(f"  {name}: the reads have {len(mine)} and {len(theirs)} bars; not combined")
            continue
        bad_mine, bad_theirs = _bad_indices(part, assume), _bad_indices(other, assume)
        states = {"p": merge.attribute_states(part), "s": merge.attribute_states(other)}
        # A silent bar in the second read where ours has notes is a fragment it
        # failed to match, not evidence: it is never taken or compared.
        silent_there = {i for i in range(len(mine))
                        if score.count_notes(mine[i]) and not score.count_notes(theirs[i])}
        sources = ["s" if i in bad_mine and i not in bad_theirs and i not in silent_there else "p"
                   for i in range(len(mine))]
        taken = [mine[i].get("number") for i, s in enumerate(sources) if s == "s"]
        disagree = [mine[i].get("number") for i in range(len(mine))
                    if i not in bad_mine and i not in bad_theirs and i not in silent_there
                    and _signature(mine[i], states["p"][i]) != _signature(theirs[i], states["s"][i])]
        neither = [mine[i].get("number") for i in range(len(mine)) if i in bad_mine and i in bad_theirs]
        if taken:
            rebuilt, current, previous = [], {}, None
            for i, source in enumerate(sources):
                bar = (mine if source == "p" else theirs)[i]
                wanted = states[source][i]
                if source != previous:
                    new = merge._restate(bar, wanted, current)
                    current = merge._end_state({**current, **wanted}, bar)
                else:
                    new = copy.deepcopy(bar)
                    current = merge._end_state(current, bar)
                rebuilt.append(new)
                previous = source
            for measure in mine:
                part.remove(measure)
            part.extend(rebuilt)
            taken_total += len(taken)
        line = f"  {name}: took bar(s) {', '.join(taken) or 'none'} from the second read"
        if disagree:
            line += f"; reads DISAGREE in bar(s) {', '.join(disagree)}"
        if neither:
            line += f"; neither read adds up in bar(s) {', '.join(neither)}"
        report.append(line)
    report.insert(0, f"bars taken from the second read: {taken_total}")
    return report
