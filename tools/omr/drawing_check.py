#!/usr/bin/env python3
"""Check and correct a read score against the drawing of its engraved PDF.

process.py runs this on the merged, repaired score, before the hand
patches, when the piece's PDFs are vector drawings and
library/<piece>/glyphs.json names their symbols (see vector.py,
vector_read.py). For every page:

1. Staves are matched to the score's parts (a two-stave part to two
   staves), top to bottom in score order, skipping parts the page hides, by
   how closely each stave's symbol counts per bar match each part's.
2. Key signatures: the flats or sharps drawn after each stave's clef, or
   at a bar line, are the key from there; where the score's key in force
   differs, the score's is corrected (Audiveris can lose a key change, and
   a merge can re-state a fragment's stale key).
3. Every bar of every matched stave is read from the drawing (vector_read),
   ties included. Where the reading adds up and differs from the score:
   - if only the ties differ, they are set as drawn;
   - if the score only lacks rests, they are put in where drawn;
   - otherwise the stave's notes are replaced by the reading, x and slash
     heads as drawn; each old lyric moves to the new note at its place on
     the page, and directions keep their beat.
   A drum part is written from the reading bar by bar, voices as drawn, a
   voice the page leaves without rests padded with invisible ones.
4. Where the reading does not add up (tuplets, grace notes) but the symbol
   counts differ, the bar is reported for checking by eye.

The notes of bars named in `skip` (hand patches) are left alone; `only`
checks just those, after the patches, adding the ties drawn and removing
none (a tie can hide from the drawing behind another voice's notes).
"""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import merge
import repair
import score
import vector
import vector_read

NOTEHEADS = {"notehead-black", "notehead-half", "notehead-whole", "notehead-x", "notehead-slash"}
REST_KINDS = {"rest-whole": "whole", "rest-quarter": "quarter", "rest-eighth": "eighth"}
KINDS = ("notes", "whole", "quarter", "eighth", "dots")


@dataclass
class Slot:
    part: str
    staff: int
    bars: dict[str, Counter] = field(default_factory=dict)       # bar number -> counts


def _staves_of(part: ET.Element) -> int:
    return max((int(a.findtext("staves")) for a in part.iter("attributes") if a.findtext("staves")), default=1)


def _read_counts(root: ET.Element) -> list[Slot]:
    """Per part and staff, per bar: noteheads, rests by kind, augmentation dots."""
    slots = []
    for (pid, name), part in zip(score.part_names(root), root.findall("part")):
        staves = _staves_of(part)
        part_slots = [Slot(name, s) for s in range(1, staves + 1)]
        for measure in part.findall("measure"):
            for slot in part_slots:
                slot.bars[measure.get("number")] = _measure_counts(measure, slot.staff, staves)
        slots += part_slots
    return slots


def _measure_counts(measure: ET.Element, staff: int, staves: int) -> Counter:
    counts = Counter()
    for note in measure.findall("note"):
        if note.find("grace") is not None or note.get("print-object") == "no":
            continue
        if staves > 1 and int(note.findtext("staff") or 1) != staff:
            continue
        rest = note.find("rest")
        if rest is None:
            counts["notes"] += 1
            if note.find("chord") is None:
                counts["dots"] += len(note.findall("dot"))
            continue
        kind = note.findtext("type")
        if rest.get("measure") == "yes" or kind in ("whole", "half"):
            counts["whole"] += 1
        elif kind in ("quarter", "eighth"):
            counts[kind] += 1
    return counts


def _drawn_counts(page: vector.Page, stave: int) -> list[Counter]:
    counts = [Counter() for _ in range(page.bar_count(stave))]
    heads = [g for g in page.glyphs if g.stave == stave and g.bar is not None and g.name in NOTEHEADS]
    for glyph in page.glyphs:
        if glyph.stave != stave or glyph.bar is None or glyph.bar >= len(counts):
            continue
        if glyph.name in NOTEHEADS:
            counts[glyph.bar]["notes"] += 1
        elif glyph.name in REST_KINDS:
            counts[glyph.bar][REST_KINDS[glyph.name]] += 1
        elif glyph.name == "dot":
            gap = page.staves[stave].gap
            if any(0 < glyph.x0 - h.x1 < gap * 1.6 and abs(glyph.cy - h.cy) < gap * 0.8 for h in heads):
                counts[glyph.bar]["dots"] += 1
    return counts


