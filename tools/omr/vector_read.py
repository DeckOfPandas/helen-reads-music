"""Read the notes of one bar of one stave from the drawing of an engraved PDF.

Built on vector.py's staves, stems and named glyphs. For a bar it finds:

  noteheads  with their stave position (step: 0 the bottom line, 1 the
             space above, 8 the top line) and kind (black, half, whole, x,
             slash)
  stems      the one each notehead hangs on (its right edge for an up-stem,
             its left for a down-stem); noteheads on one stem are a chord
  beams      four-sided pieces with vertical ends, split out of the filled
             paths that draw them (an engraver draws several to one path);
             a stem's beam count is how many hold its far end
  flags      flag glyphs at a stem's far end
  rests      by glyph; a whole/half rest rectangle is a whole rest when it
             hangs from a line, a half rest when it sits on one
  dots       augmentation dots just right of a head, one or a row
  marks      accent, marcato, tenuto, staccato and the open circle, beside
             a head on its stem's far side or near side

and gives, per voice, the events left to right with their lengths in
crotchets. With up- and down-stems in one bar there are two voices (up,
down), and rests go to the voice whose half of the stave they sit in; else
one. Ties are curves from a head to a head on the same line or space, or
short ones running off past the bar's end (or in from before its start).
Tuplets and grace notes are not read: a bar holding them comes out with
lengths that do not add up, and the caller leaves it alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

import vector

HEADS = {"notehead-black": "black", "notehead-half": "half", "notehead-whole": "whole",
         "notehead-x": "x", "notehead-slash": "slash"}
RESTS = {"rest-quarter": Fraction(1), "rest-eighth": Fraction(1, 2), "rest-16th": Fraction(1, 4)}
FLAGS = {"flag-8th-up": 1, "flag-8th-down": 1, "flag-16th-up": 2, "flag-16th-down": 2}
MARKS = {"accent": "accent", "marcato": "strong", "tenuto": "tenuto", "dot-small": "staccato", "open": "open"}
ACCIDENTALS = {"flat": -1, "sharp": 1, "natural": 0}
BEAM_THICKEST = 8.0                 # points: half a space even on a stave drawn twice the usual size
FLAT_BULB = 0.7                    # a flat's note is this many spaces above the flat's foot


def accidental_y(sign: vector.Glyph, gap: float) -> float:
    """The height of the line or space an accidental is drawn for: a sharp
    or natural is centred on it; a flat's stem rises above its bulb."""
    return sign.y1 - gap * FLAT_BULB if sign.name == "flat" else sign.cy


def accidentals_of(heads: list[vector.Glyph], signs: list[vector.Glyph], gap: float) -> dict[int, vector.Glyph]:
    """Which accidental each head has, by id(head). An accidental is its
    note's: the nearest head to its right on its line or space, reached
    within 1.6 spaces, or across a chord's stacked columns of accidentals
    with no wider opening than 0.8 spaces. A sign no head claims is a key's."""
    def reaches(sign, head) -> bool:
        edge = sign.x1
        while head.x0 - edge >= gap * 1.6:
            bridge = [s for s in signs if s is not sign and -gap * 0.2 <= s.x0 - edge < gap * 0.8
                      and s.x1 > edge and s.x1 <= head.x0]
            if not bridge:
                return False
            edge = max(s.x1 for s in bridge)
        return True

    owner: dict[int, vector.Glyph] = {}
    for sign in signs:
        y = accidental_y(sign, gap)
        row = sorted((h for h in heads if h.x0 - sign.x1 >= 0 and abs(h.cy - y) < gap * 0.3), key=lambda h: h.x0)
        if not row or not reaches(sign, row[0]):
            continue
        head = row[0]
        if id(head) not in owner or sign.x1 > owner[id(head)].x1:
            owner[id(head)] = sign
    return owner


@dataclass
class Head:
    step: int
    kind: str                       # black, half, whole, x, slash
    x: float
    y: float
    accidental: int | None = None   # drawn just before it
    tie_start: bool = False         # tied on to the next note
    tie_stop: bool = False          # tied from the note before
    left: float | None = None       # the head's left edge, where MusicXML's default-x points


