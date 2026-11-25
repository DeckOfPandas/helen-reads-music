#!/usr/bin/env python3
"""Lyrics, chord symbols and singers' names from an engraved PDF's text layer.

Notation software that keeps its words as text (MuseScore does) prints every
syllable and chord symbol at an exact place, where Audiveris can only guess
at the letters. process.py runs this after the drawing check, when the notes
are final. In each drawn system (drawing_check.drawn_systems):
- words between a vocal part's stave and the stave below are its lyrics, one
  verse per baseline, each on the note whose drawn head it is centred under;
  a drawn hyphen after a syllable joins it to the next, a drawn line under one
  is an extender;
- above the system, a group of words that all read as chord symbols is
  chords, each at the drawn note or rest its left edge sits over; other words
  there ending ":" are singers' names, written above the vocal part.
Where the text layer has lyrics or chords, those Audiveris read are dropped,
and so is every direction of words alone in the bars it covers.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import drawing_check
import repair
import score

LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}
ACCIDENTAL = {"": 0, "♯": 1, "#": 1, "♭": -1, "b": -1}
# Chord qualities, longest first: (printed suffix, MusicXML kind, degrees added).
QUALITIES = [
    ("maj7", "major-seventh", []), ("maj9", "major-ninth", []), ("m7b5", "half-diminished", []),
    ("dim7", "diminished-seventh", []), ("7sus4", "suspended-fourth", [("add", 7, 0)]),
    ("sus4", "suspended-fourth", []), ("sus2", "suspended-second", []), ("sus", "suspended-fourth", []),
    ("m7", "minor-seventh", []), ("m6", "minor-sixth", []), ("m9", "minor-ninth", []),
    ("dim", "diminished", []), ("aug", "augmented", []), ("+", "augmented", []), ("m", "minor", []),
    ("7", "dominant", []), ("9", "dominant-ninth", []), ("6", "major-sixth", []), ("5", "power", []),
    ("", "major", []),
]
CHORD = re.compile(r"^([A-G])([♯♭#b]?)(.*?)(?:/([A-G])([♯♭#b]?))?$")
ALTERATION = re.compile(r"([b#])(5|9|11|13)")


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    y: float                         # baseline
    size: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


def page_words(pg) -> list[Word]:
    """Every word of ordinary text on the page, split at spaces; music
    symbols (SMuFL's private-use characters) left out."""
    words = []
    for block in pg.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                chars: list[dict] = []
                for ch in span["chars"] + [{"c": " "}]:
                    if ch["c"].isspace() or 0xE000 <= ord(ch["c"]) <= 0xF8FF:
                        if chars:
                            text = "".join(LIGATURES.get(c["c"], c["c"]) for c in chars)
                            words.append(Word(text, chars[0]["bbox"][0], chars[-1]["bbox"][2],
                                              chars[0]["origin"][1], span["size"]))
                        chars = []
                    else:
                        chars.append(ch)
    return words


def page_lines(pg) -> list[tuple[float, float, float]]:
    """Every short stroked horizontal line (x0, x1, y): lyric hyphens and extenders among them."""
    lines = []
    for d in pg.get_drawings():
        if d.get("fill") is not None:
            continue
        for item in d["items"]:
            if item[0] == "l" and abs(item[1].y - item[2].y) < 0.05 and abs(item[1].x - item[2].x) > 0.5:
                lines.append((min(item[1].x, item[2].x), max(item[1].x, item[2].x), item[1].y))
    return lines


def parse_chord(text: str) -> dict | None:
    """A chord symbol as MusicXML wants it, or None if the text is not one."""
    if text == "N.C.":
        return {"step": "C", "alter": 0, "kind": "none", "text": "N.C.", "degrees": [], "bass": None}
    match = CHORD.match(text)
    if not match:
        return None
    step, accidental, suffix, bass_step, bass_accidental = match.groups()
    plain = suffix.replace("♯", "#").replace("♭", "b")
    for printed, kind, degrees in QUALITIES:
        if not plain.startswith(printed):
            continue
        rest = plain[len(printed):]
        degrees = list(degrees)
        while rest:
            alteration = ALTERATION.match(rest)
            if not alteration:
                break
            value = int(alteration.group(2))
            degrees.append(("alter" if value == 5 else "add", value, 1 if alteration.group(1) == "#" else -1))
            rest = rest[alteration.end():]
        if rest:
            continue
        return {"step": step, "alter": ACCIDENTAL[accidental], "kind": kind, "text": suffix, "degrees": degrees,
                "bass": (bass_step, ACCIDENTAL[bass_accidental or ""]) if bass_step else None}
    return None


def harmony_element(chord: dict) -> ET.Element:
    harmony = ET.Element("harmony")
    root = ET.SubElement(harmony, "root")
    ET.SubElement(root, "root-step").text = chord["step"]
    if chord["alter"]:
        ET.SubElement(root, "root-alter").text = str(chord["alter"])
    ET.SubElement(harmony, "kind", {"text": chord["text"]}).text = chord["kind"]
    if chord["bass"]:
        bass = ET.SubElement(harmony, "bass")
        ET.SubElement(bass, "bass-step").text = chord["bass"][0]
        if chord["bass"][1]:
            ET.SubElement(bass, "bass-alter").text = str(chord["bass"][1])
    for kind, value, alter in chord["degrees"]:
        degree = ET.SubElement(harmony, "degree")
        ET.SubElement(degree, "degree-value").text = str(value)
        ET.SubElement(degree, "degree-alter").text = str(alter)
        ET.SubElement(degree, "degree-type").text = kind
    return harmony


def _syllabics(words: list[Word], lines: list[tuple[float, float, float]], right: float) -> list[tuple[str, bool]]:
    """For a verse's words on one system, left to right: whether a drawn
    hyphen follows each (to the next word, or the system's end), and
    whether an extender line does."""
    out = []
    for word, after in zip(words, words[1:] + [None]):
        limit = after.x0 + 1 if after else right
        between = [line for line in lines if word.x1 - 1 <= line[0] < limit and line[1] <= limit + 1
                   and word.y - word.size <= line[2] <= word.y + word.size * 0.4]
        hyphen = any(line[2] < word.y - word.size * 0.15 for line in between)
        extend = any(line[2] >= word.y - word.size * 0.15 and line[0] - word.x1 < word.size for line in between)
        out.append(("hyphen" if hyphen else "", extend))
    return out


def _plain(word: str) -> str:
    return "".join(LIGATURES.get(c, c) for c in word).strip().lower()


def _printed_by_bar(systems: list, words_by_page: dict, used: set[tuple]) -> dict[str, set[str]]:
    """Per bar number of every system on a page with a text layer: the words
    printed in it, anywhere, but those written here already (`used`, as
    (page, x0, baseline)): lyrics, chords and names."""
    printed: dict[str, set[str]] = {}
    for system in systems:
        words = words_by_page[system.page_number][0]
        if not words:
            continue
        page = system.page
        top, bottom = page.staves[system.staves[0]].top, page.staves[system.staves[-1]].bottom
        bar_lines = page.bar_lines[system.staves[0]]
        for k, number in enumerate(system.bars):
            printed.setdefault(number, set())
        for word in words:
            if not top - 60 < word.y < bottom + 40 or (system.page_number, word.x0, word.y) in used:
                continue
            k = next((k for k, (a, b) in enumerate(zip(bar_lines, bar_lines[1:])) if a <= word.cx < b), None)
            if k is not None and k < len(system.bars):
                printed[system.bars[k]].add(_plain(word.text))
    return printed


def place(root: ET.Element, piece: Path, pdfs: list[Path], assume: Fraction | None = None,
          vocal_parts: list[str] | None = None, chords_part: str | None = None) -> list[str]:
    """Write the text layer's lyrics, chord symbols and singers' names into
    the score. Report lines."""
    import pymupdf
    vocal_parts = vocal_parts or ["Voice"]
    systems, slots, _ = drawing_check.drawn_systems(root, piece, pdfs)
    parts = {name: part for (_, name), part in zip(score.part_names(root), root.findall("part"))}
    if not systems:
        return ["text layer: no drawn systems to place words by; skipped"]
    chords_part = chords_part or next(iter(parts))
    if chords_part not in parts:
        raise ValueError(f"chords_part names no part {chords_part!r}")
    lengths = {name: drawing_check.bar_lengths(part, assume) for name, part in parts.items()}
    measures = {name: {m.get("number"): m for m in part.findall("measure")} for name, part in parts.items()}
    words_by_page: dict[int, tuple[list[Word], list]] = {}
    for system in systems:
        if system.page_number not in words_by_page:
            with pymupdf.open(system.pdf) as doc:
                pg = doc[system.index]
                words_by_page[system.page_number] = (page_words(pg), page_lines(pg))

    lyric_items = []                # (verse, part, bar, onset, text, hyphen, extend)
    chord_items = []                # (bar, onset, chord)
    name_items = []                 # (part, bar, onset, text)
    unplaced: list[str] = []
    other_text: list[str] = []
    used: set[tuple] = set()        # (page, x0, baseline) of each word written as lyric, chord or name
    voiced_bars: dict[str, set[str]] = {}
    previous_bottom: dict[int, float] = {}
    for system in systems:
        page = system.page
        words, lines = words_by_page[system.page_number]
        top = page.staves[system.staves[0]]
        ceiling = previous_bottom.get(system.page_number, 0.0)
        previous_bottom[system.page_number] = page.staves[system.staves[-1]].bottom
        bar_lines = page.bar_lines[system.staves[0]]

        def bar_at(x: float) -> int | None:
            return next((k for k, (a, b) in enumerate(zip(bar_lines, bar_lines[1:])) if a <= x < b), None)

        events_cache: dict[tuple[int, int], list] = {}

        def events(stave: int, k: int, part_name: str):
            if (stave, k) not in events_cache:
                number = system.bars[k]
                known = lengths[part_name].get(number)
                events_cache[(stave, k)] = drawing_check.drawn_events(system, stave, k, known[1]) if known else []
            return events_cache[(stave, k)]

        vocal = [(stave, slots[j].part) for stave, j in system.mapping.items()
                 if j is not None and slots[j].part in vocal_parts and slots[j].staff == 1]
        for stave, part_name in vocal:
            voiced_bars.setdefault(part_name, set()).update(system.bars)
            below = [s for s in system.staves if page.staves[s].top > page.staves[stave].bottom]
            floor = page.staves[below[0]].top if below else page.staves[stave].bottom + page.staves[stave].gap * 8
            band = sorted((w for w in words if page.staves[stave].bottom < w.y < floor), key=lambda w: w.x0)
            baselines = sorted({round(w.y * 2) / 2 for w in band})
            for verse, baseline in enumerate(baselines, start=1):
                line_words = [w for w in band if round(w.y * 2) / 2 == baseline]
                marks = _syllabics(line_words, lines, page.staves[stave].right)
                for word, (hyphen, extend) in zip(line_words, marks):
                    k = bar_at(word.cx)
                    drawn = [e for e in events(stave, k, part_name) if e[3]] if k is not None else []
                    if not drawn:
                        unplaced.append(f"{part_name} bar {system.bars[k] if k is not None else '?'}: "
                                        f"{word.text!r} (no note read under it)")
                        continue
                    _, _, onset, _ = min(drawn, key=lambda e: abs(e[1] - word.cx))
                    lyric_items.append((verse, part_name, system.bars[k], onset, word.text, hyphen, extend))
                    used.add((system.page_number, word.x0, word.y))

        above = sorted((w for w in words if ceiling < w.y < top.top), key=lambda w: (round(w.y), w.x0))
        groups: list[list[Word]] = []
        for word in above:                      # words on one baseline, a space apart, are one group
            if groups and abs(groups[-1][-1].y - word.y) < 0.5 and word.x0 - groups[-1][-1].x1 < word.size * 0.6:
                groups[-1].append(word)
            else:
                groups.append([word])
        for group in groups:
            chords = [parse_chord(w.text) for w in group]
            text = " ".join(w.text for w in group)
            k = bar_at(group[0].x0)
            if k is None:
                continue
            if all(chords):
                for word, chord in zip(group, chords):
                    k = bar_at(word.x0)
                    drawn = [e for s in system.staves if system.mapping.get(s) is not None
                             for e in events(s, k, slots[system.mapping[s]].part)]
                    if not drawn:
                        unplaced.append(f"chord bar {system.bars[k]}: {word.text!r} (no note read under it)")
                        continue
                    _, _, onset, _ = min(drawn, key=lambda e: abs(e[0] - word.x0))
                    chord_items.append((system.bars[k], onset, chord))
                    used.add((system.page_number, word.x0, word.y))
            elif text.endswith(":") and vocal:
                stave, part_name = vocal[0]
                drawn = events(stave, k, part_name)
                onset = min(drawn, key=lambda e: abs(e[0] - group[0].x0))[2] if drawn else Fraction(0)
                name_items.append((part_name, system.bars[k], onset, text))
                used.update((system.page_number, w.x0, w.y) for w in group)
            elif not text.isdigit():                    # fret, bar and page numbers say nothing here
                other_text.append(f"page {system.page_number}: {text!r}")

    report = []
    if lyric_items:
        for part_name, bars in voiced_bars.items():
            for number in bars:
                for note in measures[part_name][number].findall("note"):
                    for lyric in note.findall("lyric"):
                        note.remove(lyric)
        continuing: dict[tuple[str, int], bool] = {}
        placed = 0
        for verse, part_name, number, onset, text, hyphen, extend in lyric_items:
            measure = measures[part_name][number]
            divisions, _ = lengths[part_name][number]
            note = next((n for t, n in drawing_check.onsets(measure) if t == onset * divisions and n.find("rest") is None
                         and (n.findtext("staff") or "1") == "1"), None)
            if note is None:
                unplaced.append(f"{part_name} bar {number}: {text!r} (no note in the score at beat {onset})")
                continue
            before = continuing.get((part_name, verse), False)
            syllabic = {(False, False): "single", (False, True): "begin", (True, True): "middle",
                        (True, False): "end"}[(before, bool(hyphen))]
            continuing[(part_name, verse)] = bool(hyphen)
            lyric = ET.Element("lyric", {"number": str(verse)})
            ET.SubElement(lyric, "syllabic").text = syllabic
            ET.SubElement(lyric, "text").text = text
            if extend:
                ET.SubElement(lyric, "extend")
            repair._place(note, lyric)
            placed += 1
        report.append(f"text layer: {placed} lyric syllable(s) written, the read's own lyrics dropped "
                      f"from {sum(len(b) for b in voiced_bars.values())} bar(s)")
    if chord_items:
        for part in parts.values():
            for measure in part.findall("measure"):
                for harmony in measure.findall("harmony"):
                    measure.remove(harmony)
        numbers = {m.get("number"): i for i, m in enumerate(parts[chords_part].findall("measure"))}
        for number, onset, chord in sorted(chord_items, key=lambda c: (numbers.get(c[0], -1), c[1])):
            measure = measures[chords_part].get(number)
            if measure is None or number not in lengths[chords_part]:
                continue
            drawing_check.insert_at(measure, harmony_element(chord), onset * lengths[chords_part][number][0])
        report.append(f"text layer: {len(chord_items)} chord symbol(s) written into {chords_part}, "
                      f"the read's own dropped")
    if lyric_items or chord_items or name_items:
        printed = _printed_by_bar(systems, words_by_page, used)
        dropped = kept = 0
        for part in parts.values():
            for measure in part.findall("measure"):
                if measure.get("number") not in printed:
                    continue
                for direction in measure.findall("direction"):
                    kinds = {c.tag for t in direction.findall("direction-type") for c in t}
                    if kinds != {"words"}:
                        continue
                    said = [_plain(w) for t in direction.iter("words") for w in (t.text or "").split()]
                    if said and all(w in printed[measure.get("number")] for w in said):
                        kept += 1                       # printed there: "cresc.", "rit."
                    else:
                        measure.remove(direction)
                        dropped += 1
        report.append(f"text layer: {dropped} word direction(s) the read made that the page does not print "
                      f"dropped (chord boxes, names read as text), {kept} it does print kept")
    for part_name, number, onset, text in name_items:
        direction = ET.Element("direction", {"placement": "above"})
        ET.SubElement(ET.SubElement(direction, "direction-type"), "words").text = text
        drawing_check.insert_at(measures[part_name][number], direction, onset * lengths[part_name][number][0])
    if name_items:
        report.append(f"text layer: {len(name_items)} singer name(s): "
                      + ", ".join(f"{t} (bar {n})" for _, n, _, t in name_items))
    if not report:
        report.append("text layer: no lyrics, chord symbols or singers' names found")
    report += [f"  NOT PLACED {line}" for line in unplaced]
    if other_text:
        report.append("  other text above the systems, not written: " + "; ".join(other_text))
    return report
