"""Generated engraved PDFs for the tests: staves, bar lines and symbols drawn
as vector shapes the way notation software exports them, so vector.py and
vector_read.py can be tested without a score from library/.

Every symbol is a filled path of a fixed shape, so it has one fingerprint
(vector.fingerprint). `Engraver.catalogue` names each fingerprint drawn,
as library/<piece>/glyphs.json does for a real piece.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

GAP = 3.0                     # stave line spacing, points
STAVE_WIDTH = 0.37
BAR_WIDTH = 0.5
STEM_WIDTH = 0.35
BLACK = (0, 0, 0)


def _polygon(width: float, height: float, sides: int) -> list[tuple[float, float]]:
    """A convex shape of the given box, its corners on a coarse grid."""
    points = []
    for i in range(sides):
        t = i / sides
        x = width / 2 + width / 2 * _cos(t)
        y = height / 2 + height / 2 * _sin(t)
        points.append((round(x * 4) / 4, round(y * 4) / 4))
    return points


def _cos(t: float) -> float:
    import math
    return math.cos(2 * math.pi * t)


def _sin(t: float) -> float:
    import math
    return math.sin(2 * math.pi * t)


# name: (width, height, kind, detail); kind "oval" or "polygon" (detail: sides) or "rect"
SHAPES = {
    "notehead-black": (3.5, 3.0, "oval", None),
    "notehead-half": (3.5, 3.0, "polygon", 10),
    "notehead-whole": (4.5, 3.0, "polygon", 12),
    "notehead-x": (3.25, 3.0, "polygon", 4),
    "rest-quarter": (3.0, 9.0, "polygon", 5),
    "rest-eighth": (3.5, 5.5, "polygon", 7),
    "rest-whole": (3.75, 1.5, "rect", None),
    "flag-8th-up": (3.0, 9.0, "polygon", 6),
    "flag-8th-down": (3.0, 9.0, "polygon", 9),
    "dot": (1.0, 1.0, "oval", None),
    "flat": (2.25, 6.5, "polygon", 8),
    "sharp": (2.5, 7.5, "polygon", 11),
    "natural": (1.75, 7.0, "polygon", 13),
    "clef-treble": (7.0, 20.0, "polygon", 14),
    "clef-bass": (7.5, 8.5, "polygon", 15),
    "clef-percussion": (3.0, 6.0, "polygon", 16),
    "accent": (4.0, 2.5, "polygon", 4),     # `catalogue` needs a shape that fills its box
}


@dataclass
class Stave:
    top: float
    left: float
    right: float
    bar_lines: list[float]

    @property
    def bottom(self) -> float:
        return self.top + 4 * GAP

    def y(self, step: float) -> float:
        """The y of a stave position: 0 the bottom line, 8 the top line."""
        return self.bottom - step * GAP / 2


@dataclass
class Engraver:
    width: float = 595.0
    height: float = 842.0
    stem_width: float = STEM_WIDTH
    bar_width: float = BAR_WIDTH
    drawn: list[tuple[int, pymupdf.Rect, str]] = field(default_factory=list)   # page index, box, name

    def __post_init__(self):
        self.doc = pymupdf.open()
        self.shape = None
        self.new_page()

    def new_page(self) -> None:
        """Draw from here on on a fresh page after the last."""
        if self.shape is not None:
            self.shape.commit()
        self.page = self.doc.new_page(width=self.width, height=self.height)
        self.shape = self.page.new_shape()

    # --- lines --------------------------------------------------------------------

    def _line(self, x0: float, y0: float, x1: float, y1: float, width: float) -> None:
        self.shape.draw_line((x0, y0), (x1, y1))
        self.shape.finish(color=BLACK, width=width, fill=None, closePath=False)

    def stave(self, top: float, bar_lines: list[float], by_bar: bool = False) -> Stave:
        """Five lines from the first bar line to the last, and the bar lines;
        `by_bar`, each bar's lines drawn on their own, end to end."""
        left, right = bar_lines[0], bar_lines[-1]
        spans = list(zip(bar_lines, bar_lines[1:])) if by_bar else [(left, right)]
        for k in range(5):
            for x0, x1 in spans:
                self._line(x0, top + k * GAP, x1, top + k * GAP, STAVE_WIDTH)
        stave = Stave(top, left, right, bar_lines)
        for x in bar_lines:
            self._line(x, stave.top, x, stave.bottom, self.bar_width)
        return stave

    def join(self, upper: Stave, lower: Stave) -> None:
        """Bar lines from the top of `upper` to the foot of `lower`: a grand stave."""
        for x in upper.bar_lines:
            self._line(x, upper.top, x, lower.bottom, self.bar_width)

    def stem(self, x: float, y0: float, y1: float) -> None:
        self._line(x, y0, x, y1, self.stem_width)

    def ledgers(self, stave: Stave, x: float, step: int) -> None:
        """Ledger lines out to a head at `step`, as wide as a head and a bit."""
        rungs = range(10, step + 1, 2) if step >= 10 else range(-2, step - 1, -2) if step <= -2 else []
        for rung in rungs:
            y = stave.y(rung)
            self._line(x - 1.0, y, x + 4.5, y, STAVE_WIDTH)

    # --- symbols ------------------------------------------------------------------

    def glyph(self, name: str, x: float, y: float) -> pymupdf.Rect:
        """A symbol with its top-left corner at (x, y)."""
        width, height, kind, detail = SHAPES[name]
        rect = pymupdf.Rect(x, y, x + width, y + height)
        if kind == "oval":
            self.shape.draw_oval(rect)
        elif kind == "rect":
            self.shape.draw_rect(rect)
        else:
            points = [(x + px, y + py) for px, py in _polygon(width, height, detail)]
            self.shape.draw_polyline(points + [points[0]])
        self.shape.finish(color=None, fill=BLACK, closePath=True)
        self.drawn.append((self.page.number, rect, name))
        return rect

    def head(self, stave: Stave, x: float, step: int, kind: str = "black") -> pymupdf.Rect:
        name = f"notehead-{kind}"
        height = SHAPES[name][1]
        self.ledgers(stave, x, step)
        return self.glyph(name, x, stave.y(step) - height / 2)

    def note(self, stave: Stave, x: float, step: int, kind: str = "black", stem: str | None = "up",
             flag: bool = False, dot: bool = False) -> tuple[pymupdf.Rect, float]:
        """A head with its stem (3.5 spaces); returns the head and the stem's far end."""
        rect = self.head(stave, x, step, kind)
        tip = None
        if stem == "up":
            tip = rect.y0 + 1.5 - GAP * 3.5
            self.stem(rect.x1, tip, rect.y0 + 1.5)
            if flag:
                self.glyph("flag-8th-up", rect.x1 + 0.25, tip)
        elif stem == "down":
            tip = rect.y0 + 1.5 + GAP * 3.5
            self.stem(rect.x0, rect.y0 + 1.5, tip)
            if flag:
                self.glyph("flag-8th-down", rect.x0 + 0.25, tip - 9.0)
        if dot:
            self.glyph("dot", rect.x1 + 1.0, rect.y0 + 1.0)
        return rect, tip

    def beam(self, x0: float, x1: float, y: float) -> None:
        """A flat beam, its top edge at y."""
        self.shape.draw_rect(pymupdf.Rect(x0, y, x1, y + 1.5))
        self.shape.finish(color=None, fill=BLACK)

    def tie(self, x0: float, x1: float, y: float, blunt: bool = False) -> None:
        """A tie below y: a filled crescent of two curves; `blunt`, with a
        short straight edge at each end, as some software draws one."""
        middle = (x0 + x1) / 2
        end = 0.3 if blunt else 0.0
        self.shape.draw_bezier((x0, y), (middle - 1, y + 2.5), (middle + 1, y + 2.5), (x1, y))
        if blunt:
            self.shape.draw_line((x1, y), (x1, y + end))
        self.shape.draw_bezier((x1, y + end), (middle + 1, y + 1.5), (middle - 1, y + 1.5), (x0, y + end))
        if blunt:
            self.shape.draw_line((x0, y + end), (x0, y))
        self.shape.finish(color=None, fill=BLACK, closePath=True)

    def rest(self, stave: Stave, x: float, kind: str) -> pymupdf.Rect:
        """quarter, eighth, whole (hangs from the fourth line) or half (sits on
        the middle line)."""
        if kind == "whole":
            return self.glyph("rest-whole", x, stave.y(6))
        if kind == "half":
            return self.glyph("rest-whole", x, stave.y(4) - SHAPES["rest-whole"][1])
        name = f"rest-{kind}"
        return self.glyph(name, x, stave.y(4) - SHAPES[name][1] / 2)

    def clef(self, stave: Stave, kind: str) -> None:
        name = f"clef-{kind}"
        self.glyph(name, stave.left + 1.0, stave.y(4) - SHAPES[name][1] / 2)

    def sign(self, stave: Stave, name: str, x: float, step: int) -> None:
        """An accidental (or a key signature's) for `step`: a sharp or natural
        centred on it, a flat with its bulb on it, its stem rising above."""
        height = SHAPES[name][1]
        top = stave.y(step) + 0.7 * GAP - height if name == "flat" else stave.y(step) - height / 2
        self.glyph(name, x, top)

    # --- output -------------------------------------------------------------------

    def save(self, path: Path) -> Path:
        self.shape.commit()
        self.doc.save(path)
        return path

    def catalogue(self, pdf: Path, piece: Path) -> Path:
        """Name every fingerprint drawn (glyphs.json), matching each filled
        path in the saved PDF to the symbol drawn at its place."""
        import vector
        glyphs = {}
        with pymupdf.open(pdf) as doc:
            for page in doc:
                for d in page.get_drawings():
                    if d.get("fill") is None:
                        continue
                    r = d["rect"]
                    for number, rect, name in self.drawn:
                        if number == page.number and abs(r.x0 - rect.x0) < 0.3 and abs(r.y0 - rect.y0) < 0.3 \
                                and abs(r.x1 - rect.x1) < 0.3 and abs(r.y1 - rect.y1) < 0.3:
                            glyphs[vector.fingerprint(d)] = {"name": name}
                            break
        path = piece / "glyphs.json"
        path.write_text(json.dumps({"glyphs": glyphs}), encoding="utf-8")
        return path
