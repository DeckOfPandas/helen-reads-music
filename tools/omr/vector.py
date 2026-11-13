#!/usr/bin/env python3
"""Read the drawing of an engraved PDF: staves, bar lines, stems and named symbols.

USAGE (from the repo root):
    python3 tools/omr/vector.py survey library/<piece> [--min N]  # cluster the shapes; write a catalogue draft
    python3 tools/omr/vector.py show library/<piece> <page> # what was found on a page
    python3 tools/omr/vector.py picture library/<piece> <page> <stave> <first bar> <last bar>
        # a PNG of those bars of one stave, to check by eye: page from 1;
        # stave from 0, top down; bars counted on the page from 0.
        # merge-report.txt lists which stave is which part on each page,
        # and each page's bar numbers.

A PDF exported from notation software is vector drawing, not pixels: every
stave line, bar line and symbol is an exact shape at an exact place. The
same symbol (a crotchet rest, a black notehead) is drawn with the same path
every time, only moved, so its path is a fingerprint. This module finds:

  staves     five evenly spaced long horizontal lines
  bar lines  vertical strokes, thicker than stems, crossing a stave
  stems      thin vertical strokes
  glyphs     filled shapes, each with its fingerprint, stave and bar

and names glyphs from library/<piece>/glyphs.json, a catalogue mapping
fingerprint to symbol. `survey` writes a draft of it, naming what it can
from each shape's size against the stave's line spacing, and
library/<piece>/omr/views/glyphs.png showing one of each for checking. A
scanned PDF has no such drawing; then there are no staves and nothing here
applies.

Symbol names used: notehead-black, notehead-half, notehead-whole,
notehead-x, notehead-slash, rest-whole (or half: one rectangle, told apart
by where it sits), rest-quarter, rest-eighth, rest-16th, flag-8th-up,
flag-8th-down, flag-16th-up, flag-16th-down, dot (augmentation), dot-small
(staccato), accent, marcato, tenuto, open (the small circle of an open
hi-hat), flat, sharp, natural, clef-treble, clef-bass, clef-alto,
clef-percussion, other (anything else: text, dynamics, slurs).
"""
from __future__ import annotations

import io
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

LINE_WIDTH_STEM_MAX = 0.42          # the least stem width allowed for: stems are thinner than bar lines
_STEM_WIDTHS: dict[tuple[str, int], float] = {}     # (pdf path, mtime) -> stem_width


@dataclass
class Stave:
    lines: list[float]               # y of the five lines (six for tab), top first
    left: float
    right: float

    @property
    def gap(self) -> float:
        return (self.lines[-1] - self.lines[0]) / (len(self.lines) - 1)

    @property
    def top(self) -> float:
        return self.lines[0]

    @property
    def bottom(self) -> float:
        return self.lines[-1]

    def step(self, y: float) -> float:
        """Stave position of a y: 0 the bottom line, 1 the space above, 8 the
        top line; negative below, above 8 above."""
        return (self.bottom - y) / (self.gap / 2)


@dataclass
class Glyph:
    key: str                         # fingerprint
    x0: float
    y0: float
    x1: float
    y1: float
    name: str = "other"
    stave: int | None = None
    bar: int | None = None           # 0-based within the stave's bar lines

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class Stem:
    x: float
    y0: float
    y1: float


