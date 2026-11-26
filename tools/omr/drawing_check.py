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


@dataclass
class DrawnSystem:
    page_number: int
    pdf: Path
    index: int                       # the page within its PDF
    page: vector.Page
    beams: list
    curves: list
    staves: list[int]                # the page's stave indexes in this system, top first
    bars: list[str]                  # the score's bar numbers the system holds
    mapping: dict[int, int | None]   # stave index -> its slot (part and staff), or None


def drawn_systems(root: ET.Element, piece: Path, pdfs: list[Path]) -> tuple[list[DrawnSystem], list[Slot], list[str]]:
    """Every drawn system, with its bars and which part each stave is: the
    systems, the score's slots, and notes on pages with no staves. Empty
    without glyph names (glyphs.json) or SMuFL text."""
    import pymupdf
    catalogue = vector.load_catalogue(piece)
    if not catalogue and not vector.has_smufl(pdfs):
        return [], [], []
    slots = _read_counts(root)
    numbers = [m.get("number") for m in root.find("part").findall("measure")]
    notes_lines = []
    found = []
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
        found += [(page_number, pdf, index, page, beams, curves, system) for system in page.systems(len(slots))]
    systems = []
    first = 0
    for page_number, pdf, index, page, beams, curves, system in found:
        count = page.bar_count(system[0])
        bars = numbers[first:first + count]
        first += count
        if not bars:
            break
        drawn_counts = [_drawn_counts(page, s) for s in system]
        mapping = dict(zip(system, _match_staves(drawn_counts, slots, bars)))
        systems.append(DrawnSystem(page_number, pdf, index, page, beams, curves, system, bars, mapping))
    return systems, slots, notes_lines


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
                written = repair._length_spelling(length * 2)[0]
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
                    kind, _, strokes = tremolo.partition(":")
                    ET.SubElement(ornaments, "tremolo", {"type": kind.split("-")[1]}).text = strokes or "3"
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
# What a note's notations hold that the drawing check does not write itself, carried to the new notes.
CARRIED = {"slur", "glissando", "slide", "fermata", "arpeggiate", "non-arpeggiate", "ornaments", "dynamics",
           "other-notation"}


def _carried(note: ET.Element) -> list[ET.Element]:
    """A note's lyrics, and the notations the drawing check does not write (slurs, glissandi, fermatas...)."""
    out = [copy.deepcopy(lyric) for lyric in note.findall("lyric")]
    for notations in note.findall("notations"):
        out += [copy.deepcopy(c) for c in notations if c.tag in CARRIED]
    return out


def _put(note: ET.Element, element: ET.Element) -> None:
    if element.tag == "lyric":
        repair._place(note, element)
        return
    notations = note.find("notations")
    if notations is None:
        notations = ET.Element("notations")
        repair._place(note, notations)
    notations.append(element)


def _carry_to_new_notes(old: list[tuple[str | None, list[ET.Element]]], heads: list[ET.Element]) -> None:
    """Put the old notes' lyrics, slurs and other notations (`_carried`) on
    the new notes. Where every note on both sides has a default-x, each old
    note's go to the new note nearest it, within LYRIC_REACH: a note gained
    or lost then takes none from its neighbours. Otherwise only when the
    notes line up one to one."""
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
            for element in lyrics:
                if element.tag != "lyric" or element.get("number", "1") not in taken:
                    _put(near, element)
        return
    if len(heads) == len(old):
        for note, (_, lyrics) in zip(heads, old):
            for element in lyrics:
                _put(note, element)


