#!/usr/bin/env python3
"""Read guitar tablature from the drawing of an engraved PDF into MusicXML.

USAGE (from the repo root):
    python3 tools/omr/tab.py library/<piece> [--tuning E2,A2,D3,G3,B3,E4]

Audiveris does not read tab. Name the shapes once (`vector.py survey`;
fret-0 to fret-9 for the digits, time-N for time-signature figures, and the
rests, flags and dots), then this writes omr/<piece>-merged.mxl and
.musicxml: a guitar stave (treble clef an octave down), each note with its
string and fret. A full stem is a crotchet, halved by each beam or flag at
its foot; a short stem is a minim; no stem, a semibreve; dots follow a stem
or rest. Tab has no key: each run of bars takes the key that needs the
fewest accidentals. omr/tab-report.txt lists every bar that doesn't add up.
"""
from __future__ import annotations

import argparse
import math
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import repair
import score
import vector
import vector_read

STANDARD = "E2,A2,D3,G3,B3,E4"                 # string 6 (lowest) to string 1
STEP_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
LETTERS = "CDEFGAB"
SHARPS_ORDER = "FCGDAEB"
MAJOR = (0, 2, 4, 5, 7, 9, 11)
KEY_CHANGE_COST = 3                            # out-of-key notes a key change must save
MIDI_PROGRAM = 28                              # General MIDI: Electric Guitar (clean)
KEY_NAMES = "Cb Gb Db Ab Eb Bb F C G D A E B F# C#".split()     # fifths -7 to 7


def midi_of(name: str) -> int:
    alter = name.count("#") - name.count("b")
    return 12 * (int(name.lstrip("ABCDEFG#b")) + 1) + STEP_PC[name[0]] + alter


@dataclass
class Fret:
    string: int                                # 1 is the top line
    fret: int
    x0: float
    x1: float
    cy: float
    tie_start: bool = False
    tie_stop: bool = False

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class TabEvent:
    x: float
    length: Fraction | None                    # None: no stem, rest or flag could be read
    frets: list[Fret] = field(default_factory=list)      # empty: a rest
    whole_bar: bool = False


@dataclass
class TabBar:
    number: int
    page: int
    events: list[TabEvent]
    time: tuple[int, int] | None = None        # a time signature drawn in it


# --- reading the drawing ------------------------------------------------------------

def _frets(glyphs: list[vector.Glyph], stave: vector.Stave) -> list[Fret]:
    """Fret numbers: digits on a string, those touching read as one number."""
    numbers: list[Fret] = []
    for g in sorted((g for g in glyphs if g.name.startswith("fret-")), key=lambda g: g.x0):
        string = min(range(len(stave.lines)), key=lambda i: abs(stave.lines[i] - g.cy)) + 1
        last = numbers[-1] if numbers else None
        if last and last.string == string and -0.5 <= g.x0 - last.x1 < stave.gap * 0.15:
            last.fret, last.x1 = last.fret * 10 + int(g.name[5:]), g.x1
        else:
            numbers.append(Fret(string, int(g.name[5:]), g.x0, g.x1, g.cy))
    return numbers


def _dots(after: float, top: float, bottom: float, glyphs: list[vector.Glyph], gap: float,
          stems: list[vector.Stem]) -> int:
    """Dots in a row starting just right of x `after`, the first between
    heights `top` and `bottom`, none past another stem."""
    dots = [g for g in glyphs if g.name == "dot"]
    first = [d for d in dots if 0 < d.x0 - after < gap * 0.9 and top <= d.cy <= bottom
             and not any(after < s.x < d.x0 for s in stems)]
    if not first:
        return 0
    last, count = min(first, key=lambda d: d.x0), 1
    while True:
        more = [d for d in dots if 0 < d.x0 - last.x1 < gap * 0.6 and abs(d.cy - last.cy) < gap * 0.3]
        if not more:
            return count
        last, count = min(more, key=lambda d: d.x0), count + 1