@dataclass
class Page:
    staves: list[Stave]
    bar_lines: list[list[float]]     # per stave, the x of each bar line, merged, left to right
    glyphs: list[Glyph]
    stems: list[Stem]
    ledgers: list[tuple[float, float, float]] = field(default_factory=list)   # (x0, x1, y)
    verticals: list[tuple[float, float, float]] = field(default_factory=list)  # (x, y0, y1), thicker than stems

    def bar_count(self, stave: int) -> int:
        return max(len(self.bar_lines[stave]) - 1, 0)

    def systems(self, staves_in_score: int) -> list[list[int]]:
        """The page's staves, grouped into systems, top to bottom. A page with
        no more staves than the score has is one system (a full score hides
        empty staves). Otherwise adjacent staves are one system where a bar
        line runs from one into the other, as a grand stave's do."""
        indexes = list(range(len(self.staves)))
        if len(self.staves) <= staves_in_score:
            return [indexes] if indexes else []
        groups = [[0]]
        for upper, lower in zip(self.staves, self.staves[1:]):
            joined = any(y0 <= upper.bottom + 0.5 and y1 >= lower.top - 0.5 and
                         max(upper.left, lower.left) - 1 <= x <= min(upper.right, lower.right) + 1
                         for x, y0, y1 in self.verticals)
            if joined:
                groups[-1].append(groups[-1][-1] + 1)
            else:
                groups.append([groups[-1][-1] + 1])
        return groups


def fingerprint(drawing: dict) -> str:
    """The path's item kinds and points relative to its top-left corner,
    rounded to 0.1 pt: the same glyph drawn anywhere gives the same key."""
    r = drawing["rect"]
    parts = []
    for item in drawing["items"]:
        coords = []
        for p in item[1:]:
            if hasattr(p, "x"):
                coords.append(f"{p.x - r.x0:.1f},{p.y - r.y0:.1f}")
            elif hasattr(p, "x0"):
                coords.append(f"{p.x0 - r.x0:.1f},{p.y0 - r.y0:.1f},{p.x1 - r.x0:.1f},{p.y1 - r.y0:.1f}")
        parts.append(item[0] + ":" + ";".join(coords))
    return "|".join(parts)


def _staves(horizontals: list[tuple[float, float, float]], count: int = 5) -> list[Stave]:
    """Groups of `count` long horizontals (five; six for guitar tab), evenly
    spaced, sharing their x extent."""
    rows = sorted(horizontals, key=lambda h: (round(h[0]), h[2]))
    by_extent: dict[tuple[int, int], list[float]] = defaultdict(list)
    for x0, x1, y in rows:
        by_extent[(round(x0), round(x1))].append(y)
    staves = []
    for (x0, x1), ys in by_extent.items():
        ys = sorted(set(round(y, 2) for y in ys))
        i = 0
        while i + count - 1 < len(ys):
            group = ys[i:i + count]
            gaps = [b - a for a, b in zip(group, group[1:])]
            # One more line at the same spacing, either side, makes another kind
            # of stave (five of a tab's six lines are not a stave).
            beyond = i + count < len(ys) and abs(ys[i + count] - group[-1] - gaps[0]) < 0.15
            before = i > 0 and abs(group[0] - ys[i - 1] - gaps[0]) < 0.15
            if max(gaps) - min(gaps) < 0.15 and 1.5 < gaps[0] < 12 and not beyond and not before:
                staves.append(Stave(group, float(x0), float(x1)))
                i += count
            else:
                i += 1
    return sorted(staves, key=lambda s: s.top)