def replace_staff_notes(measure: ET.Element, staff: int, staves: int, mine: list[list[ET.Element]],
                        divisions: Fraction) -> None:
    """One staff's notes replaced by these voice streams (each from the
    bar's start); other staves' notes kept; the old lyrics, slurs and the
    like carried over (`_carry_to_new_notes`)."""
    kept: dict[tuple[int, str], list[ET.Element]] = defaultdict(list)
    old: list[tuple[str | None, list[ET.Element]]] = []
    for note in measure.findall("note"):
        on = int(note.findtext("staff") or 1) if staves > 1 else 1
        if on == staff:
            if note.find("chord") is None and note.find("rest") is None:
                old.append((note.get("default-x"), _carried(note)))
            continue
        kept[(on, note.findtext("voice") or "1")].append(copy.deepcopy(note))
    heads = [n for stream in mine for n in stream if n.find("chord") is None and n.find("rest") is None]
    _carry_to_new_notes(old, heads)
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
    drawn) is padded with invisible rests where its gaps are. Each of its
    events starts either straight after the event before or at a later
    onset of the other voice: whichever x, read off the other voice's
    onsets, is nearest its own."""
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
    def x_at(time: Fraction) -> float:
        before = [o for o in onsets if o[1] <= time]
        after = [o for o in onsets if o[1] > time]
        if not before or not after:
            return (before or after)[-1 if before else 0][0]
        (x0, t0), (x1, t1) = before[-1], after[0]
        return x0 + (x1 - x0) * float((time - t0) / (t1 - t0))

    padded, t = [], Fraction(0)
    for event, drum_event in zip(voices[gap_voice], out[gap_voice]):
        starts = [t] + [o[1] for o in onsets if o[1] > t]
        start = min(starts, key=lambda s: abs(x_at(s) - event.x)) if onsets else t
        if start > t:
            padded.append(repair.DrumEvent([], start - t, hidden=True))
        padded.append(drum_event)
        t = start + drum_event.length
    if t > expected:
        return None
    if t < expected:
        padded.append(repair.DrumEvent([], expected - t, hidden=True))
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


def _leading_attributes(measure: ET.Element) -> list[ET.Element]:
    """The measure's <attributes> before its first note."""
    found = []
    for child in measure:
        if child.tag == "note":
            break
        if child.tag == "attributes":
            found.append(child)
    return found


def _clef_in_force(part: ET.Element, number: str, staff: int) -> ET.Element | None:
    """The clef of a staff at the start of bar `number`, a clef at the
    bar's start included."""
    clef = None
    for measure in part.findall("measure"):
        mine = _leading_attributes(measure) if measure.get("number") == number else measure.findall("attributes")
        for attributes in mine:
            for c in attributes.findall("clef"):
                if int(c.get("number", "1")) == staff:
                    clef = c
        if measure.get("number") == number:
            break
    return clef


def _clef_octave(clef: ET.Element) -> int:
    return int(clef.findtext("clef-octave-change") or 0)