@dataclass
class Curve:
    """A filled path drawn with Bézier curves (a tie or a slur), with the
    points at its two ends."""
    left: tuple[float, float]
    right: tuple[float, float]
    steep: bool = False             # taller than a whole tie: half a tie cut off at a system's edge, or not one

    @property
    def width(self) -> float:
        return self.right[0] - self.left[0]


def _subpaths(items: list) -> list[list]:
    """A path's pieces, split where one doesn't start where the last ended:
    one path can draw several ties."""
    parts: list[list] = []
    for item in items:
        if parts and abs(item[1].x - parts[-1][-1][-1].x) < 0.01 and abs(item[1].y - parts[-1][-1][-1].y) < 0.01:
            parts[-1].append(item)
        else:
            parts.append([item])
    return parts


def curves_on_page(pg) -> list[Curve]:
    """Long, low filled shapes, at least half their pieces curves: ties and
    slurs, each subpath of a path on its own."""
    curves = []
    for d in pg.get_drawings():
        if d.get("fill") is None or any(item[0] not in ("c", "l") for item in d["items"]):
            continue
        for items in _subpaths(d["items"]):
            kinds = [item[0] for item in items]
            if kinds.count("c") < 2 or kinds.count("c") < len(kinds) * 0.5:
                continue
            points = [p for item in items for p in item[1:]]
            left = min(points, key=lambda p: p.x)
            right = max(points, key=lambda p: p.x)
            width, height = right.x - left.x, max(p.y for p in points) - min(p.y for p in points)
            if width < 2.5 or height > width * 0.85:
                continue
            curves.append(Curve((left.x, left.y), (right.x, right.y), steep=height > width * 0.6))
    return curves


@dataclass
class Event:
    x: float
    length: Fraction
    heads: list[Head] = field(default_factory=list)    # empty: a rest
    stem: str | None = None                            # up, down, or None
    marks: set[str] = field(default_factory=set)
    whole_bar: bool = False                            # a whole-bar rest


@dataclass
class Beam:
    x0: float
    x1: float
    y_left: float                   # top edge at x0
    y_right: float                  # top edge at x1
    thickness: float

    def crosses(self, x: float, y0: float, y1: float, slack: float) -> bool:
        """Whether this beam crosses the vertical segment x, y0..y1."""
        if not (self.x0 - slack <= x <= self.x1 + slack):
            return False
        t = 0 if self.x1 == self.x0 else (x - self.x0) / (self.x1 - self.x0)
        top = self.y_left + t * (self.y_right - self.y_left)
        return top <= y1 + slack and top + self.thickness >= y0 - slack


def beams_on_page(pg) -> list[Beam]:
    """Four-sided pieces with two vertical ends, from every dark filled path
    made only of lines (a light one is a background or a mask)."""
    beams = []
    for d in pg.get_drawings():
        if d.get("fill") is None or min(d["fill"]) > 0.5:
            continue
        # A flat beam can be drawn as a rectangle (re) or a quad (qu).
        for item in d["items"]:
            if item[0] == "re":
                r = item[1]
                if r.width > 2 and 0.2 < r.height <= BEAM_THICKEST:
                    beams.append(Beam(r.x0, r.x1, r.y0, r.y0, r.height))
            elif item[0] == "qu":
                q = item[1]
                xs = sorted([q.ul.x, q.ur.x, q.ll.x, q.lr.x])
                if xs[-1] - xs[0] > 2:
                    left = [p for p in (q.ul, q.ur, q.ll, q.lr) if abs(p.x - xs[0]) < 0.05]
                    right = [p for p in (q.ul, q.ur, q.ll, q.lr) if abs(p.x - xs[-1]) < 0.05]
                    if len(left) == 2 and len(right) == 2:
                        beams.append(Beam(xs[0], xs[-1], min(p.y for p in left), min(p.y for p in right),
                                          abs(left[0].y - left[1].y)))
        # A beam drawn as four lines: in runs of lines, which a path can mix
        # with rectangles and curves.
        runs: list[list] = [[]]
        for item in d["items"]:
            if item[0] == "l":
                runs[-1].append(item)
            elif runs[-1]:
                runs.append([])
        quads = [run[start:start + 4] for run in runs for start in range(0, len(run) - 3, 4)]
        for quad in quads:
            points = [quad[0][1]] + [item[2] for item in quad]
            verticals = [(p, q) for p, q in zip(points, points[1:]) if abs(p.x - q.x) < 0.05 and abs(p.y - q.y) > 0.2]
            if len(verticals) != 2:
                continue
            (a, b), (c, e) = sorted(verticals, key=lambda v: v[0].x)
            if c.x - a.x < 2 or abs(a.y - b.y) > BEAM_THICKEST:
                continue                                        # too short, or a background, not a beam
            beams.append(Beam(a.x, c.x, min(a.y, b.y), min(c.y, e.y), abs(a.y - b.y)))
    return beams