# --- music drawn as text --------------------------------------------------------
# MuseScore 4 and other software write symbols as characters of a SMuFL font
# (Leland, Bravura): the codepoint names the symbol. SMuFL fixes the em at
# four stave spaces and each glyph's place against its origin; the boxes
# here are the glyphs' ink in stave spaces from the origin, y up (Bravura's;
# other SMuFL fonts differ by less than the reader's slack).
SMUFL: dict[int, tuple[str, tuple[float, float, float, float]]] = {
    0xE0A2: ("notehead-whole", (0, -0.5, 1.69, 0.5)),
    0xE0A3: ("notehead-half", (0, -0.5, 1.18, 0.5)),
    0xE0A4: ("notehead-black", (0, -0.5, 1.18, 0.5)),
    0xE0A9: ("notehead-x", (0, -0.5, 1.16, 0.5)),
    0xE101: ("notehead-slash", (0, -1.0, 2.1, 1.0)),
    0xE1E7: ("dot", (0, -0.2, 0.4, 0.2)),
    0xE4E3: ("rest-whole", (0, -0.5, 1.13, 0)),          # hangs from the line at its origin
    0xE4E4: ("rest-whole", (0, 0, 1.13, 0.5)),           # a half rest: sits on it
    0xE4E5: ("rest-quarter", (0, -1.5, 1.08, 1.5)),
    0xE4E6: ("rest-eighth", (0, -1.0, 1.0, 0.7)),
    0xE4E7: ("rest-16th", (0, -2.0, 1.28, 0.7)),
    0xE240: ("flag-8th-up", (0, -3.24, 1.06, 0)),
    0xE241: ("flag-8th-down", (0, 0, 1.12, 3.24)),
    0xE242: ("flag-16th-up", (0, -3.24, 1.16, 0)),
    0xE243: ("flag-16th-down", (0, 0, 1.16, 3.24)),
    0xE260: ("flat", (0, -0.7, 0.9, 1.75)),
    0xE261: ("natural", (0, -1.34, 0.67, 1.34)),
    0xE262: ("sharp", (0, -1.4, 1.0, 1.4)),
    0xE050: ("clef-treble", (0, -2.6, 2.68, 4.4)),
    0xE062: ("clef-bass", (0, -2.5, 2.7, 1.0)),
    0xE05C: ("clef-alto", (0, -2.0, 2.8, 2.0)),
    0xE069: ("clef-percussion", (0, -1.0, 1.0, 1.0)),
    0xE4A0: ("accent", (0, 0, 1.35, 0.7)),
    0xE4A1: ("accent", (0, -0.7, 1.35, 0)),
    0xE4A2: ("dot-small", (0, 0, 0.4, 0.4)),
    0xE4A3: ("dot-small", (0, -0.4, 0.4, 0)),
    0xE4A4: ("tenuto", (0, 0, 1.35, 0.1)),
    0xE4A5: ("tenuto", (0, -0.1, 1.35, 0)),
    0xE4AC: ("marcato", (0, 0, 1.0, 1.1)),
    0xE4AD: ("marcato", (0, -1.1, 1.0, 0)),
}


def smufl_glyphs(page) -> list[Glyph]:
    """Every character of a private-use codepoint (SMuFL's range) on the
    page, as a glyph keyed "smufl:<hex>" and named from SMUFL ("other" for
    the rest), its box from SMUFL or else the text's own."""
    glyphs = []
    for span in page.get_texttrace():
        space = span["size"] / 4
        for code, _, origin, bbox in span["chars"]:
            if not 0xE000 <= code <= 0xF8FF:
                continue
            name, box = SMUFL.get(code, ("other", None))
            if box is None:
                x0, y0, x1, y1 = bbox
            else:
                x0, y0 = origin[0] + box[0] * space, origin[1] - box[3] * space
                x1, y1 = origin[0] + box[2] * space, origin[1] - box[1] * space
            glyphs.append(Glyph(f"smufl:{code:04X}", x0, y0, x1, y1, name))
    return glyphs


def has_smufl(pdfs: list[Path]) -> bool:
    """Whether any page draws music as SMuFL characters, which name themselves."""
    import pymupdf
    for pdf in pdfs:
        with pymupdf.open(pdf) as doc:
            if any(smufl_glyphs(page) for page in doc):
                return True
    return False