def _dotted(length: Fraction, dots: int) -> Fraction:
    return length * (2 - Fraction(1, 2 ** dots)) if dots else length


def _note_length(x: float, stems: list[vector.Stem], full: float, beams: list[vector_read.Beam],
                 glyphs: list[vector.Glyph], gap: float) -> Fraction:
    near = [s for s in stems if abs(s.x - x) < gap * 0.6]
    if not near:
        return Fraction(4)
    stem = min(near, key=lambda s: abs(s.x - x))
    flags = []
    if stem.y1 - stem.y0 < full * 0.75:
        length = Fraction(2)
    else:
        tip = stem.y1
        count = sum(1 for b in beams if b.crosses(stem.x, tip - gap * 1.3, tip, gap * 0.1))
        flags = [g for g in glyphs if g.name in vector_read.FLAGS and abs(g.x0 - stem.x) < gap * 0.3
                 and g.y0 - gap <= tip <= g.y1 + gap]
        count += max((vector_read.FLAGS[g.name] for g in flags), default=0)
        length = Fraction(1, 2 ** count)
    others = [s for s in stems if s is not stem]
    after = max([stem.x] + [g.x1 for g in flags])                  # a flagged stem's dot follows its flag
    return _dotted(length, _dots(after, stem.y0 - gap * 0.5, stem.y1, glyphs, gap, others))


def _rest(glyph: vector.Glyph, stave: vector.Stave, glyphs: list[vector.Glyph],
          stems: list[vector.Stem]) -> TabEvent:
    gap = stave.gap
    if glyph.name == "rest-whole":
        hangs = any(abs(glyph.y0 - line) < 0.35 for line in stave.lines)
        sits = any(abs(glyph.y1 - line) < 0.35 for line in stave.lines)
        if hangs and not sits:
            return TabEvent(glyph.cx, None, whole_bar=True)
        length = Fraction(2)
    else:
        length = vector_read.RESTS[glyph.name]
    dots = _dots(glyph.x1, glyph.cy - gap, glyph.cy + gap, glyphs, gap, stems)
    return TabEvent(glyph.cx, _dotted(length, dots))


def _time(glyphs: list[vector.Glyph]) -> tuple[int, int] | None:
    figures = sorted((g for g in glyphs if g.name.startswith("time-")), key=lambda g: g.cy)
    if len(figures) < 2:
        return None
    middle = (figures[0].cy + figures[-1].cy) / 2
    top = "".join(g.name[5:] for g in sorted((g for g in figures if g.cy < middle), key=lambda g: g.x0))
    bottom = "".join(g.name[5:] for g in sorted((g for g in figures if g.cy >= middle), key=lambda g: g.x0))
    return (int(top), int(bottom)) if top and bottom else None


def _mark_ties(curves: list[vector_read.Curve], frets: list[Fret], stave: vector.Stave) -> None:
    """A curve from a fret number to the same fret on the same string is a
    tie; one with a number at only one end runs over a system break."""
    gap = stave.gap

    def at(point, side):
        edge = (lambda f: f.x1) if side == "left" else (lambda f: f.x0)
        found = [f for f in frets if abs(point[0] - edge(f)) < gap * 0.8 and abs(point[1] - f.cy) < gap * 0.9]
        return min(found, key=lambda f: abs(point[0] - edge(f)) + abs(point[1] - f.cy), default=None)

    for curve in curves:
        if not (stave.top - gap * 2 <= curve.left[1] <= stave.bottom + gap) or curve.right[0] < stave.left \
                or curve.left[0] > stave.right:
            continue
        start, end = at(curve.left, "left"), at(curve.right, "right")
        if start and end and not curve.steep:
            if start.string == end.string and start.fret == end.fret and start is not end:
                start.tie_start = end.tie_stop = True
        elif start and curve.right[0] > max((f.x1 for f in frets), default=0):
            start.tie_start = True
        elif end and curve.left[0] < min((f.x0 for f in frets), default=0):
            end.tie_stop = True