def _touches(stem: vector.Stem, head) -> bool:
    return min(abs(stem.x - head.x1), abs(stem.x - head.x0)) < 0.8


def _stem_direction(stem: vector.Stem, heads: list, gap: float) -> str | None:
    """Up if noteheads sit at the stem's foot and none at its top, down the
    other way round; None if heads sit at both ends or neither."""
    at_top = any(_touches(stem, h) and abs(h.cy - stem.y0) < gap * 0.75 for h in heads)
    at_foot = any(_touches(stem, h) and abs(h.cy - stem.y1) < gap * 0.75 for h in heads)
    if at_foot != at_top:
        return "up" if at_foot else "down"
    return None


def _attach_stem(head, stems: list[vector.Stem], gap: float, heads: list | None = None) -> tuple[vector.Stem, str] | None:
    """The stem a head hangs on, and its direction. The direction is the
    stem's own (_stem_direction, from `heads`): the end with no notehead
    is its tip. A head in a chord of a second sits on the stem's far side,
    so either edge may meet it. Where the stem's ends don't tell, a head
    meets an up-stem with its right edge and a down-stem with its left."""
    best = None
    for stem in stems:
        if not (stem.y0 - gap * 0.6 <= head.cy <= stem.y1 + gap * 0.6) or not _touches(stem, head):
            continue
        direction = _stem_direction(stem, heads or [head], gap)
        if direction is None:
            if abs(stem.x - head.x1) < 0.8 and stem.y0 < head.cy - gap * 0.3:
                direction = "up"
            elif abs(stem.x - head.x0) < 0.8 and stem.y1 > head.cy + gap * 0.3:
                direction = "down"
            else:
                continue
        elif (direction == "up" and stem.y0 >= head.cy - gap * 0.3) or \
                (direction == "down" and stem.y1 <= head.cy + gap * 0.3):
            continue
        candidate = (min(abs(stem.x - head.x1), abs(stem.x - head.x0)), stem, direction)
        if best is None or candidate[0] < best[0]:
            best = candidate
    return (best[1], best[2]) if best else None