def _fix_clef_octave(part: ET.Element, number: str, staff: int, octave: int) -> None:
    """Give the staff's clef at bar `number` the drawn octave change, at
    that bar: the clef there is changed, or a copy of the one in force is
    set there. The notes it governs, read under the wrong clef, move by
    the difference: each sits where it was drawn."""
    shift = octave - _clef_octave(_clef_in_force(part, number, staff))
    measures = part.findall("measure")
    start = next(i for i, m in enumerate(measures) if m.get("number") == number)
    for i, m in enumerate(measures[start:]):
        stop = False
        for child in m:
            if child.tag == "attributes" and i > 0 and any(
                    int(c.get("number", "1")) == staff for c in child.findall("clef")):
                stop = True
                break
            if child.tag == "note" and int(child.findtext("staff") or 1) == staff and child.find("pitch") is not None:
                child.find("pitch/octave").text = str(int(child.findtext("pitch/octave")) + shift)
        if stop:
            break
    measure = measures[start]
    clef = next((c for a in _leading_attributes(measure) for c in a.findall("clef")
                 if int(c.get("number", "1")) == staff), None)
    if clef is None:
        clef = copy.deepcopy(_clef_in_force(part, number, staff))
        attributes = next(iter(_leading_attributes(measure)), None)
        if attributes is None:
            attributes = ET.Element("attributes")
            position = 0
            while position < len(measure) and measure[position].tag == "print":
                position += 1
            measure.insert(position, attributes)
        repair._insert_in_order(attributes, clef)
    change = clef.find("clef-octave-change")
    if octave == 0 and change is not None:
        clef.remove(change)
    elif octave != 0:
        if change is None:
            change = ET.SubElement(clef, "clef-octave-change")
        change.text = str(octave)


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
    skip = skip or set()
    systems, slots, notes_lines = drawn_systems(root, piece, pdfs)
    if not slots:
        return [], ["drawing check: no glyphs.json for this piece (vector.py survey makes one); skipped"]
    parts = {name: p for (pid, name), p in zip(score.part_names(root), root.findall("part"))}
    findings: list[Finding] = []
    layout = []
    counts = Counter()
    slur_bars: list[str] = []
    known_marks = {vector_read.MARKS[g.name] for s in systems for g in s.page.glyphs
                   if g.name in vector_read.MARKS} & (set(ARTICULATIONS) | set(BOWINGS))
    for drawn_system in systems:
        page_number, page, beams, curves = (drawn_system.page_number, drawn_system.page,
                                            drawn_system.beams, drawn_system.curves)
        bars, mapping = drawn_system.bars, drawn_system.mapping
        drawn_counts = {s: _drawn_counts(page, s) for s in drawn_system.staves}
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
            drawn = vector_read.drawn_clef(page, stave)
            clef = drawn or ("G", 2)
            keys = drawn_keys(page, stave) if not drums else []
            key_now = None
            for k, number in enumerate(bars):
                measure = measures.get(number)
                if measure is None:
                    continue
                # An octave clef's 8 is drawn at every system; a read can miss it on some.
                in_force = _clef_in_force(part, number, slot.staff) if k == 0 and drawn and not drums else None
                if in_force is not None and (in_force.findtext("sign"), int(in_force.findtext("line") or 0)) == drawn[:2]:
                    octave = drawn[2] if len(drawn) == 3 else 0
                    if (only is None or (slot.part, number) in only) and _clef_octave(in_force) != octave:
                        findings.append(Finding(page_number, number, slot.part, slot.staff,
                                                f"clef octave drawn {octave}, score {_clef_octave(in_force)}", fix))
                        if fix:
                            _fix_clef_octave(part, number, slot.staff, octave)
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
                        drawn_bar = copy.deepcopy(measure)
                        _rebuild(drawn_bar, [repair._drum_notes(up, "1", "up", divisions),
                                             repair._drum_notes(down, "2", "down", divisions)], divisions)
                        # The drawing reads no grace notes (flams): a bar holding some, or a
                        # hand-patched bar, is compared, never rewritten.
                        if only is not None or measure.find("note/grace") is not None:
                            if _drum_signature(drawn_bar) != _drum_signature(measure):
                                findings.append(Finding(page_number, number, slot.part, slot.staff,
                                                        "drum bar differs from the drawing's reading"))
                        elif fix and ET.tostring(drawn_bar) != ET.tostring(measure):
                            _rebuild(measure, [repair._drum_notes(up, "1", "up", divisions),
                                               repair._drum_notes(down, "2", "down", divisions)], divisions)
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
                vector_read.spell(voices, clef, fifths,
                                  clef_for=lambda x, s=stave: vector_read.clef_at(page, s, x))
                mine = _drawn_voices(voices, expected)
                theirs = _score_voices(measure, slot.staff, staves, divisions)
                lines = page.staves[stave]
                left_x, right_x = page.bar_lines[stave][k], page.bar_lines[stave][k + 1]
                column = [(g.cx, vector_read.MARKS[g.name]) for g in page.glyphs
                          if g.name in vector_read.MARKS and left_x <= g.cx <= right_x
                          and abs(g.cy - (lines.top + lines.bottom) / 2) < lines.gap * 9]
                reach = lines.gap * 1.2
                if fix and only is None and known_marks and \
                        _set_marks(measure, slot.staff, staves, voices, divisions, known_marks, column, reach):
                    counts["marks"] += 1
                if fix and only is None:
                    taken = _check_slurs(measure, slot.staff, staves, voices, divisions, curves, lines.gap)
                    counts["slurs"] += taken
                    if taken:
                        slur_bars.append(f"page {page_number} bar {number} {slot.part}: {taken}")
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
                if known_marks:
                    _set_marks(measure, slot.staff, staves, voices, divisions, known_marks, column, reach)
                taken = _check_slurs(measure, slot.staff, staves, voices, divisions, curves, lines.gap)
                counts["slurs"] += taken
                if taken:
                    slur_bars.append(f"page {page_number} bar {number} {slot.part}: {taken}")
                finding.fixed = True
                finding.what += "; replaced by the drawing's reading"
                counts["replaced"] += 1
    matched = _match_dynamics(systems, slots, parts, assume) if fix and only is None else None
    fixed = sum(f.fixed for f in findings)
    report = [f"drawing check: {len(findings)} difference(s) between drawing and score, {fixed} fixed "
              f"({counts['rests']} by putting back rests, {counts['ties']} by setting ties, {counts['replaced']} "
              f"bar(s) replaced by the drawing's reading, {counts['keys']} key signature(s)); "
              f"{counts['drum bars written']} drum bar(s) written "
              f"from the drawing; articulations set as drawn in {counts['marks']} bar(s); "
              f"{counts['slurs']} slur(s) no drawn curve joins taken out"]
    report += [f"  {f.describe()}" for f in findings]
    if slur_bars:
        report.append(f"  slurs no drawn curve joins, taken out: {'; '.join(slur_bars)}")
    if matched is not None:
        undrawn, added = matched
        report.append(f"  dynamics in bars that draw none, dropped: {len(undrawn)}"
                      + (f" ({', '.join(undrawn)})" if undrawn else ""))
        report.append(f"  dynamics written as drawn, where the score lacked them or read them wrong: {len(added)}"
                      + (f" ({', '.join(added)})" if added else ""))
    report += [f"  {line}" for line in notes_lines]
    report += ["", "drawn staves matched to parts (stave numbers from the top of the page, from 0):"]
    report += [f"  {line}" for line in layout]
    return findings, report