def read_tab(piece: Path) -> list[TabBar]:
    import pymupdf
    catalogue = vector.load_catalogue(piece)
    bars: list[TabBar] = []
    for page_number, (pdf, index) in enumerate(vector.pdf_pages(vector.piece_pdfs(piece)), start=1):
        with pymupdf.open(pdf) as doc:
            pg = doc[index]
            page = vector.read_page(pg, lines=6)
            beams = vector_read.beams_on_page(pg)
            curves = vector_read.curves_on_page(pg)
        vector.name_glyphs(page, catalogue)
        for s, stave in enumerate(page.staves):
            gap = stave.gap
            mine = [g for g in page.glyphs if g.stave == s]
            stems = [t for t in page.stems if stave.left <= t.x <= stave.right
                     and stave.bottom < t.y0 < stave.bottom + gap * 3]
            full = max((t.y1 - t.y0 for t in stems), default=0.0)
            frets = _frets(mine, stave)
            _mark_ties(curves, frets, stave)
            lines = page.bar_lines[s]
            for k in range(page.bar_count(s)):
                left, right = lines[k], lines[k + 1]
                glyphs = [g for g in mine if g.bar == k]
                in_bar = [f for f in frets if left <= f.cx < right]
                bar_stems = [t for t in stems if left <= t.x <= right]
                events: list[TabEvent] = []
                for f in sorted(in_bar, key=lambda f: f.cx):
                    if events and events[-1].frets and abs(f.cx - events[-1].x) < gap * 0.5:
                        events[-1].frets.append(f)
                    else:
                        events.append(TabEvent(f.cx, None, [f]))
                for event in events:
                    event.length = _note_length(event.x, bar_stems, full, beams, glyphs, gap)
                events += [_rest(g, stave, glyphs, bar_stems) for g in glyphs
                           if g.name in vector_read.RESTS or g.name == "rest-whole"]
                events.sort(key=lambda e: e.x)
                bars.append(TabBar(len(bars) + 1, page_number, events, _time(glyphs)))
    _complete_ties(bars)
    return bars


def _complete_ties(bars: list[TabBar]) -> None:
    """A tie needs both ends: a started tie stops on the same string and fret
    in the next event, or is dropped; likewise a stopped one."""
    events = [e for bar in bars for e in bar.events]
    for before, after in zip(events, events[1:]):
        for f in before.frets:
            match = next((g for g in after.frets if (g.string, g.fret) == (f.string, f.fret)), None)
            if f.tie_start and match is not None:
                match.tie_stop = True
            elif f.tie_start:
                f.tie_start = False
        for g in after.frets:
            if g.tie_stop and not any(f.tie_start and (f.string, f.fret) == (g.string, g.fret)
                                      for f in before.frets):
                g.tie_stop = False
    for f in events[0].frets if events else []:
        f.tie_stop = False
    for f in events[-1].frets if events else []:
        f.tie_start = False


# --- pitch and key --------------------------------------------------------------------

def fifths_by_bar(bars: list[list[int]]) -> list[int]:
    """Per bar, the key (fifths, -7 to 7) of a path through the bars that
    keeps notes in key, changing key only where that saves more than
    KEY_CHANGE_COST accidentals; bars are lists of MIDI pitches."""
    keys = range(-7, 8)

    def out_of(key: int, pitches: list[int]) -> float:
        """Accidentals needed, then fewer sharps or flats, sharps before flats."""
        tonic = (7 * key) % 12
        return sum(1 for p in pitches if (p - tonic) % 12 not in MAJOR) + abs(key) * 0.01 - (key > 0) * 0.001

    cost = {k: out_of(k, bars[0]) for k in keys} if bars else {}
    back: list[dict[int, int]] = []
    for pitches in bars[1:]:
        step, came = {}, {}
        for k in keys:
            prev = min(keys, key=lambda j: cost[j] + (0 if j == k else KEY_CHANGE_COST))
            step[k] = cost[prev] + (0 if prev == k else KEY_CHANGE_COST) + out_of(k, pitches)
            came[k] = prev
        cost, back = step, back + [came]
    if not bars:
        return []
    path = [min(keys, key=lambda k: cost[k])]
    for came in reversed(back):
        path.append(came[path[-1]])
    return path[::-1]


