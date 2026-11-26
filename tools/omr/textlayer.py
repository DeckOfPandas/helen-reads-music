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
import vector

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
    symbols left out: SMuFL's private-use characters, every character of a
    music font (vector.FONT_GLYPHS), and control characters."""
    words = []
    for block in pg.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if vector.font_family(span["font"]) in vector.FONT_GLYPHS:
                    continue
                chars: list[dict] = []
                for ch in span["chars"] + [{"c": " "}]:
                    if ch["c"].isspace() or 0xE000 <= ord(ch["c"]) <= 0xF8FF or ord(ch["c"]) < 32:
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


# On a vocal stave, a pit score's list of who sings a line: capitals, numbers and
# punctuation, bracketed ("(M4)", "(M1,2,5-7,LEFOU)", "(ALL—except YELLERS)",
# a list broken over two lines). A cue has words in lower case: "(1st time only)".
SINGERS = re.compile(r"^\(?[+A-Z0-9,.\-—–/ ]*(?:except [A-Z ]+)?\)?$")
TEMPO = re.compile(r"^(.*?)\s*=\s*(\d+)$")
STAFF_TEXT_REACH = 12           # stave spaces: text farther from every stave is a page's heading or footing
ABOVE_PULL = 0.8                # text above a stave counts as this much nearer to it than below the one above
HEADING_SIZE = 2.0              # text this many times the page's usual size is a title, not a stave's
BOILERPLATE = re.compile(r"©|Rights Reserved|by Permission|Copyright", re.IGNORECASE)   # a footing near a stave


def _ink_top(page, stave: int, x0: float, x1: float) -> float:
    """The top of a stave, or of the highest stem between x0 and x1 that
    rises out of it."""
    lines = page.staves[stave]
    return min([lines.top] + [stem.y0 for stem in page.stems if x0 - lines.gap <= stem.x <= x1 + lines.gap
                              and stem.y1 > lines.top and stem.y0 < lines.bottom])


def without_singers(text: str) -> str:
    """A vocal stave's phrase without its bracketed singers' lists, which can
    share a line with a cue: "(M4) (last x)" -> "(last x)"; "" if nothing else."""
    pieces = [p.strip() for p in re.findall(r"\([^)]*\)?|[^()]+\)?", text) if p.strip()]
    return " ".join(p for p in pieces if not (("(" in p or ")" in p) and SINGERS.match(p)))


def _groups(words: list[Word]) -> list[list[Word]]:
    """Words on one baseline, a space apart, as phrases; a tempo's "= 152"
    joins the words before it across the note printed between them."""
    groups: list[list[Word]] = []
    for word in sorted(words, key=lambda w: (round(w.y, 1), w.x0)):
        reach = word.size * (3.0 if word.text.startswith("=") else 0.6)
        if groups and abs(groups[-1][-1].y - word.y) < 0.5 and word.x0 - groups[-1][-1].x1 < reach:
            groups[-1].append(word)
        else:
            groups.append([word])
    return groups


def _staff_texts(systems: list, slots: list, words_by_page: dict, used: set[tuple], lengths: dict,
                 other_text: list[str], vocal_parts: list[str]) -> tuple[list[tuple], int]:
    """Every printed phrase not used as a lyric, chord symbol or name, on the
    stave nearest it: (part, staff, bar, onset, text, placement), at the drawn
    note nearest its left edge. Left out: numbers (bar, fret, page), singers'
    lists, stave names in the margin, and text far from every stave (listed
    in `other_text`). Returns the items and how many singers' lists were left out."""
    items, singers = [], 0
    two_staves = {s.part for s in slots if s.staff > 1}
    pages: dict[int, list] = {}
    for system in systems:
        pages.setdefault(system.page_number, []).append(system)
    for page_number, page_systems in pages.items():
        page = page_systems[0].page
        words = [w for w in words_by_page[page_number][0] if (page_number, w.x0, w.y) not in used]
        sizes = sorted(w.size for w in words_by_page[page_number][0])
        usual = sizes[len(sizes) // 2] if sizes else 0
        for group in _groups(words):
            text = " ".join(w.text for w in group)
            if text.isdigit():
                continue
            if BOILERPLATE.search(text) or group[0].size > usual * HEADING_SIZE:
                other_text.append(f"page {page_number}: {text!r}")
                continue
            middle = group[0].y - group[0].size * 0.35
            # An instruction is printed above the stave it is for, clear of the stems
            # rising out of it; an expression close under its own stave. So the
            # distance to a stave's top counts from its highest stem under the text,
            # and text midway between two staves is the lower one's.
            tops = {s: _ink_top(page, s, group[0].x0, group[-1].x1) for sy in page_systems for s in sy.staves}

            def nearness(s: int) -> float:
                gap = page.staves[s].gap
                if middle < tops[s]:
                    return (tops[s] - middle) / gap * ABOVE_PULL
                return max(middle - page.staves[s].bottom, 0) / gap

            _, system, stave = min(((nearness(s), sy, s) for sy in page_systems for s in sy.staves),
                                   key=lambda t: t[0])
            distance = max(page.staves[stave].top - middle, middle - page.staves[stave].bottom, 0) / \
                page.staves[stave].gap
            lines = page.staves[stave]
            if distance > STAFF_TEXT_REACH:
                other_text.append(f"page {page_number}: {text!r}")
                continue
            if group[-1].x1 <= lines.left or system.mapping.get(stave) is None:
                continue                                # a stave's name in the margin
            slot = system.mapping[stave]
            if slots[slot].part in vocal_parts:
                kept = without_singers(text)
                singers += kept != text
                if not kept:
                    continue
                text = kept
            bar_lines = page.bar_lines[stave]
            k = next((k for k, (a, b) in enumerate(zip(bar_lines, bar_lines[1:])) if a <= group[0].x0 < b), 0)
            if k >= len(system.bars):
                continue
            part_name, staff, number = slots[slot].part, slots[slot].staff, system.bars[k]
            known = lengths[part_name].get(number)
            drawn = drawing_check.drawn_events(system, stave, k, known[1]) if known else []
            onset = min(drawn, key=lambda e: abs(e[0] - group[0].x0))[2] if drawn else Fraction(0)
            items.append((part_name, staff if part_name in two_staves else None, number, onset, text,
                          "above" if middle < lines.top else "below"))
    return items, singers


def number_as_printed(root: ET.Element, piece: Path, pdfs: list[Path]) -> list[str]:
    """Number the bars as the page does, where it prints a number under every
    bar of a system (a pit score skips cut bars: 1, 2, 7). Systems that print
    only their first bar's number keep the read's. Report lines."""
    renumber = printed_numbers(root, piece, pdfs)
    if not renumber:
        return ["bar numbers: the page prints none under every bar; the read's kept"]
    for part in root.findall("part"):
        for measure in part.findall("measure"):
            if measure.get("number") in renumber:
                measure.set("number", renumber[measure.get("number")])
    changed = sum(1 for old, new in renumber.items() if old != new)
    return [f"bar numbers as printed: {len(renumber)} bar(s) numbered from the page, {changed} of them "
            f"differing from the read's"]


def printed_numbers(root: ET.Element, piece: Path, pdfs: list[Path]) -> dict[str, str]:
    """{the read's bar number: the number printed under it}, for the systems
    that print a number under every bar."""
    import pymupdf
    systems, _, _ = drawing_check.drawn_systems(root, piece, pdfs)
    renumber: dict[str, str] = {}
    words_by_page: dict[int, list[Word]] = {}
    for system in systems:
        if system.page_number not in words_by_page:
            with pymupdf.open(system.pdf) as doc:
                words_by_page[system.page_number] = page_words(doc[system.index])
        page = system.page
        lowest = page.staves[system.staves[-1]]
        bar_lines = page.bar_lines[system.staves[-1]]
        found: dict[int, list[str]] = {}
        for word in words_by_page[system.page_number]:
            if word.text.isdigit() and lowest.bottom < word.y < lowest.bottom + lowest.gap * 10:
                k = next((k for k, (a, b) in enumerate(zip(bar_lines, bar_lines[1:])) if a <= word.cx < b), None)
                if k is not None and k < len(system.bars):
                    found.setdefault(k, []).append(word.text)
        if len(found) == len(system.bars) and all(len(v) == 1 for v in found.values()):
            renumber.update({system.bars[k]: found[k][0] for k in found})
    return renumber


def _staff_direction(text: str, placement: str, staff: int | None) -> ET.Element:
    """Words above or below a stave; "Moderately fast = 152" with a metronome
    mark and its tempo, a crotchet the beat (the text layer drops the note
    printed before "=")."""
    direction = ET.Element("direction", {"placement": placement})
    tempo = TEMPO.match(text)
    words = tempo.group(1) if tempo else text
    if words:
        ET.SubElement(ET.SubElement(direction, "direction-type"), "words").text = words
    if tempo:
        metronome = ET.SubElement(ET.SubElement(direction, "direction-type"), "metronome")
        ET.SubElement(metronome, "beat-unit").text = "quarter"
        ET.SubElement(metronome, "per-minute").text = tempo.group(2)
    if staff is not None:                           # a part of more than one stave
        ET.SubElement(direction, "staff").text = str(staff)
    if tempo:
        ET.SubElement(direction, "sound", {"tempo": tempo.group(2)})
    return direction

def place(root: ET.Element, piece: Path, pdfs: list[Path], assume: Fraction | None = None,
          vocal_parts: list[str] | None = None, chords_part: str | None = None,
          keep_lyrics: set[tuple[str, str]] | None = None) -> list[str]:
    """Write the text layer's lyrics, chord symbols and singers' names into
    the score. A bar in `keep_lyrics` ((part, bar number): a hand patch gave
    its lyrics) keeps them unless the text layer places its own. Report lines."""
    import pymupdf
    vocal_parts = vocal_parts or ["Voice"]
    keep_lyrics = keep_lyrics or set()
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
            k = next((k for k, (a, b) in enumerate(zip(bar_lines, bar_lines[1:])) if a <= x < b), None)
            return k if k is not None and k < len(system.bars) else None

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
                on_line = [w for w in band if round(w.y * 2) / 2 == baseline]
                # Some programs print a lyric's hyphen as text: it joins the word before to the next.
                dashed = {id(before) for before, w in zip(on_line, on_line[1:]) if not w.text.strip("-–")}
                used.update((system.page_number, w.x0, w.y) for w in on_line if not w.text.strip("-–"))
                line_words = [w for w in on_line if w.text.strip("-–")]
                marks = _syllabics(line_words, lines, page.staves[stave].right)
                marks = [("hyphen" if id(w) in dashed else hyphen, extend)
                         for w, (hyphen, extend) in zip(line_words, marks)]
                for word, (hyphen, extend) in zip(line_words, marks):
                    k = bar_at(word.cx)
                    drawn = [e for e in events(stave, k, part_name) if e[3]] if k is not None else []
                    if not drawn:
                        if k is not None and (part_name, system.bars[k]) in keep_lyrics:
                            used.add((system.page_number, word.x0, word.y))     # the patch wrote it
                            continue
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
            # Anything else above the system is left for the staff text pass.

    report = []
    if lyric_items:
        written = {(part_name, number) for _, part_name, number, *_ in lyric_items}
        for part_name, bars in voiced_bars.items():
            for number in bars:
                if (part_name, number) in keep_lyrics and (part_name, number) not in written:
                    continue                    # a hand patch's lyrics, where the page's words found no note
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
    staff_items, skipped = _staff_texts(systems, slots, words_by_page, used, lengths, other_text, vocal_parts)
    if lyric_items or chord_items or name_items or staff_items:
        covered = {number for system in systems if words_by_page[system.page_number][0] for number in system.bars}
        dropped = 0
        for part in parts.values():
            for measure in part.findall("measure"):
                if measure.get("number") not in covered:
                    continue
                for direction in measure.findall("direction"):
                    kinds = {c.tag for t in direction.findall("direction-type") for c in t}
                    if kinds == {"words"}:
                        measure.remove(direction)
                        dropped += 1
        report.append(f"text layer: {dropped} word direction(s) the read made dropped (chord boxes and names "
                      "read as text, words misread); every printed word is written from the text layer")
    for part_name, staff, number, onset, text, placement in staff_items:
        measure = measures[part_name].get(number)
        if measure is None or number not in lengths[part_name]:
            continue
        drawing_check.insert_at(measure, _staff_direction(text, placement, staff),
                                onset * lengths[part_name][number][0])
    if staff_items:
        report.append(f"text layer: {len(staff_items)} text(s) written on the stave they are printed by "
                      f"(instruments, sounds, cues, expressions); {skipped} singers' lists left out")
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