SLUR_END_REACH = 2.5    # stave spaces from a slur's note to the drawn curve's end, across
SLUR_HEIGHT = 7.0       # and up or down: a slur can clear the stems and beams over its notes


def _check_slurs(measure: ET.Element, staff: int, staves: int, voices: dict[str, list[vector_read.Event]],
                 divisions: Fraction, curves: list, gap: float) -> int:
    """Take out each slur that starts and stops on the staff's notes within
    the bar but that no drawn curve joins (Audiveris reads tremolo strokes and
    crossing curves as slurs), and the second of two slurs between the same
    notes. Each note is found in the drawing by its onset and pitches; a slur
    with an end not found there is kept. How many slurs were taken out."""
    drawn: dict[tuple[Fraction, tuple[int, ...]], vector_read.Event] = {}
    for events in voices.values():
        onset = Fraction(0)
        for event in events:
            if event.heads and all(getattr(h, "pitch", None) for h in event.heads):
                drawn[(onset * divisions, tuple(sorted(vector_read.midi_of(h.pitch) for h in event.heads)))] = event
            onset += event.length
    elements = list(measure)
    event_of: dict[int, vector_read.Event] = {}
    for onset, note in onsets(measure):
        if note.find("rest") is not None or (staves > 1 and (note.findtext("staff") or "1") != str(staff)):
            continue
        chord = [note]
        for following in elements[elements.index(note) + 1:]:
            if following.tag != "note" or following.find("chord") is None:
                break
            chord.append(following)
        if any(m.find("pitch") is None for m in chord):
            continue
        pitches = tuple(sorted(vector_read.midi_of((m.findtext("pitch/step"), int(m.findtext("pitch/alter") or 0),
                                                    int(m.findtext("pitch/octave")))) for m in chord))
        if (onset, pitches) in drawn:
            for member in chord:
                event_of[id(member)] = drawn[(onset, pitches)]

    def joined(a: vector_read.Event, b: vector_read.Event) -> bool:
        def near(point, event):
            return abs(point[0] - event.x) < gap * SLUR_END_REACH and \
                min(abs(point[1] - h.y) for h in event.heads) < gap * SLUR_HEIGHT
        return any(near(c.left, a) and near(c.right, b) for c in curves)

    open_slurs: dict[str, tuple[ET.Element, ET.Element]] = {}
    pairs = []
    for note in measure.findall("note"):
        for slur in note.findall("notations/slur"):
            number = slur.get("number", "1")
            if slur.get("type") == "start":
                open_slurs[number] = (note, slur)
            elif slur.get("type") == "stop" and number in open_slurs:
                start_note, start = open_slurs.pop(number)
                pairs.append((start_note, start, note, slur))
    removed, seen = 0, set()
    for start_note, start, stop_note, stop in pairs:
        a, b = event_of.get(id(start_note)), event_of.get(id(stop_note))
        if a is None or b is None or a is b:
            continue
        if (id(a), id(b)) in seen or not joined(a, b):
            for note, slur in ((start_note, start), (stop_note, stop)):
                notations = note.find("notations")
                notations.remove(slur)
                if not len(notations):
                    note.remove(notations)
            removed += 1
            continue
        seen.add((id(a), id(b)))
    return removed