def spell(midi: int, fifths: int) -> tuple[str, int, int]:
    """(step, alter, octave) of a MIDI pitch in a key: as the key spells it
    if it is in the key, else a natural, else sharp in a sharp key and flat
    in a flat one."""
    alters = {letter: (1 if letter in SHARPS_ORDER[:fifths] else 0) if fifths >= 0 else
              (-1 if letter in SHARPS_ORDER[::-1][:-fifths] else 0) for letter in LETTERS}
    pc = midi % 12
    choices = [(letter, alters[letter]) for letter in LETTERS if (STEP_PC[letter] + alters[letter]) % 12 == pc]
    choices = choices or [(letter, 0) for letter in LETTERS if STEP_PC[letter] == pc]
    choices = choices or [(letter, 1 if fifths >= 0 else -1) for letter in LETTERS
                          if (STEP_PC[letter] + (1 if fifths >= 0 else -1)) % 12 == pc]
    letter, alter = choices[0]
    return letter, alter, (midi - alter - STEP_PC[letter]) // 12 - 1


# --- writing MusicXML -----------------------------------------------------------------

def _element(tag: str, text: str | None = None, **attrs) -> ET.Element:
    element = ET.Element(tag, {k.replace("_", "-"): v for k, v in attrs.items()})
    if text is not None:
        element.text = text
    return element


