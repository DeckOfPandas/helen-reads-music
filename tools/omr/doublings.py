#!/usr/bin/env python3
"""Doublings as a second opinion: parts that play the same notes.

An arranger doubles lines: harp and piano, bass guitar and double bass,
violins in unison, a flute an octave above the oboe. Where two staves play
the same notes (in concert pitch, perhaps octaves apart) in most bars
where both play, each is a check on the other. A bar of one that does not
add up while its double's does, and that shares at least half its notes
with it, is filled from the double: its notes copied and respelt from the
double's transposition to its own (a trumpet in Bb from an oboe: a major
second up), moved by the octaves between the two.

process.py runs this after the drawing check, before the hand patches;
bars the patches name are left alone.
"""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction

import drawing_check
import merge
import repair
import score

MIN_BARS = 8              # bars both play, and the same, before a pair is a doubling
MIN_SHARE = 0.6           # of the bars both play


@dataclass
class Staff:
    part: str                       # part name
    element: ET.Element             # the <part>
    staff: int
    staves: int
    chromatic: int                  # written to sounding, semitones (with octaves)
    diatonic: int = 0               # written to sounding, letter steps (with octaves)


def _transposition(part: ET.Element) -> tuple[int, int]:
    """(chromatic, diatonic) from written to sounding, octaves included."""
    transpose = part.find(".//attributes/transpose")
    if transpose is None:
        return 0, 0
    octaves = int(transpose.findtext("octave-change") or 0)
    return (int(transpose.findtext("chromatic") or 0) + 12 * octaves,
            int(transpose.findtext("diatonic") or 0) + 7 * octaves)


STEPS = "CDEFGAB"
NATURAL = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def _respell(pitch: ET.Element, diatonic: int, chromatic: int) -> bool:
    """Move a <pitch> by an interval: `diatonic` letter steps, `chromatic`
    semitones. False if that needs more than a double sharp or flat."""
    step, octave = pitch.findtext("step"), int(pitch.findtext("octave"))
    alter = int(float(pitch.findtext("alter") or 0))
    midi = 12 * (octave + 1) + NATURAL[step] + alter + chromatic
    index = STEPS.index(step) + 7 * octave + diatonic
    new_step, new_octave = STEPS[index % 7], index // 7
    new_alter = midi - (12 * (new_octave + 1) + NATURAL[new_step])
    if abs(new_alter) > 2:
        return False
    pitch.find("step").text = new_step
    pitch.find("octave").text = str(new_octave)
    element = pitch.find("alter")
    if new_alter and element is None:
        element = ET.Element("alter")
        pitch.insert(1, element)
    if new_alter:
        element.text = str(new_alter)
    elif element is not None:
        pitch.remove(element)
    return True


def staves_of(root: ET.Element) -> list[Staff]:
    out = []
    for (pid, name), part in zip(score.part_names(root), root.findall("part")):
        if part.find(".//unpitched") is not None:
            continue
        staves = max((int(a.findtext("staves")) for a in part.iter("attributes") if a.findtext("staves")), default=1)
        chromatic, diatonic = _transposition(part)
        for staff in range(1, staves + 1):
            out.append(Staff(name, part, staff, staves, chromatic, diatonic))
    return out


def _bar(measure: ET.Element, staff: Staff, divisions: Fraction) -> tuple[tuple, bool]:
    """(chords: (onset, length, sounding pitches)..., whether it has notes)."""
    time, last = Fraction(0), Fraction(0)
    chords: list[list] = []
    sounding = False
    for element in measure:
        if element.tag == "backup":
            time -= Fraction(element.findtext("duration") or 0) / divisions
        elif element.tag == "forward":
            time += Fraction(element.findtext("duration") or 0) / divisions
        elif element.tag == "note":
            if element.find("grace") is not None:
                continue
            on = int(element.findtext("staff") or 1) if staff.staves > 1 else 1
            length = Fraction(element.findtext("duration") or 0) / divisions
            chord = element.find("chord") is not None
            onset = last if chord else time
            if not chord:
                last = time
                time += length
            if on != staff.staff or element.find("pitch") is None:
                continue
            sounding = True
            pitch = score.midi(element) + staff.chromatic
            if chord and chords and chords[-1][0] == onset:
                chords[-1][2].append(pitch)
            else:
                chords.append([onset, length, [pitch]])
    shape = tuple(sorted((onset, length, tuple(sorted(p))) for onset, length, p in chords))
    return shape, sounding