def _distance(a: Counter, b: Counter) -> int:
    return sum(abs(a[k] - b[k]) for k in KINDS)


def _match_staves(drawn: list[list[Counter]], slots: list[Slot], bars: list[str]) -> list[int | None]:
    """For each drawn stave, top to bottom, the index of its slot: a monotonic
    alignment that may skip slots (hidden staves) but not staves, at the
    least total count difference."""
    big = 10 ** 9
    n, m = len(drawn), len(slots)
    cost = [[sum(_distance(drawn[i][k] if k < len(drawn[i]) else Counter(), slots[j].bars.get(b, Counter()))
                 for k, b in enumerate(bars))
             for j in range(m)] for i in range(n)]
    best = [[big] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        best[0][j] = 0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            skip = best[i][j - 1]
            take = best[i - 1][j - 1] + cost[i - 1][j - 1]
            if take <= skip:
                best[i][j], back[i][j] = take, (i - 1, j - 1)
            else:
                best[i][j], back[i][j] = skip, (i, j - 1)
    result: list[int | None] = [None] * n
    i, j = n, m
    while i > 0 and j > 0:
        step = back[i][j]
        if step == (i - 1, j - 1):
            result[i - 1] = j - 1
        i, j = step
    return result


# --- what was read, per voice ------------------------------------------------------

def _score_voices(measure: ET.Element, staff: int, staves: int, divisions: Fraction) -> list[tuple]:
    """Per voice: ((length, sorted (midi pitch, tie) pairs), ...), sorted,
    comparable with the drawing's. A tie is "" or a mix of "<" (from the note
    before) and ">" (on to the next)."""
    voices: dict[str, list] = defaultdict(list)
    for note in measure.findall("note"):
        if note.find("grace") is not None or (staves > 1 and int(note.findtext("staff") or 1) != staff):
            continue
        voice = note.findtext("voice") or "1"
        types = {t.get("type") for t in note.findall("tie")}
        head = (score.midi(note), ("<" if "stop" in types else "") + (">" if "start" in types else ""))
        if note.find("chord") is not None and voices[voice]:
            length, heads = voices[voice][-1]
            voices[voice][-1] = (length, tuple(sorted(heads + (head,))))
            continue
        length = Fraction(note.findtext("duration") or 0) / divisions
        heads = () if note.find("rest") is not None or note.find("pitch") is None else (head,)
        voices[voice].append((length, heads))
    return sorted(tuple(v) for v in voices.values() if v)


def _drawn_voices(voices: dict[str, list[vector_read.Event]], expected: Fraction) -> list[tuple]:
    out = []
    for events in voices.values():
        if len(events) == 1 and events[0].whole_bar:
            out.append(((expected, ()),))
            continue
        out.append(tuple((e.length, tuple(sorted(
            (vector_read.midi_of(h.pitch), ("<" if h.tie_stop else "") + (">" if h.tie_start else ""))
            for h in e.heads))) for e in events))
    return sorted(out)


# --- writing a reading into the score ------------------------------------------------

def _pitched_notes(events: list[vector_read.Event], voice: str, staff: int | None, staves: int,
                   divisions: Fraction, expected: Fraction,
                   origin: tuple[float, float] | None = None) -> list[ET.Element]:
    """MusicXML notes for one voice's events. With `origin` (the bar's left
    x and the stave's line gap), each note gets its default-x: tenths of a
    space from the bar line to its head's left edge, the measure Audiveris
    writes, so the old notes' lyrics can find the new notes they belong to."""
    notes = []
    for event in events:
        length = expected if event.whole_bar else event.length
        tremolo = next((m for m in event.marks if m.startswith("tremolo")), None)
        lefts = [h.left for h in event.heads if h.left is not None]
        for index, head in enumerate(sorted(event.heads, key=lambda h: h.step) or [None]):
            note = ET.Element("note")
            if origin is not None and lefts:
                note.set("default-x", str(round((min(lefts) - origin[0]) / origin[1] * 10)))
            if index:
                note.append(ET.Element("chord"))
            if head is None:
                note.append(ET.Element("rest", {"measure": "yes"} if event.whole_bar else {}))
            else:
                letter, alter, octave = head.pitch
                pitch = ET.SubElement(note, "pitch")
                ET.SubElement(pitch, "step").text = letter
                if alter:
                    ET.SubElement(pitch, "alter").text = str(alter)
                ET.SubElement(pitch, "octave").text = str(octave)
            note.append(repair._text_element("duration", "0"))
            repair._place(note, repair._text_element("voice", voice))
            if staves > 1:
                repair._place(note, repair._text_element("staff", str(staff)))
            if tremolo:
                note.find("duration").text = str(int(length * divisions))
                written = "whole" if head is not None and head.kind == "whole" else "half"
                repair._place(note, repair._text_element("type", written))
                modification = ET.Element("time-modification")
                ET.SubElement(modification, "actual-notes").text = "2"
                ET.SubElement(modification, "normal-notes").text = "1"
                repair._place(note, modification)
            else:
                repair._set_length(note, length, divisions)
            if head is not None and event.stem:
                repair._place(note, repair._text_element("stem", event.stem))
            if head is not None and head.kind in ("x", "slash"):
                repair._place(note, repair._text_element("notehead", head.kind))
            notations = ET.Element("notations")
            ties = [kind for kind, on in (("stop", head is not None and head.tie_stop),
                                          ("start", head is not None and head.tie_start)) if on]
            for kind in ties:
                repair._place(note, ET.Element("tie", {"type": kind}))
                ET.SubElement(notations, "tied", {"type": kind})
            if head is not None and index == 0:
                if tremolo:
                    ornaments = ET.SubElement(notations, "ornaments")
                    ET.SubElement(ornaments, "tremolo", {"type": tremolo.split("-")[1]}).text = "3"
                marks = [m for m in ("accent", "strong", "tenuto", "staccato") if m in event.marks]
                if marks:
                    articulations = ET.SubElement(notations, "articulations")
                    for mark in marks:
                        ET.SubElement(articulations, {"strong": "strong-accent"}.get(mark, mark))
            if len(notations):
                repair._place(note, notations)
            notes.append(note)
    return notes


def _rebuild(measure: ET.Element, streams: list[list[ET.Element]], divisions: Fraction) -> None:
    """Replace a bar's notes with these voice streams, each from the bar's
    start, a backup between. Directions (dynamics, words) and mid-bar
    attributes keep their onsets: they go into the first stream before its
    first note at or after them."""
    time, seen_note = 0, False
    movable: list[tuple[int, ET.Element]] = []
    for element in list(measure):
        if element.tag == "note":
            seen_note = True
            if element.find("chord") is None and element.find("grace") is None:
                time += int(element.findtext("duration") or 0)
            measure.remove(element)
        elif element.tag in ("backup", "forward"):
            step = int(element.findtext("duration") or 0)
            time += step if element.tag == "forward" else -step
            measure.remove(element)
        elif element.tag == "barline" or element.tag == "print" or (element.tag == "attributes" and not seen_note):
            continue
        else:
            movable.append((time, element))
            measure.remove(element)
    movable.sort(key=lambda m: m[0])
    position = len(measure)
    while position and measure[position - 1].tag == "barline" and measure[position - 1].get("location") != "left":
        position -= 1
    new: list[ET.Element] = []
    previous = None
    for stream in streams:
        if not stream:
            continue
        if previous is not None:
            back = ET.Element("backup")
            ET.SubElement(back, "duration").text = str(sum(
                int(n.findtext("duration")) for n in previous if n.find("chord") is None))
            new.append(back)
        if previous is None:
            onset = 0
            for note in stream:
                if note.find("chord") is None:
                    while movable and movable[0][0] <= onset:
                        new.append(movable.pop(0)[1])
                    onset += int(note.findtext("duration"))
                new.append(note)
            new += [element for _, element in movable]
            movable = []
        else:
            new += stream
        previous = stream
    new += [element for _, element in movable]
    for offset, element in enumerate(new):
        measure.insert(position + offset, element)


def _strip_ties(voices: list[tuple]) -> list[tuple]:
    return sorted(tuple((length, tuple(p for p, _ in heads)) for length, heads in v) for v in voices)


def _set_ties(measure: ET.Element, staff: int, staves: int, drawn: list[tuple], keep: bool = False) -> None:
    """Make the staff's ties those drawn, the notes otherwise matching; with
    `keep` (a bar patched by hand), add the drawn ties and keep its own."""
    voices: dict[str, list[list[ET.Element]]] = defaultdict(list)
    for note in measure.findall("note"):
        if note.find("grace") is not None or (staves > 1 and int(note.findtext("staff") or 1) != staff):
            continue
        voice = note.findtext("voice") or "1"
        if note.find("chord") is not None and voices[voice]:
            voices[voice][-1].append(note)
        else:
            voices[voice].append([note])
    remaining = list(drawn)
    for events in voices.values():
        pitches = tuple(tuple(sorted(score.midi(n) for n in chord if n.find("pitch") is not None))
                        for chord in events)
        match = next((v for v in remaining if tuple(tuple(p for p, _ in heads) for _, heads in v) == pitches),
                     None)
        if match is None:
            continue
        remaining.remove(match)
        for chord, (_, heads) in zip(events, match):
            pitched = sorted((n for n in chord if n.find("pitch") is not None), key=score.midi)
            for note, (_, tie) in zip(pitched, heads):
                had = {old.get("type") for old in note.findall("tie")} if keep else set()
                for old in note.findall("tie"):
                    note.remove(old)
                for notations in note.findall("notations"):
                    for old in notations.findall("tied"):
                        notations.remove(old)
                kinds = [kind for kind, mark in (("stop", "<"), ("start", ">")) if mark in tie or kind in had]
                for kind in kinds:
                    repair._place(note, ET.Element("tie", {"type": kind}))
                if kinds:
                    notations = note.find("notations")
                    if notations is None:
                        notations = ET.Element("notations")
                        repair._place(note, notations)
                    for kind in kinds:
                        ET.SubElement(notations, "tied", {"type": kind})
                for notations in note.findall("notations"):
                    if not len(notations):
                        note.remove(notations)


def _replace_staff(measure: ET.Element, staff: int, staves: int, voices: dict[str, list[vector_read.Event]],
                   divisions: Fraction, expected: Fraction, origin: tuple[float, float] | None = None) -> None:
    """The drawing's voices for one staff; other staves' notes kept as they were."""
    base = 1 + 4 * (staff - 1)
    mine = [_pitched_notes(events, str(base + index), staff, staves, divisions, expected, origin)
            for index, events in enumerate(voices.values())]
    replace_staff_notes(measure, staff, staves, mine, divisions)


LYRIC_REACH = 12        # tenths: how far a lyric's old note may sit from the new note it moves to


def _carry_lyrics(old: list[tuple[str | None, list[ET.Element]]], heads: list[ET.Element]) -> None:
    """Put the old notes' lyrics on the new notes. Where every note on both
    sides has a default-x, each old note's lyrics go to the new note nearest
    it, within LYRIC_REACH: a note gained or lost then takes no lyric from
    its neighbours. Otherwise only when the notes line up one to one."""
    def tenths(value: str | None) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    sung = [(tenths(where), lyrics) for where, lyrics in old if lyrics]
    if not sung:
        return
    at = [(tenths(h.get("default-x")), h) for h in heads]
    if all(where is not None for where, _ in sung) and all(where is not None for where, _ in at):
        for where, lyrics in sung:
            distance, near = min(((abs(x - where), h) for x, h in at), key=lambda d: d[0], default=(None, None))
            if near is None or distance > LYRIC_REACH:
                continue
            taken = {l.get("number", "1") for l in near.findall("lyric")}
            for lyric in lyrics:
                if lyric.get("number", "1") not in taken:
                    repair._place(near, lyric)
        return
    if len(heads) == len(old):
        for note, (_, lyrics) in zip(heads, old):
            for lyric in lyrics:
                repair._place(note, lyric)


def replace_staff_notes(measure: ET.Element, staff: int, staves: int, mine: list[list[ET.Element]],
                        divisions: Fraction) -> None:
    """One staff's notes replaced by these voice streams (each from the
    bar's start); other staves' notes kept; the old lyrics carried over
    (`_carry_lyrics`)."""
    kept: dict[tuple[int, str], list[ET.Element]] = defaultdict(list)
    old: list[tuple[str | None, list[ET.Element]]] = []
    for note in measure.findall("note"):
        on = int(note.findtext("staff") or 1) if staves > 1 else 1
        if on == staff:
            if note.find("chord") is None and note.find("rest") is None:
                old.append((note.get("default-x"), [copy.deepcopy(l) for l in note.findall("lyric")]))
            continue
        kept[(on, note.findtext("voice") or "1")].append(copy.deepcopy(note))
    heads = [n for stream in mine for n in stream if n.find("chord") is None and n.find("rest") is None]
    _carry_lyrics(old, heads)
    streams = []
    for s in range(1, staves + 1):
        if s == staff:
            streams += mine
        else:
            streams += [v for (on, _), v in sorted(kept.items()) if on == s]
    _rebuild(measure, streams, divisions)


def _drum_events(events: list[vector_read.Event], expected: Fraction) -> list[repair.DrumEvent]:
    out = []
    for event in events:
        length = expected if event.whole_bar else event.length
        heads = []
        for head in event.heads:
            letter, octave = vector_read.pitch_at(head.step, ("percussion", 2))
            heads.append(repair.DrumHead(letter, octave, cross=head.kind == "x", open="open" in event.marks,
                                         slash=head.kind == "slash", tie_stop=head.tie_stop))
        out.append(repair.DrumEvent(heads, length, accent="accent" in event.marks, strong="strong" in event.marks,
                                    tie_start=any(h.tie_start for h in event.heads)))
    return out


def _pad(voices: dict[str, list[vector_read.Event]], expected: Fraction) -> dict[str, list[repair.DrumEvent]] | None:
    """Drum voices as DrumEvents; one voice the page leaves short (no rests
    drawn) is padded with invisible rests, placed by matching its first
    event's x to the other voice's onsets."""
    names = list(voices)
    full = {n: vector_read.span(voices[n]) == expected or
            (len(voices[n]) == 1 and voices[n][0].whole_bar) for n in names}
    out = {n: _drum_events(voices[n], expected) for n in names}
    short = [n for n in names if not full[n]]
    if not short:
        return out
    if len(short) != 1 or len(names) != 2:
        return None
    gap_voice, whole_voice = short[0], [n for n in names if n != short[0]][0]
    onsets, t = [], Fraction(0)
    for event in voices[whole_voice]:
        onsets.append((event.x, t))
        t += event.length
    first_x = voices[gap_voice][0].x if voices[gap_voice] else None
    start = min(onsets, key=lambda o: abs(o[0] - first_x))[1] if first_x is not None and onsets else Fraction(0)
    end = expected - start - vector_read.span(voices[gap_voice])
    if end < 0:
        return None
    padded = []
    if start:
        padded.append(repair.DrumEvent([], start, hidden=True))
    padded += out[gap_voice]
    if end:
        padded.append(repair.DrumEvent([], end, hidden=True))
    out[gap_voice] = padded
    return out


# --- key signatures ---------------------------------------------------------------

def drawn_keys(page: vector.Page, stave_index: int) -> list[int | None]:
    """Per bar of a stave, the fifths of a key signature drawn at its start,
    or None where none is. The first bar's is the one after the clef (0
    when the clef has no flats or sharps after it); a later bar's is a
    cluster of flats, sharps or naturals between its bar line and its first
    note or rest (naturals alone: C major). An accidental just left of a
    notehead at its height belongs to that note, not to a key."""
    stave = page.staves[stave_index]
    gap = stave.gap
    lines = page.bar_lines[stave_index]
    mine = [g for g in page.glyphs if g.stave == stave_index]
    heads = [g for g in mine if g.name in NOTEHEADS]
    music = [g for g in mine if g.name in NOTEHEADS or g.name.startswith("rest")]
    clefs = sorted((g for g in mine if g.name.startswith("clef")), key=lambda g: g.x0)

    owned = {id(sign) for sign in vector_read.accidentals_of(
        heads, [g for g in mine if g.name in vector_read.ACCIDENTALS], gap).values()}

    def on_a_note(sign: vector.Glyph) -> bool:
        return id(sign) in owned

    keys: list[int | None] = []
    for k in range(page.bar_count(stave_index)):
        start, end = lines[k], lines[k + 1]
        if k == 0:
            if not clefs or clefs[0].x0 > end:
                keys.append(None)
                continue
            start = clefs[0].x1
        first = min((g.x0 for g in music if start < g.x0 < end), default=end)
        signs = [g for g in mine if g.name in ("flat", "sharp", "natural") and start < g.x0 < first
                 and not on_a_note(g)]
        flats = sum(g.name == "flat" for g in signs)
        sharps = sum(g.name == "sharp" for g in signs)
        if flats and sharps:
            keys.append(None)
        elif flats or sharps:
            keys.append(sharps - flats)
        else:
            keys.append(0 if k == 0 or signs else None)
    return keys


def _fix_key(part: ET.Element, number: str, fifths: int) -> None:
    measure = next(m for m in part.findall("measure") if m.get("number") == number)
    key = measure.find("attributes/key")
    if key is None:
        attributes = measure.find("attributes")
        if attributes is None:
            attributes = ET.Element("attributes")
            position = 0
            while position < len(measure) and measure[position].tag == "print":
                position += 1
            measure.insert(position, attributes)
        key = ET.Element("key")
        ET.SubElement(key, "fifths")
        repair._insert_in_order(attributes, key)
    key.find("fifths").text = str(fifths)


def _key_in_force(part: ET.Element, number: str) -> int:
    fifths = 0
    for measure in part.findall("measure"):
        for key in measure.iter("key"):
            if key.findtext("fifths") not in (None, "null"):
                fifths = int(key.findtext("fifths"))
        if measure.get("number") == number:
            break
    return fifths


# --- the pass -----------------------------------------------------------------------

@dataclass
class Finding:
    page: int
    bar: str
    part: str
    staff: int
    what: str
    fixed: bool = False

    def describe(self) -> str:
        where = f"{self.part}" + (f" staff {self.staff}" if self.staff > 1 else "")
        return f"page {self.page} bar {self.bar} {where}: {self.what}" + (" -- FIXED" if self.fixed else "")


def _insert_missing_rests(measure: ET.Element, staff: int, staves: int, drawn_events: list[vector_read.Event],
                          divisions: Fraction, expected: Fraction) -> bool:
    """Put in the rests a single-voice staff lacks where the drawing has them,
    keeping every note (and its ties, lyrics and marks). Only if that is the
    whole difference and the voice then fills the bar."""
    notes = [n for n in measure.findall("note")
             if (int(n.findtext("staff") or 1) if staves > 1 else 1) == staff and n.find("grace") is None]
    voices = {n.findtext("voice") or "1" for n in notes}
    if len(voices) != 1:
        return False
    voice = voices.pop()
    events = [n for n in notes if n.find("chord") is None]
    # A rest matches a rest of its length; a note any note (the caller
    # compares pitches afterwards).
    kinds = [("rest", Fraction(int(n.findtext("duration") or 0)) / divisions) if n.find("rest") is not None
             else ("note",) for n in events]
    drawn = [("rest", expected if e.whole_bar else e.length) if not e.heads else ("note",) for e in drawn_events]
    inserts, i, j = [], 0, 0
    while i < len(drawn):
        if j < len(kinds) and drawn[i] == kinds[j]:
            i, j = i + 1, j + 1
        elif drawn[i][0] == "rest":
            inserts.append((j, drawn_events[i].length))
            i += 1
        else:
            return False
    if j != len(kinds) or not inserts:
        return False
    trial = copy.deepcopy(measure)
    trial_events = [n for n in trial.findall("note")
                    if (int(n.findtext("staff") or 1) if staves > 1 else 1) == staff
                    and n.find("grace") is None and n.find("chord") is None]
    for position, length in sorted(inserts, reverse=True):
        rest = ET.Element("note")
        rest.append(ET.Element("rest"))
        rest.append(repair._text_element("duration", "0"))
        repair._place(rest, repair._text_element("voice", voice))
        if staves > 1:
            repair._place(rest, repair._text_element("staff", str(staff)))
        try:
            repair._set_length(rest, length, divisions)
        except ValueError:
            return False
        if position < len(trial_events):
            index = list(trial).index(trial_events[position])
        else:
            last = trial_events[-1]
            index = list(trial).index(last) + 1
            while index < len(trial) and trial[index].tag == "note" and trial[index].find("chord") is not None:
                index += 1
        trial.insert(index, rest)
    if repair._voice_span(trial, voice, divisions) != expected:
        return False
    repair._fix_following_backup(trial, voice, divisions)
    for element in list(measure):
        measure.remove(element)
    for element in list(trial):
        measure.append(element)
    return True


def check(root: ET.Element, piece: Path, pdfs: list[Path], fix: bool = True,
          assume: Fraction | None = None, skip: set[tuple[str, str]] | None = None,
          only: set[tuple[str, str]] | None = None) -> tuple[list[Finding], list[str]]:
    """Check (and with `fix`, correct) every bar against the drawing. The
    notes of the (part, bar) pairs in `skip` are left alone, unchecked;
    with `only`, just those pairs' notes are checked, and only their ties
    corrected."""
    import pymupdf
    skip = skip or set()
    catalogue = vector.load_catalogue(piece)
    if not catalogue and not vector.has_smufl(pdfs):
        return [], ["drawing check: no glyphs.json for this piece (vector.py survey makes one); skipped"]
    slots = _read_counts(root)
    numbers = [m.get("number") for m in root.find("part").findall("measure")]
    parts = {name: p for (pid, name), p in zip(score.part_names(root), root.findall("part"))}
    findings: list[Finding] = []
    notes_lines = []
    layout = []
    counts = Counter()
    first = 0
    systems = []                                    # (page number, page, beams, curves, staves of one system)
    for page_number, (pdf, index) in enumerate(vector.pdf_pages(pdfs), start=1):
        with pymupdf.open(pdf) as doc:
            pg = doc[index]
            page = vector.read_page(pg)
            beams = vector_read.beams_on_page(pg)
            curves = vector_read.curves_on_page(pg)
        if not page.staves:
            notes_lines.append(f"page {page_number}: no staves drawn (a scan?)")
            continue
        vector.name_glyphs(page, catalogue)
        systems += [(page_number, page, beams, curves, system) for system in page.systems(len(slots))]
    for page_number, page, beams, curves, system in systems:
        count = page.bar_count(system[0])
        bars = numbers[first:first + count]
        first += count
        if not bars:
            break
        drawn_counts = {s: _drawn_counts(page, s) for s in system}
        mapping = dict(zip(system, _match_staves([drawn_counts[s] for s in system], slots, bars)))
        layout.append(f"page {page_number} (bars {bars[0]}-{bars[-1]}): " + ", ".join(
            f"{stave}={slots[j].part}" + (f"/{slots[j].staff}" if _staves_of(parts[slots[j].part]) > 1 else "")
            for stave, j in mapping.items() if j is not None))
        for stave, slot_index in mapping.items():
            if slot_index is None:
                notes_lines.append(f"page {page_number}: stave {stave} matched to no part")
                continue
            slot = slots[slot_index]
            part = parts[slot.part]
            drums = part.find(".//unpitched") is not None
            staves = _staves_of(part)
            states = dict(zip([m.get("number") for m in part.findall("measure")], merge.attribute_states(part)))
            measures = {m.get("number"): m for m in part.findall("measure")}
            clef = vector_read.drawn_clef(page, stave) or ("G", 2)
            keys = drawn_keys(page, stave) if not drums else []
            key_now = None
            for k, number in enumerate(bars):
                measure = measures.get(number)
                if measure is None:
                    continue
                if k < len(keys) and keys[k] is not None:
                    key_now = keys[k]
                if only is not None and (slot.part, number) not in only:
                    continue
                # The drawn key is the key: a stale or lost one in the score is corrected.
                if key_now is not None and slot.staff == 1 and key_now != _key_in_force(part, number):
                    findings.append(Finding(page_number, number, slot.part, 1,
                                            f"key drawn {key_now}, score {_key_in_force(part, number)}", fix))
                    if fix:
                        _fix_key(part, number, key_now)
                        counts["keys"] += 1
                if only is None and (slot.part, number) in skip:
                    continue
                divisions = repair._divisions(measure, states[number])
                own = [score._time_signature(a) for a in measure.findall("attributes")]
                own = [t for t in own if t]
                state = states[number]
                expected = own[-1] if own else (score._time_signature(repair._wrap(state[("time", "1")]))
                                                if ("time", "1") in state else assume)
                if expected is None or divisions is None:
                    continue
                voices = vector_read.read_bar(page, beams, stave, k, expected, prefer_two=drums, curves=curves)
                if voices is None:
                    continue
                read_counts = _measure_counts(measure, slot.staff, staves)
                if drums:
                    if not voices or (slot.part, number) in skip:
                        continue
                    if not vector_read.adds_up(voices, expected) and _pad(voices, expected) is None:
                        # Some engravers write the kit as one voice, stems either way.
                        one = vector_read.read_bar(page, beams, stave, k, expected, curves=curves)
                        if one and vector_read.adds_up(one, expected):
                            voices = one
                    if vector_read.adds_up(voices, expected) or _pad(voices, expected) is not None:
                        drum_voices = _pad(voices, expected)
                        if drum_voices is None:
                            continue
                        up = drum_voices.get("up", [])
                        down = drum_voices.get("down", [])
                        if "single" in voices:              # one voice: up or down by its stems
                            stems = Counter(e.stem for e in voices["single"] if e.stem)
                            if stems["down"] > stems["up"]:
                                down = drum_voices["single"]
                            else:
                                up = drum_voices["single"]
                        if fix:
                            before = copy.deepcopy(measure)
                            _rebuild(measure, [repair._drum_notes(up, "1", "up", divisions),
                                               repair._drum_notes(down, "2", "down", divisions)], divisions)
                            if ET.tostring(before) != ET.tostring(measure):
                                counts["drum bars written"] += 1
                    else:
                        if _distance(drawn_counts[stave][k], read_counts):
                            findings.append(Finding(page_number, number, slot.part, slot.staff,
                                                    "drum bar the drawing could not be read in"))
                    continue
                if not voices or not vector_read.adds_up(voices, expected):
                    if _distance(drawn_counts[stave][k], read_counts):
                        findings.append(Finding(page_number, number, slot.part, slot.staff,
                                                f"counts differ ({_count_diff(drawn_counts[stave][k], read_counts)}); "
                                                "the drawing's reading does not add up (tuplets? grace notes?)"))
                    continue
                fifths = key_now if key_now is not None else _key_in_force(part, number)
                vector_read.spell(voices, clef, fifths)
                mine = _drawn_voices(voices, expected)
                theirs = _score_voices(measure, slot.staff, staves, divisions)
                if mine == theirs:
                    continue
                what = _describe_difference(mine, theirs)
                finding = Finding(page_number, number, slot.part, slot.staff, what)
                findings.append(finding)
                if not fix:
                    continue
                if _strip_ties(mine) == _strip_ties(theirs):
                    _set_ties(measure, slot.staff, staves, mine, keep=only is not None)
                    finding.fixed = True
                    finding.what += "; drawn ties added" if only is not None else "; ties set as drawn"
                    counts["ties"] += 1
                    continue
                if only is not None:
                    continue                                    # a checked bar's notes are not replaced
                single = list(voices.values())[0] if len(voices) == 1 else None
                if single is not None and _insert_missing_rests(measure, slot.staff, staves, single,
                                                                 divisions, expected) and \
                        _score_voices(measure, slot.staff, staves, divisions) == mine:
                    finding.fixed = True
                    finding.what += "; rests put back"
                    counts["rests"] += 1
                    continue
                _replace_staff(measure, slot.staff, staves, voices, divisions, expected,
                               (page.bar_lines[stave][k], page.staves[stave].gap))
                finding.fixed = True
                finding.what += "; replaced by the drawing's reading"
                counts["replaced"] += 1
    fixed = sum(f.fixed for f in findings)
    report = [f"drawing check: {len(findings)} difference(s) between drawing and score, {fixed} fixed "
              f"({counts['rests']} by putting back rests, {counts['ties']} by setting ties, {counts['replaced']} "
              f"bar(s) replaced by the drawing's reading, {counts['keys']} key signature(s)); "
              f"{counts['drum bars written']} drum bar(s) written "
              f"from the drawing"]
    report += [f"  {f.describe()}" for f in findings]
    report += [f"  {line}" for line in notes_lines]
    report += ["", "drawn staves matched to parts (stave numbers from the top of the page, from 0):"]
    report += [f"  {line}" for line in layout]
    return findings, report


def _count_diff(drawn: Counter, read: Counter) -> str:
    return ", ".join(f"{k} drawn {drawn[k]} read {read[k]}" for k in KINDS if drawn[k] != read[k])


def _describe_difference(mine: list[tuple], theirs: list[tuple]) -> str:
    def show(voices):
        return " | ".join(" ".join(f"{_name(p)}{l}" for l, p in v) for v in voices)
    return f"drawn {show(mine)}; score {show(theirs)}"


def _name(heads: tuple) -> str:
    if not heads:
        return "r"
    names = "C C# D Eb E F F# G Ab A Bb B".split()
    return "+".join(f"{tie[:1] if tie.startswith('<') else ''}{names[p % 12]}{p // 12 - 1}"
                    f"{'>' if tie.endswith('>') else ''}" for p, tie in heads) + ":"