ARTICULATIONS = {"accent": "accent", "strong": "strong-accent", "tenuto": "tenuto", "staccato": "staccato"}
BOWINGS = {"up-bow": "up-bow", "down-bow": "down-bow"}


def _set_marks(measure: ET.Element, staff: int, staves: int, voices: dict[str, list[vector_read.Event]],
               divisions: Fraction, known: set[str], column: list[tuple[float, str]] | None = None,
               reach: float = 0.0) -> bool:
    """Give each of the staff's notes the articulations and bowings drawn on
    it, and take away those not drawn: each drawn note is found in the score
    by its onset and pitches. Only the kinds of mark in `known` (those the
    drawing names somewhere) are touched. A mark is taken away only if none
    of its kind is drawn in the note's column (`column`: (x, mark) of every
    mark near the stave, within `reach` of the note): a mark far above a
    low note is drawn, though too far to be read as the note's. Whether
    anything changed."""
    drawn: dict[tuple[Fraction, tuple[int, ...]], tuple[set[str], float]] = {}
    for events in voices.values():
        onset = Fraction(0)
        for event in events:
            if event.heads and all(getattr(h, "pitch", None) for h in event.heads):
                key = (onset * divisions, tuple(sorted(vector_read.midi_of(h.pitch) for h in event.heads)))
                drawn[key] = ({m for m in event.marks if m in known}, event.x)
            onset += event.length
    changed = False
    elements = list(measure)
    for onset, note in onsets(measure):
        if note.find("rest") is not None or (staves > 1 and (note.findtext("staff") or "1") != str(staff)):
            continue
        chord = [note]
        for following in elements[elements.index(note) + 1:]:
            if following.tag != "note" or following.find("chord") is None:
                break
            chord.append(following)
        pitches = []
        for member in chord:
            pitch = member.find("pitch")
            if pitch is None:
                break
            pitches.append(vector_read.midi_of((pitch.findtext("step"), int(pitch.findtext("alter") or 0),
                                                int(pitch.findtext("octave")))))
        found = drawn.get((onset, tuple(sorted(pitches))))
        if found is None:
            continue
        wanted, x = found
        nearby = {mark for cx, mark in (column or []) if abs(cx - x) < reach}
        have = set()
        for member in chord:
            for group, table in (("articulations", ARTICULATIONS), ("technical", BOWINGS)):
                for container in member.findall(f"notations/{group}"):
                    for child in list(container):
                        mark = next((m for m, tag in table.items() if tag == child.tag), None)
                        if mark is not None and mark in known:
                            if member is note and (mark in wanted or mark in nearby):
                                have.add(mark)
                            else:
                                container.remove(child)
                                changed = True
                    if not len(container):
                        member.find("notations").remove(container)
            notations = member.find("notations")
            if notations is not None and not len(notations):
                member.remove(notations)
        for mark in sorted(wanted - have):
            notations = note.find("notations")
            if notations is None:
                notations = ET.Element("notations")
                repair._place(note, notations)
            group = "articulations" if mark in ARTICULATIONS else "technical"
            container = notations.find(group)
            if container is None:
                container = ET.SubElement(notations, group)
            ET.SubElement(container, ARTICULATIONS.get(mark) or BOWINGS[mark])
            changed = True
    return changed