def read_bar(page: vector.Page, beams: list[Beam], stave_index: int, bar: int,
             expected: Fraction | None = None, prefer_two: bool = False,
             curves: list[Curve] | None = None) -> dict[str, list[Event]] | None:
    """The bar's events per voice ("up" and "down", or "single"), left to
    right; None if the stave has no such bar. With the bar's `expected`
    length, a bar with stems both ways is read as one voice if that adds up,
    else as two; `prefer_two` (drums) always splits. With the page's
    `curves`, ties are marked on the heads."""
    if bar >= page.bar_count(stave_index):
        return None
    stave = page.staves[stave_index]
    gap = stave.gap
    left, right = page.bar_lines[stave_index][bar], page.bar_lines[stave_index][bar + 1]
    glyphs = [g for g in page.glyphs if g.stave == stave_index and g.bar == bar]
    stems = [s for s in page.stems if left <= s.x <= right
             and s.y1 >= stave.top - gap * 7 and s.y0 <= stave.bottom + gap * 7]
    heads = [g for g in glyphs if g.name in HEADS]
    accidentals = [g for g in glyphs if g.name in ACCIDENTALS]

    chords: dict[int, Event] = {}
    loose: list[Event] = []
    owners = accidentals_of(heads, accidentals, gap)
    for glyph in heads:
        head = Head(round(stave.step(glyph.cy)), HEADS[glyph.name], glyph.cx, glyph.cy, left=glyph.x0)
        if id(glyph) in owners:
            head.accidental = ACCIDENTALS[owners[id(glyph)].name]
        attached = _attach_stem(glyph, stems, gap, heads) if head.kind in ("black", "half", "x") else None
        if attached is None:
            length = {"whole": Fraction(4), "slash": Fraction(1)}.get(head.kind)
            if length is None:
                return {}                                       # a stemless black note: can't say
            loose.append(Event(glyph.cx, length, [head]))
            continue
        stem, direction = attached
        event = chords.setdefault(id(stem), Event(glyph.cx, Fraction(0), [], direction))
        event.heads.append(head)
        event.x = min(event.x, glyph.cx)
        if head.kind == "half":
            event.length = Fraction(2)
        elif event.length == 0:
            tip = stem.y0 if direction == "up" else stem.y1
            reach = (tip, tip + gap * 1.8) if direction == "up" else (tip - gap * 1.8, tip)
            count = sum(1 for b in beams if b.crosses(stem.x, reach[0], reach[1], gap * 0.15))
            flagged = [g for g in glyphs if g.name in FLAGS and abs(g.x0 - stem.x) < 1.2
                       and (g.y0 - gap <= tip <= g.y1 + gap)]
            count += max((FLAGS[g.name] for g in flagged), default=0)
            event.length = Fraction(1, 2 ** count)
        event.stem_x = stem.x
    loose.sort(key=lambda e: e.x)
    merged_loose: list[Event] = []
    for event in loose:                                         # stemless heads at one x are a chord
        if merged_loose and abs(event.x - merged_loose[-1].x) < gap * 0.5 and \
                event.length == merged_loose[-1].length:
            merged_loose[-1].heads += event.heads
        else:
            merged_loose.append(event)
    events = list(chords.values()) + merged_loose

    # A mark above a high note can sit nearer the stave above, and be placed
    # there: marks are looked for across the page, and each goes to the
    # nearest head, on whichever stave.
    marks = [g for g in page.glyphs if g.name in MARKS and left <= g.cx <= right]
    others = [g for g in page.glyphs if g.name in HEADS and g.stave != stave_index]
    for event in events:
        for glyph in marks:
            if abs(glyph.cx - event.x) < gap * 1.2 and any(abs(glyph.cy - h.y) < gap * 3.2 for h in event.heads):
                mine = min(abs(glyph.cy - h.y) for h in event.heads)
                if any(abs(o.cx - glyph.cx) < gap * 1.2 and abs(o.cy - glyph.cy) < mine for o in others):
                    continue
                if glyph.name == "dot-small" and any(abs(glyph.cy - h.y) < gap * 0.6 for h in event.heads):
                    continue                                    # beside, not above: not staccato
                event.marks.add(MARKS[glyph.name])
        count = max((_dots_after(h, glyphs, gap) for h in event.heads), default=0)
        if count:
            event.length = event.length * (2 - Fraction(1, 2 ** count))

    rests = []
    for glyph in glyphs:
        if glyph.name == "rest-whole":
            hangs = any(abs(glyph.y0 - line) < 0.35 for line in stave.lines)
            sits = any(abs(glyph.y1 - line) < 0.35 for line in stave.lines)
            length = Fraction(4) if hangs and not sits else Fraction(2)
            rests.append(Event(glyph.cx, length, whole_bar=hangs and not sits))
        elif glyph.name in RESTS:
            rest = Event(glyph.cx, RESTS[glyph.name])
            if any(g.name == "dot" and 0 < g.x0 - glyph.x1 < gap * 1.2 and abs(g.cy - glyph.cy) < gap for g in glyphs):
                rest.length *= Fraction(3, 2)
            rests.append(rest)
    for rest in rests:
        rest.y = next(g.cy for g in glyphs if abs(g.cx - rest.x) < 0.01)

    if curves:
        elsewhere = [Head(round(stave.step(g.cy)), HEADS[g.name], g.cx, g.cy) for g in page.glyphs
                     if g.stave == stave_index and g.bar != bar and g.name in HEADS]
        _find_ties([h for e in events for h in e.heads], elsewhere, curves, gap, left, right)
    voices = split_voices(events, rests, stave, expected, prefer_two)
    if expected is not None and not adds_up(voices, expected) and "single" in voices:
        tremolo = _find_tremolos(voices["single"], beams, gap, expected)
        if tremolo:
            voices = {"single": tremolo}
    return voices