def _shift(a: tuple, b: tuple) -> int | None:
    """The octaves a sounds above b, if their notes are the same otherwise."""
    if len(a) != len(b) or not a:
        return None
    shifts = set()
    for (onset_a, length_a, pa), (onset_b, length_b, pb) in zip(a, b):
        if onset_a != onset_b or length_a != length_b or len(pa) != len(pb):
            return None
        differences = {x - y for x, y in zip(pa, pb)}
        if len(differences) != 1:
            return None
        shifts |= differences
    if len(shifts) != 1:
        return None
    shift = shifts.pop()
    return shift // 12 if shift % 12 == 0 else None


def _shared(a: tuple, b: tuple, octaves: int) -> float:
    """The share of a's notes that b has too, moved by the octaves."""
    mine = Counter(p for _, _, ps in a for p in ps)
    theirs = Counter(p + 12 * octaves for _, _, ps in b for p in ps)
    total = sum(mine.values())
    return sum((mine & theirs).values()) / total if total else 0.0


def _chords(measure: ET.Element, staff: Staff) -> list[list[ET.Element]]:
    """The staff's notes (not rests) grouped into chords, in order."""
    chords: list[list[ET.Element]] = []
    for note in measure.findall("note"):
        on = int(note.findtext("staff") or 1) if staff.staves > 1 else 1
        if on != staff.staff or note.find("grace") is not None or note.find("pitch") is None:
            continue
        if note.find("chord") is not None and chords:
            chords[-1].append(note)
        else:
            chords.append([note])
    return chords


def _copy_notes(source: ET.Element, staff_from: Staff, staff_to: Staff, octaves: int,
                div_from: Fraction, div_to: Fraction, target: ET.Element | None = None
                ) -> tuple[list[list[ET.Element]], bool] | None:
    """The source staff's voices as streams for the target staff, durations
    rescaled; and whether the pitches are the target's own. A reader's usual
    mistake is rhythm, not pitch: when the `target` bar has as many chords
    of the same sizes as the source, it keeps its pitches and takes the
    source's rhythm (two doubled lines can part for a harmony note). Else
    the source's written pitches are respelt from its transposition to the
    target's, moved by the octaves between the two."""
    own = _chords(target, staff_to) if target is not None else []
    theirs = _chords(source, staff_from)
    keep = bool(own) and [len(c) for c in own] == [len(c) for c in theirs]
    own_pitches = iter(n.find("pitch") for chord in own for n in chord)
    chromatic = 12 * octaves + staff_from.chromatic - staff_to.chromatic
    diatonic = 7 * octaves + staff_from.diatonic - staff_to.diatonic
    streams: dict[str, list[ET.Element]] = {}
    base = 1 + 4 * (staff_to.staff - 1)
    names: dict[str, str] = {}
    for note in source.findall("note"):
        on = int(note.findtext("staff") or 1) if staff_from.staves > 1 else 1
        if on != staff_from.staff or note.find("grace") is not None:
            continue
        voice = note.findtext("voice") or "1"
        names.setdefault(voice, str(base + len(names)))
        new = copy.deepcopy(note)
        for lyric in new.findall("lyric"):
            new.remove(lyric)
        ticks = Fraction(new.findtext("duration") or 0) / div_from * div_to
        if ticks.denominator != 1:
            return None
        new.find("duration").text = str(ticks.numerator)
        new.find("voice").text = names[voice]
        staff_element = new.find("staff")
        if staff_to.staves > 1:
            if staff_element is None:
                repair._place(new, repair._text_element("staff", str(staff_to.staff)))
            else:
                staff_element.text = str(staff_to.staff)
        elif staff_element is not None:
            new.remove(staff_element)
        pitch = new.find("pitch")
        if pitch is not None:
            if keep:
                _swap(new, pitch, copy.deepcopy(next(own_pitches)))
            elif not _respell(pitch, diatonic, chromatic):
                return None
        streams.setdefault(voice, []).append(new)
    return list(streams.values()), keep


def _swap(parent: ET.Element, old: ET.Element, new: ET.Element) -> None:
    index = list(parent).index(old)
    parent.remove(old)
    parent.insert(index, new)