def drawn_clef_signs(root: ET.Element, piece: Path, pdfs: list[Path], part_name: str) -> dict[str, str]:
    """Per bar number of `part_name`, the clef drawn in force at its first
    note ("G", "F", "C", "percussion"): for splitting a stave a player reads
    several instruments from, whose clefs Audiveris may not have read."""
    systems, slots, _ = drawn_systems(root, piece, pdfs)
    signs: dict[str, str] = {}
    for system in systems:
        page = system.page
        for stave, slot in system.mapping.items():
            if slot is None or slots[slot].part != part_name:
                continue
            for k, number in enumerate(system.bars):
                heads = [g.cx for g in page.glyphs if g.stave == stave and g.bar == k and g.name in NOTEHEADS]
                lines = page.bar_lines[stave]
                x = min(heads) if heads else (lines[k] + lines[k + 1]) / 2 if k + 1 < len(lines) else lines[-1]
                clef = vector_read.clef_at(page, stave, x)
                if clef:
                    signs[number] = clef[0]
    return signs


def onsets(measure: ET.Element) -> list[tuple[Fraction, ET.Element]]:
    """Each note's (or rest's) onset in divisions, chord members and grace notes left out."""
    out = []
    cursor = Fraction(0)
    for element in measure:
        length = Fraction(element.findtext("duration") or 0)
        if element.tag == "backup":
            cursor -= length
        elif element.tag == "forward":
            cursor += length
        elif element.tag == "note" and element.find("grace") is None and element.find("chord") is None:
            out.append((cursor, element))
            cursor += length
    return out


def insert_at(measure: ET.Element, element: ET.Element, ticks: Fraction) -> None:
    """Before the first note starting at `ticks`, or at the bar's start with an <offset>."""
    anchor = next((note for onset, note in onsets(measure) if onset == ticks), None)
    if anchor is None:
        anchor = next((e for e in measure if e.tag in ("note", "backup", "forward")), None)
        if ticks:
            offset = ET.Element("offset")
            offset.text = str(int(ticks)) if ticks.denominator == 1 else str(float(ticks))
            after = ("footnote", "level", "voice", "staff", "sound", "listening")   # what follows <offset>
            position = next((i for i, c in enumerate(element) if c.tag in after), len(element))
            element.insert(position, offset)
    measure.insert(list(measure).index(anchor) if anchor is not None else len(measure), element)


def drawn_events(system: DrawnSystem, stave: int, bar: int,
                 expected: Fraction) -> list[tuple[float, float, Fraction, bool]]:
    """(left edge, centre, onset in crotchets, starts a sound) for each drawn
    note and rest of a bar whose reading adds up; [] where it does not."""
    voices = vector_read.read_bar(system.page, system.beams, stave, bar, expected, curves=system.curves)
    if not voices or not vector_read.adds_up(voices, expected):
        return []
    out = []
    for events in voices.values():
        onset = Fraction(0)
        for event in events:
            if event.heads:
                left = min(h.left if h.left is not None else h.x for h in event.heads)
                out.append((left, event.x, onset, not all(h.tie_stop for h in event.heads)))
            else:
                out.append((event.x, event.x, onset, False))
            onset += event.length
    return out


def bar_lengths(part: ET.Element, assume: Fraction | None) -> dict[str, tuple[Fraction, Fraction]]:
    """Per bar number: (divisions, length in crotchets), where both are known."""
    out = {}
    for measure, state in zip(part.findall("measure"), merge.attribute_states(part)):
        divisions = repair._divisions(measure, state)
        own = [t for t in (score._time_signature(a) for a in measure.findall("attributes")) if t]
        expected = own[-1] if own else (score._time_signature(repair._wrap(state[("time", "1")]))
                                        if ("time", "1") in state else assume)
        if divisions is not None and expected is not None:
            out[measure.get("number")] = (divisions, expected)
    return out