def _dots_after(head: Head, glyphs: list[vector.Glyph], gap: float) -> int:
    """How many augmentation dots follow a head: the first right of it
    within 2.5 spaces (a chord's dots line up, some pushed out) with no
    other notehead between, each further one close after the last, in a row."""
    dots = [g for g in glyphs if g.name == "dot"]
    heads = [g for g in glyphs if g.name in HEADS]
    first = [g for g in dots if 0 < g.x0 - (head.x + gap * 0.6) < gap * 2.5 and abs(g.cy - head.y) < gap * 0.8
             and not any(head.x + gap * 0.6 < h.cx < g.x0 for h in heads)]
    if not first:
        return 0
    last, count = min(first, key=lambda g: g.x0), 1
    while True:
        after = [g for g in dots if 0 < g.x0 - last.x1 < gap and abs(g.cy - last.cy) < gap * 0.3]
        if not after:
            return count
        last, count = min(after, key=lambda g: g.x0), count + 1


def _find_ties(heads: list[Head], elsewhere: list[Head], curves: list[Curve], gap: float,
               left: float, right: float) -> None:
    """Mark the bar's heads tied on or tied from. A tie is a curve with one
    end beside a head and the other beside a head on the same line or space
    (in this bar or another on the stave); or, across a system break, a
    short curve with nothing at its other end. A curve between different
    notes is a slur, not a tie."""
    near = gap * 1.6
    everything = heads + elsewhere

    def nearest(point, among):
        found = [h for h in among if abs(point[0] - h.x) < near and abs(point[1] - h.y) < near]
        return min(found, key=lambda h: abs(point[0] - h.x) + abs(point[1] - h.y), default=None)

    for curve in curves:
        if curve.right[0] < left - gap * 8 or curve.left[0] > right + gap * 8:
            continue
        start, end = nearest(curve.left, everything), nearest(curve.right, everything)
        if start is end:
            continue
        mine_start = start is not None and any(start is h for h in heads)
        mine_end = end is not None and any(end is h for h in heads)
        if not (mine_start or mine_end):
            continue
        if start is not None and end is not None:
            # Tied notes are neighbours: a note between them (near their
            # height, so not another voice's) makes the curve a slur.
            between = any(start.x + gap * 0.5 < h.x < end.x - gap * 0.5 and abs(h.y - start.y) < gap * 3.5
                          for h in everything)
            if start.step == end.step and end.x > start.x and not between and not curve.steep:
                start.tie_start = end.tie_stop = True
            continue
        if curve.width >= gap * 6:
            continue
        found = start if start is not None else end
        if curve.steep and not (start is not None and curve.right[0] > max(h.x for h in everything) or
                                end is not None and curve.left[0] < min(h.x for h in everything)):
            continue                                            # a cut-off tie runs past every note
        between = [h for h in everything if curve.left[0] - near < h.x < curve.right[0] + near
                   and abs(h.x - found.x) > gap * 0.5]                 # not its own chord
        if between:
            continue
        if mine_start:
            start.tie_start = True                              # on, past the system's end
        elif mine_end:
            end.tie_stop = True                                 # from before the system's start


def _find_tremolos(events: list[Event], beams: list[Beam], gap: float, expected: Fraction) -> list[Event] | None:
    """A fingered tremolo: two neighbouring minims (or whole notes), each
    written at the full value but together lasting it, with tremolo strokes
    (shaped like beams, which such notes never have) drawn near them. Halve
    every such pair; return the events if the bar then adds up."""
    changed = False
    out = [Event(e.x, e.length, e.heads, e.stem, set(e.marks), e.whole_bar) for e in events]
    i = 0
    while i + 1 < len(out):
        a, b = out[i], out[i + 1]
        kinds = {h.kind for h in a.heads + b.heads}
        if a.heads and b.heads and kinds <= {"half", "whole"} and a.length == b.length and \
                any(beam.x1 >= a.x - gap * 2 and beam.x0 <= b.x + gap for beam in beams
                    if beam.y_left > min(h.y for h in a.heads + b.heads) - gap * 6
                    and beam.y_left < max(h.y for h in a.heads + b.heads) + gap * 6):
            a.length, b.length = a.length / 2, b.length / 2
            a.marks.add("tremolo-start")
            b.marks.add("tremolo-stop")
            changed = True
            i += 2
        else:
            i += 1
    if changed and span(out) == expected:
        return out
    return None