def doublings(root: ET.Element, assume: Fraction | None = None,
              skip: set[tuple[str, str]] | None = None) -> list[str]:
    """Find doubled staves; fill a bar that does not add up from its double.
    Report lines."""
    skip = skip or set()
    staves = staves_of(root)
    numbers = [m.get("number") for m in root.find("part").findall("measure")]
    info: dict[int, dict[str, tuple]] = {}
    contexts: dict[int, dict[str, tuple[ET.Element, Fraction, Fraction | None]]] = {}
    for index, staff in enumerate(staves):
        states = merge.attribute_states(staff.element)
        info[index], contexts[index] = {}, {}
        for measure, state in zip(staff.element.findall("measure"), states):
            divisions = repair._divisions(measure, state)
            if divisions is None:
                continue
            own = [t for t in (score._time_signature(a) for a in measure.findall("attributes")) if t]
            expected = own[-1] if own else (score._time_signature(repair._wrap(state[("time", "1")]))
                                            if ("time", "1") in state else assume)
            info[index][measure.get("number")] = _bar(measure, staff, divisions)
            contexts[index][measure.get("number")] = (measure, divisions, expected)

    def adds_up(index: int, number: str) -> bool:
        measure, divisions, expected = contexts[index][number]
        if expected is None:
            return True
        staff = staves[index]
        voices: Counter = Counter()
        for note in measure.findall("note"):
            on = int(note.findtext("staff") or 1) if staff.staves > 1 else 1
            if on != staff.staff or note.find("chord") is not None or note.find("grace") is not None:
                continue
            voices[note.findtext("voice") or "1"] += Fraction(note.findtext("duration") or 0) / divisions
        return all(v == expected for v in voices.values())

    report: list[str] = []
    filled = 0
    pairs = []
    for i in range(len(staves)):
        for j in range(i + 1, len(staves)):
            if staves[i].element is staves[j].element:
                continue
            both, same, shifts = 0, 0, Counter()
            for number in numbers:
                a, b = info[i].get(number), info[j].get(number)
                if not a or not b or not a[1] or not b[1]:
                    continue
                both += 1
                shift = _shift(a[0], b[0])
                if shift is not None:
                    same += 1
                    shifts[shift] += 1
            if same >= MIN_BARS and same >= MIN_SHARE * both:
                pairs.append((i, j, shifts.most_common(1)[0][0], same, both))
    for i, j, octaves, same, both in pairs:
        a, b = staves[i], staves[j]
        label = lambda s: s.part + (f" staff {s.staff}" if s.staves > 1 else "")  # noqa: E731
        where = f"{label(a)} = {label(b)}" + (f" {abs(octaves)} octave(s) {'above' if octaves > 0 else 'below'}"
                                              if octaves else "")
        report.append(f"doubling: {where}, the same in {same} of the {both} bars both play")
        for number in numbers:
            if number not in info[i] or number not in info[j]:
                continue
            for target, source, shift in ((i, j, octaves), (j, i, -octaves)):
                t, s = info[target][number], info[source][number]
                if not t[1] or not s[1] or _shift(t[0], s[0]) is not None:
                    continue
                if adds_up(target, number) or not adds_up(source, number):
                    continue
                name = staves[target].part
                if (name, number) in skip:
                    continue
                if _shared(t[0], s[0], shift) < 0.5:
                    report.append(f"  bar {number} {label(staves[target])}: does not add up; its double "
                                  f"{label(staves[source])} does, but shares too few notes to copy")
                    continue
                measure, div_to, _ = contexts[target][number]
                source_measure, div_from, _ = contexts[source][number]
                copied = _copy_notes(source_measure, staves[source], staves[target], shift, div_from, div_to,
                                     measure)
                if copied is None:
                    report.append(f"  bar {number} {label(staves[target])}: does not add up; its double "
                                  f"{label(staves[source])} does, but its notes could not be respelt")
                    continue
                streams, kept = copied
                drawing_check.replace_staff_notes(measure, staves[target].staff, staves[target].staves,
                                                  streams, div_to)
                info[target][number] = _bar(measure, staves[target], div_to)
                filled += 1
                report.append(f"  bar {number} {label(staves[target])}: did not add up; rhythm from "
                              f"{label(staves[source])}" + (", its own pitches kept" if kept else
                                                            ", pitches too (check them)"))
    report.insert(0, f"doublings: {len(pairs)} pair(s) of staves found; {filled} bar(s) filled from a double")
    return report