def _join_segments(lines: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """Horizontal lines at one height that meet end to end, as one line:
    some software draws a stave bar by bar."""
    joined: list[tuple[float, float, float]] = []
    for x0, x1, y in sorted(lines, key=lambda h: (round(h[2], 2), h[0])):
        if joined and abs(joined[-1][2] - y) < 0.05 and -0.5 < x0 - joined[-1][1] < 0.5:
            joined[-1] = (joined[-1][0], max(x1, joined[-1][1]), joined[-1][2])
        else:
            joined.append((x0, x1, y))
    return joined


def _vertical_ends(page, touching: Counter, total: Counter) -> None:
    """Per vertical stroke width, how many strokes there are, and how many
    end against the side of a filled shape or a music character (a
    notehead or a beam)."""
    import pymupdf
    drawings = page.get_drawings()
    sides: dict[int, list] = defaultdict(list)
    shapes = [d["rect"] for d in drawings if d.get("fill") is not None]
    shapes += [pymupdf.Rect(g.x0, g.y0, g.x1, g.y1) for g in smufl_glyphs(page)]
    for r in shapes:
        sides[int(r.x0)].append((r.x0, r))
        sides[int(r.x1)].append((r.x1, r))
    for d in drawings:
        if d.get("fill") is not None:
            continue
        width = round(d.get("width") or 0, 3)
        for item in d["items"]:
            p, q = item[1], item[-1]
            if item[0] != "l" or abs(p.x - q.x) >= 0.05 or abs(p.y - q.y) <= 3:
                continue
            total[width] += 1
            near = [r for k in (int(p.x) - 1, int(p.x), int(p.x) + 1) for x, r in sides[k] if abs(x - p.x) < 0.6]
            if any(r.y0 - 1 <= y <= r.y1 + 1 for r in near for y in (p.y, q.y)):
                touching[width] += 1


def stem_width(doc) -> float:
    """The widest stroke that is a stem: the widest vertical stroke width
    of which most strokes in the document end against a notehead or beam
    (a bar line ends at a stave's edge), never less than
    LINE_WIDTH_STEM_MAX. Software draws stems from 0.35 to 0.6 pt."""
    path = Path(doc.name) if doc.name else None
    key = (str(path), path.stat().st_mtime_ns) if path and path.is_file() else None
    if key is not None and key in _STEM_WIDTHS:
        return _STEM_WIDTHS[key]
    touching: Counter = Counter()
    total: Counter = Counter()
    for page in doc:
        _vertical_ends(page, touching, total)
    stems = [w for w in total if touching[w] * 2 > total[w]]
    width = max([LINE_WIDTH_STEM_MAX, *(w + 0.001 for w in stems)])
    if key is not None:
        _STEM_WIDTHS[key] = width
    return width


def read_page(page) -> Page:
    """Everything this module finds on one pymupdf page."""
    drawings = page.get_drawings()
    stem_max = stem_width(page.parent) if page.parent is not None else LINE_WIDTH_STEM_MAX
    horizontals, verticals, stems, ledgers = [], [], [], []
    filled = []
    for d in drawings:
        if d.get("fill") is not None:
            filled.append(d)
            continue
        width = d.get("width") or 0
        for item in d["items"]:
            if item[0] != "l":
                continue
            p, q = item[1], item[2]
            if abs(p.y - q.y) < 0.05:
                if abs(p.x - q.x) > 3:
                    horizontals.append((min(p.x, q.x), max(p.x, q.x), p.y))
            elif abs(p.x - q.x) < 0.05 and abs(p.y - q.y) > 3:
                y0, y1 = min(p.y, q.y), max(p.y, q.y)
                if width <= stem_max:
                    stems.append(Stem(p.x, y0, y1))
                else:
                    verticals.append((p.x, y0, y1))
    horizontals = _join_segments(horizontals)
    ledgers = [h for h in horizontals if h[1] - h[0] <= 40]
    staves = _staves([h for h in horizontals if h[1] - h[0] > 40])
    bar_lines = []
    for stave in staves:
        xs = sorted(x for x, y0, y1 in verticals
                    if y0 <= stave.top + 0.5 and y1 >= stave.bottom - 0.5 and stave.left - 1 <= x <= stave.right + 1)
        merged: list[float] = []
        for x in xs:
            if not merged or x - merged[-1] > stave.gap * 1.4:      # a double bar line counts once
                merged.append(x)
            else:
                merged[-1] = x                                    # keep the right-hand line
        if merged and merged[0] - stave.left > stave.gap * 2:
            merged.insert(0, stave.left)                          # the system's opening line, if not drawn here
        bar_lines.append(merged)
    glyphs = smufl_glyphs(page)
    for d in filled:
        r = d["rect"]
        glyphs.append(Glyph(fingerprint(d), r.x0, r.y0, r.x1, r.y1))
    result = Page(staves, bar_lines, glyphs, stems, ledgers, verticals)
    for glyph in glyphs:
        _place_glyph(result, glyph)
    return result


def _place_glyph(page: Page, glyph: Glyph) -> None:
    """The stave whose reach (six spaces beyond its outer lines) holds the
    glyph's centre, nearest first; then the bar it falls in."""
    best = None
    for index, stave in enumerate(page.staves):
        reach = stave.gap * 6
        if stave.top - reach <= glyph.cy <= stave.bottom + reach and stave.left - 2 <= glyph.cx <= stave.right + 2:
            distance = 0 if stave.top <= glyph.cy <= stave.bottom else min(
                abs(glyph.cy - stave.top), abs(glyph.cy - stave.bottom))
            if best is None or distance < best[0]:
                best = (distance, index)
    if best is None:
        return
    glyph.stave = best[1]
    lines = page.bar_lines[glyph.stave]
    inside = [i for i in range(len(lines) - 1) if lines[i] <= glyph.cx < lines[i + 1]]
    glyph.bar = inside[0] if inside else None


# --- the catalogue ---------------------------------------------------------------

def catalogue_path(piece: Path) -> Path:
    return piece / "glyphs.json"


def load_catalogue(piece: Path) -> dict[str, str]:
    path = catalogue_path(piece)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {key: entry["name"] for key, entry in data.get("glyphs", {}).items()}


def name_glyphs(page: Page, catalogue: dict[str, str]) -> None:
    """Names from the catalogue; a SMuFL character not in it keeps its own."""
    for glyph in page.glyphs:
        own = SMUFL.get(int(glyph.key[6:], 16), ("other",))[0] if glyph.key.startswith("smufl:") else "other"
        glyph.name = catalogue.get(glyph.key, own)
    _place_by_ledgers(page)


def _ledger_ladder(stave: Stave, y: float, ledgers: list[float]) -> bool:
    """Whether a note at height y is joined to this stave by its ledger lines:
    one at every stave space from the stave's edge out to the note (none if
    the note is within a space of the stave)."""
    gap, slack = stave.gap, stave.gap * 0.3
    if y < stave.top:
        distance, edge, sign = stave.top - y, stave.top, -1
    elif y > stave.bottom:
        distance, edge, sign = y - stave.bottom, stave.bottom, 1
    else:
        return True
    needed = int(distance / gap + 0.25)
    if needed == 0:
        return distance < gap
    if needed > 8:
        return False
    return all(any(abs(l - (edge + sign * k * gap)) < slack for l in ledgers) for k in range(1, needed + 1))


def _place_by_ledgers(page: Page) -> None:
    """A notehead outside the staves belongs to the stave its ledger lines
    lead to, not the nearest one: a cello's high notes, three ledger lines
    above its stave, can sit nearer the viola's. Ledgers below the head mean
    it is above a stave (the first stave below the lowest of them); ledgers
    above mean it is below one. An augmentation dot follows the notehead it
    belongs to."""
    heads = [g for g in page.glyphs if g.name.startswith("notehead")]
    for head in heads:
        if head.stave is None:
            continue
        stave = page.staves[head.stave]
        if stave.top - stave.gap * 0.75 <= head.cy <= stave.bottom + stave.gap * 0.75:
            continue
        ledgers = [y for x0, x1, y in page.ledgers if x0 - 1 <= head.cx <= x1 + 1]
        owners = [i for i, s in enumerate(page.staves) if _ledger_ladder(s, head.cy, ledgers)]
        target = None
        if len(owners) == 1:
            target = owners[0]
        elif len(owners) > 1:
            target = min(owners, key=lambda i: min(abs(head.cy - page.staves[i].top),
                                                   abs(head.cy - page.staves[i].bottom)))
        if target is not None and target != head.stave:
            head.stave = target
            lines = page.bar_lines[target]
            inside = [i for i in range(len(lines) - 1) if lines[i] <= head.cx < lines[i + 1]]
            head.bar = inside[0] if inside else None
    for dot in (g for g in page.glyphs if g.name == "dot"):
        gap = page.staves[dot.stave].gap if dot.stave is not None else 3
        owners = [h for h in heads if 0 < dot.x0 - h.x1 < gap * 1.6 and abs(dot.cy - h.cy) < gap * 0.8]
        if owners:
            owner = min(owners, key=lambda h: dot.x0 - h.x1)
            dot.stave, dot.bar = owner.stave, owner.bar


def guess_name(width: float, height: float, kinds: str, gap: float) -> str:
    """A first guess from a shape's size in stave spaces and its path kinds;
    the survey's draft, to be confirmed by eye."""
    w, h = width / gap, height / gap
    curves, lines = kinds.count("c"), kinds.count("l")
    if 1.1 <= w <= 1.45 and 0.85 <= h <= 1.15 and lines == 0:
        return "notehead-black"
    if 0.95 <= w <= 1.15 and 0.9 <= h <= 1.1 and curves == 0 and lines >= 8:
        return "notehead-x"
    if 1.6 <= w <= 1.9 and 0.9 <= h <= 1.1:
        return "notehead-whole"
    if 1.15 <= w <= 1.35 and 0.4 <= h <= 0.55 and kinds == "llll":
        return "rest-whole"
    if 0.95 <= w <= 1.15 and 3.1 <= h <= 3.5:
        return "rest-quarter"
    if 1.05 <= w <= 1.2 and 1.7 <= h <= 1.9:
        return "rest-eighth"
    if 0.2 <= w <= 0.4 and 0.2 <= h <= 0.4 and lines == 0:
        return "dot"
    if 0.7 <= w <= 0.85 and 2.3 <= h <= 2.6:
        return "flat"
    if 0.7 <= w <= 0.85 and 0.7 <= h <= 0.85 and lines == 0:
        return "open"
    return "other"


def survey(piece: Path, pdfs: list[Path]) -> dict:
    """Cluster every filled shape in the piece; draft names; render exemplars."""
    import pymupdf
    clusters: dict[str, dict] = {}
    for pdf in pdfs:
        with pymupdf.open(pdf) as doc:
            for page_number, pg in enumerate(doc):
                page = read_page(pg)
                if not page.staves:
                    continue
                gap = sorted(s.gap for s in page.staves)[len(page.staves) // 2]
                for glyph in page.glyphs:
                    entry = clusters.setdefault(glyph.key, {
                        "count": 0, "width": round(glyph.width, 2), "height": round(glyph.height, 2),
                        "kinds": "".join(part.split(":")[0] for part in glyph.key.split("|")),
                        "example": [pdf.name, page_number, glyph.x0, glyph.y0, glyph.x1, glyph.y1],
                        "gap": round(gap, 3)})
                    entry["count"] += 1
    return clusters


def write_draft(piece: Path, clusters: dict, minimum: int = 3) -> Path:
    existing = load_catalogue(piece)
    glyphs = {}
    for key, entry in sorted(clusters.items(), key=lambda kv: -kv[1]["count"]):
        if entry["count"] < minimum and key not in existing:
            continue
        if key.startswith("smufl:"):
            name = existing.get(key) or SMUFL.get(int(key[6:], 16), ("other",))[0]
        else:
            name = existing.get(key) or guess_name(entry["width"], entry["height"], entry["kinds"], entry["gap"])
        glyphs[key] = {"name": name, "count": entry["count"], "size": [entry["width"], entry["height"]],
                       "example": entry["example"]}
    path = catalogue_path(piece)
    path.write_text(json.dumps({"_help": "fingerprint -> symbol name (see tools/omr/vector.py); "
                                         "check names against omr/views/glyphs.png",
                                "glyphs": glyphs}, indent=1) + "\n", encoding="utf-8")
    return path


def render_exemplars(piece: Path, out: Path) -> Path:
    """One example of each catalogued shape, numbered as in glyphs.json, with its name."""
    import pymupdf
    from PIL import Image, ImageDraw
    data = json.loads(catalogue_path(piece).read_text(encoding="utf-8"))["glyphs"]
    tiles = []
    for number, (key, entry) in enumerate(data.items()):
        pdf, page_number, x0, y0, x1, y1 = entry["example"]
        with pymupdf.open(piece / pdf) as doc:
            clip = pymupdf.Rect(x0 - 3, y0 - 3, x1 + 3, y1 + 3)
            png = doc[page_number].get_pixmap(matrix=pymupdf.Matrix(6, 6), clip=clip).tobytes("png")
        image = Image.open(io.BytesIO(png)).convert("RGB")
        tile = Image.new("RGB", (220, 160), "white")
        image.thumbnail((210, 110))
        tile.paste(image, (5, 5))
        ImageDraw.Draw(tile).text((5, 125), f"#{number} {entry['name']} x{entry['count']}", fill=(200, 0, 0))
        tiles.append(tile)
    columns = 8
    sheet = Image.new("RGB", (220 * columns, 160 * ((len(tiles) + columns - 1) // columns)), "white")
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % columns) * 220, (index // columns) * 160))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def piece_pdfs(piece: Path) -> list[Path]:
    import pages
    return sorted((p for p in piece.iterdir() if p.suffix.lower() == ".pdf"), key=pages.page_order)


def pdf_pages(pdfs: list[Path]) -> list[tuple[Path, int]]:
    """Every page of the PDFs in order, as (pdf, page index in it): page N of
    a piece, counted from 1, is entry N - 1."""
    import pymupdf
    result = []
    for pdf in pdfs:
        with pymupdf.open(pdf) as doc:
            result += [(pdf, index) for index in range(doc.page_count)]
    return result


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "survey":
        piece = Path(argv[1])
        minimum = int(argv[argv.index("--min") + 1]) if "--min" in argv else 1
        clusters = survey(piece, piece_pdfs(piece))
        path = write_draft(piece, clusters, minimum)
        sheet = render_exemplars(piece, piece / "omr" / "views" / "glyphs.png")
        named = Counter(e["name"] for e in json.loads(path.read_text())["glyphs"].values())
        print(f"{len(clusters)} distinct shapes; catalogue draft {path}; exemplars {sheet}")
        print("names:", dict(named))
        return 0
    if len(argv) == 3 and argv[0] == "show":
        import pymupdf
        piece = Path(argv[1])
        pdf, index = pdf_pages(piece_pdfs(piece))[int(argv[2]) - 1]
        with pymupdf.open(pdf) as doc:
            page = read_page(doc[index])
        name_glyphs(page, load_catalogue(piece))
        for index, stave in enumerate(page.staves):
            counts = Counter(g.name for g in page.glyphs if g.stave == index)
            print(f"stave {index}: y {stave.top:.1f}-{stave.bottom:.1f} gap {stave.gap:.2f}, "
                  f"{page.bar_count(index)} bars, {dict(counts)}")
        return 0
    if len(argv) == 6 and argv[0] == "picture" and all(a.isdigit() for a in argv[2:]):
        print(picture(Path(argv[1]), *(int(a) for a in argv[2:])))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


def picture(piece: Path, page_number: int, stave_index: int, first: int, last: int | None = None) -> Path:
    """A PNG of bars `first` to `last` (counted on the page from 0) of one
    drawn stave (from 0, top down), with room above and below for marks and
    lyrics, written to library/<piece>/omr/views/bars/."""
    import pymupdf
    last = first if last is None else last
    pdf, index = pdf_pages(piece_pdfs(piece))[page_number - 1]
    out = piece / "omr" / "views" / "bars" / f"page{page_number}-stave{stave_index}-bars{first}-{last}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(pdf) as doc:
        page = read_page(doc[index])
        stave = page.staves[stave_index]
        lines = page.bar_lines[stave_index]
        clip = pymupdf.Rect(lines[first] - 4, stave.top - stave.gap * 7, lines[min(last + 1, len(lines) - 1)] + 4,
                            stave.bottom + stave.gap * 7)
        doc[index].get_pixmap(clip=clip, dpi=400).save(out)
    return out


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