def split_voices(events: list[Event], rests: list[Event], stave: vector.Stave,
                 expected: Fraction | None, prefer_two: bool = False) -> dict[str, list[Event]]:
    """One voice, unless that does not add up (or `prefer_two`, for drums)
    and the bar has both up- and down-stems: then up and down voices, rests
    to the half of the stave they sit in. In a single pitched line stems
    point either way with the pitch, so stem direction alone is no voice."""
    single = {"single": sorted(events + rests, key=lambda e: e.x)}
    directions = {e.stem for e in events if e.stem}
    if directions != {"up", "down"}:
        return single
    if not prefer_two and (expected is None or adds_up(single, expected)):
        return single
    middle = (stave.top + stave.bottom) / 2
    base = {"up": [e for e in events if e.stem == "up"], "down": [e for e in events if e.stem == "down"]}
    for event in events:
        if event.stem is None:
            base["up" if event.heads[0].y < middle else "down"].append(event)
    preferred = ["up" if rest.y < middle else "down" for rest in rests]
    # Each rest to either voice, nearest the preferred assignment first: a
    # tall crotchet rest can straddle the middle line.
    choices = [preferred]
    if expected is not None and len(rests) <= 10:
        from itertools import product
        flips = sorted(product((False, True), repeat=len(rests)), key=sum)
        choices = [[("down" if p == "up" else "up") if f else p for p, f in zip(preferred, flip)] for flip in flips]
    voices = None
    for choice in choices:
        trial = {"up": list(base["up"]), "down": list(base["down"])}
        for rest, voice in zip(rests, choice):
            trial[voice].append(rest)
        for name in trial:
            trial[name].sort(key=lambda e: e.x)
        if voices is None:
            voices = trial
        if expected is not None and adds_up(trial, expected):
            voices = trial
            break
    if not prefer_two and expected is not None and not adds_up(voices, expected):
        return single
    return voices


def span(events: list[Event]) -> Fraction:
    return sum((e.length for e in events), Fraction(0))


def adds_up(voices: dict[str, list[Event]], expected: Fraction) -> bool:
    if not voices:
        return False
    for events in voices.values():
        if len(events) == 1 and events[0].whole_bar:
            continue
        if span(events) != expected:
            return False
    return True


# --- pitch -----------------------------------------------------------------------

STEP_NAMES = "CDEFGAB"
CLEF_BOTTOM_LINE = {("G", 2): ("E", 4), ("F", 4): ("G", 2), ("C", 3): ("F", 3), ("C", 4): ("D", 3),
                    ("percussion", 3): ("E", 4), ("percussion", 2): ("E", 4)}


def pitch_at(step: int, clef: tuple[str, int], octave_change: int = 0) -> tuple[str, int]:
    """The diatonic note at a stave position under a clef."""
    name, octave = CLEF_BOTTOM_LINE.get(clef, ("E", 4))
    index = STEP_NAMES.index(name) + octave * 7 + step + octave_change * 7
    return STEP_NAMES[index % 7], index // 7


CLEF_GLYPHS = {"clef-treble": ("G", 2), "clef-bass": ("F", 4), "clef-alto": ("C", 3),
               "clef-percussion": ("percussion", 2)}


def drawn_clef(page: vector.Page, stave_index: int) -> tuple[str, int] | None:
    """The clef drawn at the start of a stave."""
    clefs = sorted((g for g in page.glyphs if g.stave == stave_index and g.name in CLEF_GLYPHS), key=lambda g: g.x0)
    return CLEF_GLYPHS[clefs[0].name] if clefs else None


def spell(voices: dict[str, list[Event]], clef: tuple[str, int], fifths: int) -> None:
    """Give every head its pitch (step letter, alteration, octave), from its
    stave position, the clef, the key and the bar's accidentals: a drawn
    accidental holds for later notes on the same line or space until the
    bar line."""
    import repair
    carried: dict[tuple[str, int], int] = {}
    heads = sorted((h for events in voices.values() for e in events for h in e.heads), key=lambda h: h.x)
    for head in heads:
        letter, octave = pitch_at(head.step, clef)
        if head.accidental is not None:
            carried[(letter, octave)] = head.accidental
        head.pitch = (letter, carried.get((letter, octave), repair.key_alter(fifths, letter)), octave)


def midi_of(pitch: tuple[str, int, int]) -> int:
    letter, alter, octave = pitch
    return 12 * (octave + 1) + {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[letter] + alter