# SMuFL's dynamics as MusicXML's; single letters (p, m, f, r, s, z, n) drawn side by side spell one.
SMUFL_DYNAMICS = {
    0xE520: "p", 0xE521: "m", 0xE522: "f", 0xE523: "r", 0xE524: "s", 0xE525: "z", 0xE526: "n",
    0xE527: "pppppp", 0xE528: "ppppp", 0xE529: "pppp", 0xE52A: "ppp", 0xE52B: "pp", 0xE52C: "mp",
    0xE52D: "mf", 0xE52E: "pf", 0xE52F: "ff", 0xE530: "fff", 0xE531: "ffff", 0xE532: "fffff",
    0xE533: "ffffff", 0xE534: "fp", 0xE535: "fz", 0xE536: "sf", 0xE537: "sfp", 0xE538: "sfpp",
    0xE539: "sfz", 0xE53A: "sfzp", 0xE53B: "sffz", 0xE53C: "rf", 0xE53D: "rfz",
}
MUSICXML_DYNAMICS = {"p", "pp", "ppp", "pppp", "ppppp", "pppppp", "f", "ff", "fff", "ffff", "fffff",
                     "ffffff", "mp", "mf", "sf", "sfp", "sfpp", "fp", "rf", "rfz", "sfz", "sffz", "fz", "n",
                     "pf", "sfzp"}


def _dynamic_letters(g: vector.Glyph) -> str | None:
    """A dynamic glyph's letters, where its character says them (SMuFL's or a
    music font's); None for a shape named "dynamic" in glyphs.json."""
    if g.key.startswith("smufl:"):
        return SMUFL_DYNAMICS.get(int(g.key[6:], 16))
    if g.key.startswith("font:"):
        _, family, code = g.key.split(":")
        return vector.FONT_DYNAMICS.get((family, int(code, 16)))
    return None


def _drawn_dynamics(system: DrawnSystem) -> dict[tuple[int, int], list[tuple[float, str]]]:
    """Per (stave, bar) of the system: each dynamic drawn as a character
    (SMuFL's, or a music font's in vector.FONT_DYNAMICS), (x, letters),
    neighbouring single letters joined into one ("f" "p" is fp)."""
    found: dict[tuple[int, int], list[tuple[float, float, float, float, str]]] = defaultdict(list)
    for g in sorted(system.page.glyphs, key=lambda g: g.x0):
        if g.name != "dynamic" or g.stave not in system.staves:
            continue
        letters = _dynamic_letters(g)
        if letters is None:
            continue
        group = found[(g.stave, g.bar)]
        # Letters of one dynamic share a line: their boxes overlap in height
        # (an f's rises above a p's), with no more than a space between, and
        # together they spell a dynamic, or its start ("f" "mp" are two).
        if group and min(group[-1][3], g.y1) - max(group[-1][2], g.y0) > 0.5 * min(group[-1][3] - group[-1][2],
                                                                                 g.y1 - g.y0) \
                and g.x0 - group[-1][1] < system.page.staves[g.stave].gap \
                and any(known.startswith(group[-1][4] + letters) for known in MUSICXML_DYNAMICS):
            x0, _, y0, y1, text = group[-1]
            group[-1] = (x0, g.x1, min(y0, g.y0), max(y1, g.y1), text + letters)
        else:
            group.append((g.x0, g.x1, g.y0, g.y1, letters))
    return {key: [(x0, text) for x0, _, _, _, text in group] for key, group in found.items()}