def to_musicxml(bars: list[TabBar], tuning: list[int], name: str,
                given_keys: dict[int, int] | None = None) -> tuple[ET.Element, list[str]]:
    """The score, and report lines: bars that don't add up, keys chosen.
    `given_keys` ({bar: fifths}) sets the keys instead of inferring them."""
    report = []
    pitches = [[tuning[6 - f.string] + f.fret for e in bar.events for f in e.frets] for bar in bars]
    if given_keys:
        keys, current = [], 0
        for bar in bars:
            current = given_keys.get(bar.number, current)
            keys.append(current)
    else:
        keys = fifths_by_bar(pitches)
    lengths = [e.length for bar in bars for e in bar.events if e.length is not None]
    divisions = math.lcm(*(length.denominator for length in lengths)) if lengths else 1
    root = ET.Element("score-partwise", {"version": score.MUSICXML_VERSION})
    work = ET.SubElement(root, "work")
    work.append(_element("work-title", name))
    part_list = ET.SubElement(root, "part-list")
    entry = ET.SubElement(part_list, "score-part", {"id": "P1"})
    entry.append(_element("part-name", "Guitar"))
    entry.append(_element("part-abbreviation", "Gtr."))
    instrument = ET.SubElement(entry, "score-instrument", {"id": "P1-I1"})
    instrument.append(_element("instrument-name", "Electric Guitar"))
    midi = ET.SubElement(entry, "midi-instrument", {"id": "P1-I1"})
    midi.append(_element("midi-channel", "1"))
    midi.append(_element("midi-program", str(MIDI_PROGRAM)))
    part = ET.SubElement(root, "part", {"id": "P1"})
    time, key = (4, 4), None
    for bar, fifths in zip(bars, keys):
        measure = ET.SubElement(part, "measure", {"number": str(bar.number)})
        attributes = ET.Element("attributes")
        if bar.number == 1:
            attributes.append(_element("divisions", str(divisions)))
        if fifths != key:
            k = ET.SubElement(attributes, "key")
            k.append(_element("fifths", str(fifths)))
            report.append(f"bar {bar.number}: key of {fifths} fifths ({KEY_NAMES[fifths + 7]} major)")
            key = fifths
        if bar.time and (bar.time != time or bar.number == 1):
            time = bar.time
        if bar.time or bar.number == 1:
            t = ET.SubElement(attributes, "time")
            t.append(_element("beats", str(time[0])))
            t.append(_element("beat-type", str(time[1])))
        if bar.number == 1:
            clef = ET.SubElement(attributes, "clef")
            clef.append(_element("sign", "G"))
            clef.append(_element("line", "2"))
            clef.append(_element("clef-octave-change", "-1"))
        if len(attributes):
            measure.append(attributes)
        expected = Fraction(4 * time[0], time[1])
        for event in bar.events:
            length = expected if event.whole_bar else event.length
            if length is None:
                continue
            if not event.frets:
                note = ET.SubElement(measure, "note")
                note.append(_element("rest", measure="yes") if event.whole_bar else ET.Element("rest"))
                note.append(_element("duration", "0"))
                note.append(_element("voice", "1"))
                repair._set_length(note, length, Fraction(divisions))
                continue
            for i, f in enumerate(sorted(event.frets, key=lambda f: -f.string)):
                step, alter, octave = spell(tuning[6 - f.string] + f.fret, fifths)
                note = ET.SubElement(measure, "note")
                if i:
                    note.append(ET.Element("chord"))
                pitch = ET.SubElement(note, "pitch")
                pitch.append(_element("step", step))
                if alter:
                    pitch.append(_element("alter", str(alter)))
                pitch.append(_element("octave", str(octave)))
                note.append(_element("duration", "0"))
                note.append(_element("voice", "1"))
                repair._set_length(note, length, Fraction(divisions))
                ties = [kind for kind, on in (("stop", f.tie_stop), ("start", f.tie_start)) if on]
                for kind in ties:
                    repair._place(note, _element("tie", type=kind))
                notations = ET.Element("notations")
                for kind in ties:
                    notations.append(_element("tied", type=kind))
                technical = ET.SubElement(notations, "technical")
                technical.append(_element("string", str(f.string)))
                technical.append(_element("fret", str(f.fret)))
                repair._place(note, notations)
        held = sum((expected if e.whole_bar else e.length) or 0 for e in bar.events)
        if held != expected or any(e.length is None and not e.whole_bar for e in bar.events):
            report.append(f"bar {bar.number} (page {bar.page}): {held} crotchets of {expected}, DOES NOT ADD UP")
    return root, report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("piece", type=Path)
    parser.add_argument("--tuning", default=STANDARD, help="the strings' open pitches, lowest first")
    parser.add_argument("--keys", help="key signatures to use instead of inferring them, as "
                                       "<bar>:<fifths>,... e.g. 1:6,9:-4")
    args = parser.parse_args(argv)
    tuning = [midi_of(n) for n in args.tuning.split(",")]
    if len(tuning) != 6:
        parser.error("--tuning names six strings")
    given = None
    if args.keys:
        try:
            given = {int(b): int(f) for b, f in (pair.split(":") for pair in args.keys.split(","))}
        except ValueError:
            parser.error("--keys is <bar>:<fifths>,...")
        if any(not -7 <= f <= 7 for f in given.values()):
            parser.error("--keys fifths run from -7 to 7")
    bars = read_tab(args.piece)
    if not bars:
        print("no tab staves found", file=sys.stderr)
        return 1
    name = args.piece.name
    root, report = to_musicxml(bars, tuning, name, given)
    out = args.piece / "omr" / f"{name}-merged.mxl"
    out.parent.mkdir(parents=True, exist_ok=True)
    score.save(root, out)
    score.save(root, out.with_suffix(".musicxml"))
    bad = [line for line in report if "DOES NOT ADD UP" in line]
    lines = [f"{out}", f"tab read: {len(bars)} bars, {sum(len(e.frets) for b in bars for e in b.events)} notes, "
             f"tuning {args.tuning}", *report, f"bars that do not add up: {len(bad)} of {len(bars)}"]
    (args.piece / "omr" / "tab-report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