def _match_dynamics(systems: list[DrawnSystem], slots: list[Slot], parts: dict[str, ET.Element],
                    assume: Fraction | None) -> tuple[list[str], list[str]] | None:
    """Take out each dynamic the score has in a bar of a system that draws
    none there (Audiveris reads dots and lettering, a chord box or a fret
    number, as p, f and sfz); write each dynamic drawn as a character at the
    drawn note nearest it, in place of the bar's read ones on that stave
    where they differ (kept if the bar also draws one with no known
    letters). Only where the
    drawing names its dynamics (SMuFL's, or "dynamic" in glyphs.json);
    otherwise None, nothing done. Returns (dropped, written)."""
    if not any(g.name == "dynamic" for s in systems for g in s.page.glyphs):
        return None
    dropped, written = [], []
    lengths = {name: bar_lengths(part, assume) for name, part in parts.items()}
    for system in systems:
        drawn_bars = {g.bar for g in system.page.glyphs if g.name == "dynamic" and g.stave in system.staves}
        names = sorted({slots[j].part for j in system.mapping.values() if j is not None})
        for name in names:
            measures = {m.get("number"): m for m in parts[name].findall("measure")}
            for k, number in enumerate(system.bars):
                if k in drawn_bars or number not in measures:
                    continue
                for direction in measures[number].findall("direction"):
                    if direction.find("direction-type/dynamics") is not None:
                        measures[number].remove(direction)
                        dropped.append(f"{name} {number}")
        unread = {(g.stave, g.bar) for g in system.page.glyphs
                  if g.name == "dynamic" and g.stave in system.staves and _dynamic_letters(g) is None}
        for (stave, k), marks in _drawn_dynamics(system).items():
            slot = system.mapping.get(stave)
            if slot is None or k is None or k >= len(system.bars):
                continue
            name, number = slots[slot].part, system.bars[k]
            measure = next((m for m in parts[name].findall("measure") if m.get("number") == number), None)
            if measure is None or number not in lengths[name]:
                continue
            # The bar's dynamics on this stave, as read: kept if they are the drawn
            # ones, else replaced by them (Audiveris reads sffz as ff, mp as p).
            staff = str(slots[slot].staff)
            existing = [d for d in measure.findall("direction") if d.find("direction-type/dynamics") is not None
                        and (d.findtext("staff") or "1") == staff]
            have = sorted(c.text if c.tag == "other-dynamics" else c.tag
                          for d in existing for c in d.find("direction-type/dynamics"))
            if have == sorted(letters for _, letters in marks) or (existing and (stave, k) in unread):
                continue                        # as drawn, or the drawing can't say all it holds
            for direction in existing:
                measure.remove(direction)
            was = f" (read {'+'.join(have)})" if existing else ""
            divisions, expected = lengths[name][number]
            events = drawn_events(system, stave, k, expected)
            for x, letters in marks:
                onset = min(events, key=lambda e: abs(e[0] - x))[2] if events else Fraction(0)
                direction = ET.Element("direction", {"placement": "below"})
                dynamics = ET.SubElement(ET.SubElement(direction, "direction-type"), "dynamics")
                if letters in MUSICXML_DYNAMICS:
                    ET.SubElement(dynamics, letters)
                else:
                    ET.SubElement(dynamics, "other-dynamics").text = letters
                if slots[slot].staff > 1:
                    ET.SubElement(direction, "staff").text = str(slots[slot].staff)
                insert_at(measure, direction, onset * divisions)
                written.append(f"{name} {number} {letters}{was}")
    return dropped, written


def _drum_signature(measure: ET.Element) -> list[tuple]:
    """A drum bar's notes and rests as (voice, heads, length), in time order
    per voice, chords sorted; grace notes left out, and a run of rests one
    rest (a gap may be written as one rest or several)."""
    voices: dict[str, list[list]] = defaultdict(list)
    for note in measure.findall("note"):
        if note.find("grace") is not None:
            continue
        voice = note.findtext("voice") or "1"
        length = int(note.findtext("duration") or 0)
        head = ("rest",) if note.find("rest") is not None else (
            note.findtext("unpitched/display-step"), note.findtext("unpitched/display-octave"),
            note.findtext("notehead") or "normal")
        if note.find("chord") is not None and voices[voice]:
            voices[voice][-1][0].append(head)
        elif head == ("rest",) and voices[voice] and voices[voice][-1][0] == [("rest",)]:
            voices[voice][-1][1] += length
        else:
            voices[voice].append([[head], length])
    return [(voice, tuple(sorted(heads)), length) for voice in sorted(voices) for heads, length in voices[voice]]


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
