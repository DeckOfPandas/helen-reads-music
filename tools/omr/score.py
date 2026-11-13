#!/usr/bin/env python3
"""Read, check and write MusicXML (.mxl or .musicxml/.xml).

USAGE (from the repo root):
    python3 tools/omr/score.py check <score.mxl> [<score.mxl> ...] [--time 4/4]
    python3 tools/omr/score.py names <score.mxl>
    python3 tools/omr/score.py diff <old.mxl> <new.mxl>
    python3 tools/omr/score.py extract <score> <out> <part name> [<part name> ...]

check   Per part: bars, notes, rests, octave range, whether it carries a
        <transpose> (a part without one is read as concert pitch), and every
        bar whose notes and rests do not add up to its time signature. Those
        bars are where Audiveris dropped or invented a note or rest; the
        report gives the bar number, what it holds and what it should.
        A pickup bar (implicit="yes") is allowed to be short.
        Audiveris can miss a time signature altogether. A bar with none in
        force is judged against --time (read it off the printed page); with
        no --time it is reported NOT CHECKED, never passed.
        Also every bar with a note below the instrument's lowest written
        note (by its part name): usually a stave read an octave low.
names  Every part name, in score order: the raw material for a merge config.
diff   Every bar, per part (matched by name), whose notes or marks differ
       between two versions: lyrics, articulations, noteheads, directions.
       Save the merged score before changing a tool or merge.json, run
       again, and diff: what changed is what to look at.
extract  A copy of the score with only the named parts (as `names` prints
       them), to .mxl or plain .musicxml by <out>'s suffix: the band's
       parts without the singer's, say. Read the whole score first; the
       drawing check matches staves to every part.

This reads structure, not notation: it says where to look, it is not a
proofread.
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path


# --- reading and writing --------------------------------------------------------

def _inner_name(z: zipfile.ZipFile) -> str:
    """The score inside an .mxl: the rootfile META-INF/container.xml names,
    else the first .xml outside META-INF."""
    try:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        for rootfile in container.iter():
            if rootfile.tag.endswith("rootfile") and rootfile.get("full-path"):
                return rootfile.get("full-path")
    except KeyError:
        pass
    names = [n for n in z.namelist()
             if n.endswith((".xml", ".musicxml")) and not n.startswith("META-INF")]
    if not names:
        raise ValueError("no score inside this .mxl")
    return names[0]


def load(path: Path | str) -> ET.Element:
    path = Path(path)
    if path.suffix.lower() == ".mxl":
        with zipfile.ZipFile(path) as z:
            return ET.fromstring(z.read(_inner_name(z)))
    return ET.fromstring(path.read_bytes())


# Audiveris writes version="4.0.3", the version of its MusicXML library,
# not of MusicXML; Sibelius does not recognise it and warns that the file
# is an old version. Any version MusicXML never had is written as this one.
MUSICXML_VERSION = "4.0"
MUSICXML_VERSIONS = {"1.0", "1.1", "2.0", "3.0", "3.1", "4.0"}
DOCTYPES = {
    "score-partwise": '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML {version} Partwise//EN" '
                      '"http://www.musicxml.org/dtds/partwise.dtd">',
}


def save(root: ET.Element, out: Path | str, like: Path | str | None = None) -> None:
    """Write `root` to `out` (.mxl or plain XML), with the DOCTYPE naming
    its MusicXML version, a real one. For .mxl, every other member of `like`
    (container, images) is copied across. Written to a side file and
    renamed, so a failure never leaves half a score."""
    out = Path(out)
    if root.tag in DOCTYPES and root.get("version") not in MUSICXML_VERSIONS:
        root.set("version", MUSICXML_VERSION)
    xml_bytes = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    if root.tag in DOCTYPES:
        declaration, rest = xml_bytes.split(b"\n", 1)
        doctype = DOCTYPES[root.tag].format(version=root.get("version") or MUSICXML_VERSION)
        xml_bytes = declaration + b"\n" + doctype.encode() + b"\n" + rest
    tmp = out.with_name(out.name + ".partial")
    if out.suffix.lower() != ".mxl":
        tmp.write_bytes(xml_bytes)
    else:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            inner = "score.xml"
            if like is not None and Path(like).suffix.lower() == ".mxl":
                with zipfile.ZipFile(like) as zin:
                    inner = _inner_name(zin)
                    for item in zin.infolist():
                        if item.filename != inner:
                            zout.writestr(item, zin.read(item.filename))
            else:
                zout.writestr("META-INF/container.xml", (
                    '<?xml version="1.0" encoding="UTF-8"?>\n<container><rootfiles>'
                    f'<rootfile full-path="{inner}"/></rootfiles></container>'))
            zout.writestr(inner, xml_bytes)
    os.replace(tmp, out)


def extract(root: ET.Element, keep: list[str]) -> ET.Element:
    """The score with only the parts named in `keep`, in score order; a
    bracket or brace (<part-group>) left holding none of them goes too."""
    names = dict(part_names(root))
    missing = [name for name in keep if name not in names.values()]
    if missing:
        raise ValueError(f"no part named {', '.join(missing)}; the parts are {', '.join(names.values())}")
    gone = {pid for pid, name in names.items() if name not in keep}
    part_list = root.find("part-list")
    open_groups: dict[str, tuple[ET.Element, bool]] = {}
    for child in list(part_list):
        if child.tag == "score-part":
            if child.get("id") in gone:
                part_list.remove(child)
            else:
                open_groups = {n: (start, True) for n, (start, _) in open_groups.items()}
        elif child.tag == "part-group" and child.get("type") == "start":
            open_groups[child.get("number", "1")] = (child, False)
        elif child.tag == "part-group" and child.get("type") == "stop":
            start, used = open_groups.pop(child.get("number", "1"), (None, True))
            if not used:
                part_list.remove(start)
                part_list.remove(child)
    for part in root.findall("part"):
        if part.get("id") in gone:
            root.remove(part)
    return root


def part_names(root: ET.Element) -> list[tuple[str, str]]:
    """(part id, part name) in score order."""
    part_list = root.find("part-list")
    if part_list is None:
        return []
    return [(sp.get("id"), (sp.findtext("part-name") or "").strip())
            for sp in part_list.iter("score-part")]


# --- counting -------------------------------------------------------------------

def is_rest(note: ET.Element) -> bool:
    return note.find("rest") is not None


def count_notes(measure: ET.Element) -> int:
    """Sounding notes (not rests) in a measure, chord members counted singly."""
    return sum(1 for n in measure.findall("note") if not is_rest(n))


def count_part_notes(part: ET.Element) -> int:
    return sum(count_notes(m) for m in part.findall("measure"))


# --- bar durations --------------------------------------------------------------

@dataclass
class BadBar:
    number: str
    holds: Fraction                       # quarter notes the bar's notes fill
    expected: Fraction                    # quarter notes the time signature asks for
    voices: dict[str, Fraction] = field(default_factory=dict)

    def describe(self) -> str:
        what = "short" if self.holds < self.expected else "long"
        line = f"bar {self.number}: {_beats(self.holds)} of {_beats(self.expected)} ({what})"
        if len(self.voices) > 1:
            parts = ", ".join(f"voice {v} {_beats(d)}" for v, d in sorted(self.voices.items()))
            line += f" [{parts}]"
        return line


def _beats(quarters: Fraction) -> str:
    text = str(quarters) if quarters.denominator != 1 else str(quarters.numerator)
    return f"{text} crotchet{'s' if quarters != 1 else ''}"


def _time_signature(attributes: ET.Element) -> Fraction | None:
    time = attributes.find("time")
    if time is None or time.find("senza-misura") is not None:
        return None
    try:
        beats = sum(int(b) for b in (time.findtext("beats") or "").split("+"))
        beat_type = int(time.findtext("beat-type") or "")
    except ValueError:
        return None
    return Fraction(4 * beats, beat_type)


def parse_time(text: str) -> Fraction:
    """"4/4" or "6/8" as crotchets per bar."""
    beats, _, beat_type = text.partition("/")
    if not (beats.isdigit() and beat_type.isdigit() and int(beats) and int(beat_type)):
        raise ValueError(f"not a time signature: {text!r}")
    return Fraction(4 * int(beats), int(beat_type))


def bad_bars(part: ET.Element, assume: Fraction | None = None) -> list[BadBar]:
    """Bars whose content does not fill the time signature exactly."""
    return judge(part, assume)[0]


def judge(part: ET.Element, assume: Fraction | None = None) -> tuple[list[BadBar], int]:
    """(bars that do not add up, count of bars that could not be judged).
    A bar is judged against the time signature in force, else `assume`.
    Without either it cannot be judged, and is counted, never passed."""
    divisions = None
    expected = assume
    unjudged = 0
    bad = []
    printed = True
    for measure in part.findall("measure"):
        cursor = longest = Fraction(0)
        voices: dict[str, Fraction] = {}
        for details in measure.iter("staff-details"):
            printed = details.get("print-object", "yes") != "no"
        for element in measure:
            if element.tag == "attributes":
                if element.findtext("divisions"):
                    divisions = Fraction(element.findtext("divisions"))
                signature = _time_signature(element)
                if signature is not None:
                    expected = signature
                continue
            if divisions is None or element.tag not in ("note", "backup", "forward"):
                continue
            duration = Fraction(element.findtext("duration") or 0) / divisions
            if element.tag == "backup":
                cursor -= duration
            elif element.tag == "forward":
                cursor += duration
            elif element.find("grace") is None and element.find("chord") is None:
                cursor += duration
                voice = element.findtext("voice") or "1"
                voices[voice] = voices.get(voice, Fraction(0)) + duration
            longest = max(longest, cursor)
        # A bar with nothing in it on staves not printed is a fragment's
        # padding where another fragment plays (book.join_scores, and
        # Audiveris itself): there is nothing to judge.
        if not printed and measure.find("note") is None:
            continue
        if expected is None or divisions is None:
            unjudged += 1
            continue
        if measure.get("implicit") == "yes" and longest <= expected:
            continue
        uneven = len(voices) > 1 and any(d != expected for d in voices.values())
        if longest != expected or uneven:
            bad.append(BadBar(measure.get("number") or "?", longest, expected, voices))
    return bad, unjudged


# --- written range --------------------------------------------------------------

# Lowest written note, as a MIDI number, by a word in the part name: the
# first word that matches wins, so "contrabass" is tried before "bass".
# An instrument not named here is not range-checked. A bass guitar is
# written under a bass clef with an 8 below, which MusicXML holds at
# sounding pitch: its open E string is E1.
LOWEST_WRITTEN = (
    ("piccolo", 62), ("flute", 60), ("oboe", 58), ("clarinet", 52), ("bassoon", 34),
    ("horn", 42), ("trumpet", 54), ("trombone", 40), ("violin", 55), ("viola", 48),
    ("violoncello", 36), ("cello", 36), ("contrabass", 40), ("double bass", 40),
    ("bass guitar", 28), ("electric bass", 28), ("bass", 40),
)
STEPS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def midi(note: ET.Element) -> int | None:
    pitch = note.find("pitch")
    if pitch is None:
        return None
    alter = Fraction(pitch.findtext("alter") or 0)
    return 12 * (int(pitch.findtext("octave")) + 1) + STEPS[pitch.findtext("step")] + int(alter)


def lowest_written(name: str) -> int | None:
    lowered = name.lower()
    for word, lowest in LOWEST_WRITTEN:
        if word in lowered:
            return lowest
    return None


def below_range(part: ET.Element, name: str) -> list[str]:
    """Bar numbers holding a note below the instrument's lowest written note:
    usually a whole stave read an octave low (a clef misread as "8 below")."""
    lowest = lowest_written(name)
    if lowest is None:
        return []
    bars = []
    for measure in part.findall("measure"):
        if any((m := midi(n)) is not None and m < lowest for n in measure.iter("note")):
            bars.append(measure.get("number") or "?")
    return bars


# --- the check ------------------------------------------------------------------

def check(path: Path | str, assume: Fraction | None = None) -> list[str]:
    """The report. Its last line totals the bars that do not add up and,
    if any, the bars that could not be checked."""
    root = load(path)
    names = dict(part_names(root))
    parts = root.findall("part")
    lines = [str(path), f"parts: {len(parts)}"]
    if not any(True for _ in root.iter("time")):
        lines.append("NO TIME SIGNATURE anywhere in this score: Audiveris did not read it"
                     + (f"; checking against --time ({_beats(assume)} a bar)" if assume else
                        ". Give --time (e.g. 4/4) from the printed page to check the bars."))
    counts = []
    total_bad = total_unjudged = 0
    for part in parts:
        pid = part.get("id")
        measures = part.findall("measure")
        counts.append(len(measures))
        notes = rests = 0
        octaves = []
        for note in part.iter("note"):
            if is_rest(note):
                rests += 1
            else:
                notes += 1
                octave = note.findtext("pitch/octave")
                if octave and octave.isdigit():
                    octaves.append(int(octave))
        transposed = part.find("measure/attributes/transpose") is not None
        flag = ""
        if not measures:
            flag = "  <-- NO BARS"
        elif notes + rests == 0:
            flag = "  <-- NO NOTES OR RESTS"
        elif rests / (notes + rests) > 0.95:
            flag = "  <-- almost all rests: a leftover fragment, or a silent part"
        span = f", octaves {min(octaves)}-{max(octaves)}" if octaves else ""
        pitch = "transposing" if transposed else "concert pitch (no <transpose>)"
        lines.append(f"[{pid}] {names.get(pid, pid)}: {len(measures)} bars, {notes} notes, "
                     f"{rests} rests{span}, {pitch}{flag}")
        bad, unjudged = judge(part, assume)
        total_bad += len(bad)
        total_unjudged += unjudged
        for bar in bad:
            lines.append(f"    {bar.describe()}")
        if unjudged:
            lines.append(f"    {unjudged} bar(s) NOT CHECKED: no time signature or divisions")
        low = below_range(part, names.get(pid, ""))
        if low:
            lines.append(f"    BELOW THE INSTRUMENT'S RANGE in bar(s) {', '.join(low)}: "
                         "probably read an octave low")
    if counts and len(set(counts)) > 1:
        lines.append(f"WARNING: parts have different bar counts: {counts}")
    total = f"bars that do not add up: {total_bad} of {sum(counts) - total_unjudged} checked"
    if total_unjudged:
        total += f"; {total_unjudged} NOT CHECKED (no time signature)"
    lines.append(total)
    return lines


# --- comparing two versions -------------------------------------------------------

def _bar_contents(measure: ET.Element, divisions: Fraction) -> tuple[list[tuple], Counter, Fraction]:
    """(notes, marks, divisions after the bar): notes as (voice, staff,
    onset, what, length, ties), sorted; marks as a count of lyrics,
    articulations, noteheads and directions."""
    time = last = Fraction(0)
    notes, marks = [], Counter()
    for element in measure:
        if element.tag == "attributes" and element.findtext("divisions"):
            divisions = Fraction(element.findtext("divisions"))
        elif element.tag in ("backup", "forward"):
            step = Fraction(element.findtext("duration") or 0) / divisions
            time += step if element.tag == "forward" else -step
        elif element.tag == "direction":
            for kind in element.findall("direction-type/*"):
                text = (kind.text or "").strip()
                marks[" ".join(["direction", kind.tag, text] + [c.tag for c in kind]).strip()] += 1
        elif element.tag == "note" and element.find("grace") is None:
            length = Fraction(element.findtext("duration") or 0) / divisions
            onset = last if element.find("chord") is not None else time
            if element.find("pitch") is not None:
                what = "".join(element.findtext(f"pitch/{k}") or "" for k in ("step", "alter", "octave"))
            elif element.find("unpitched") is not None:
                what = "".join(element.findtext(f"unpitched/{k}") or "" for k in ("display-step", "display-octave"))
            else:
                what = "rest" + (" hidden" if element.get("print-object") == "no" else "")
            ties = "".join(sorted(t.get("type") for t in element.findall("tie")))
            notes.append((element.findtext("voice") or "1", element.findtext("staff") or "1", onset, what,
                          length, ties))
            if element.find("chord") is None:
                last, time = time, time + length
            for lyric in element.findall("lyric"):
                marks[f"lyric {lyric.findtext('text') or ''}"] += 1
            for mark in element.findall("notations/articulations/*") + element.findall("notations/ornaments/*"):
                marks[mark.tag] += 1
            if element.findtext("notehead"):
                marks[f"notehead {element.findtext('notehead')}"] += 1
    return sorted(notes), marks, divisions


def diff(old_path: Path | str, new_path: Path | str) -> list[str]:
    """Every bar, per part, whose notes (pitch, onset, length, ties, voice)
    or marks (lyrics, articulations, noteheads, directions) differ between
    two versions of a score, parts matched by name."""
    def bars(path):
        root = load(path)
        out = {}
        for (_, name), part in zip(part_names(root), root.findall("part")):
            divisions, per_bar = Fraction(1), {}
            for measure in part.findall("measure"):
                notes, marks, divisions = _bar_contents(measure, divisions)
                per_bar[measure.get("number")] = (notes, marks)
            out[name] = per_bar
        return out

    def show(items) -> str:
        return ", ".join(str(i) for i in items) or "-"

    old, new = bars(old_path), bars(new_path)
    lines, changed = [], 0
    for name in new:
        if name not in old:
            lines.append(f"{name}: only in the new score")
            continue
        for number, (notes, marks) in new[name].items():
            before_notes, before_marks = old[name].get(number, ([], Counter()))
            if notes != before_notes:
                changed += 1
                lines.append(f"{name} bar {number} notes: old {show(n for n in before_notes if n not in notes)}; "
                             f"new {show(n for n in notes if n not in before_notes)}")
            if marks != before_marks:
                changed += 1
                lines.append(f"{name} bar {number} marks: old {show((before_marks - marks).elements())}; "
                             f"new {show((marks - before_marks).elements())}")
    lines += [f"{name}: only in the old score" for name in old if name not in new]
    lines.append(f"{changed} difference(s)")
    return lines


def main(argv: list[str]) -> int:
    assume = None
    if "--time" in argv:
        at = argv.index("--time")
        try:
            assume = parse_time(argv[at + 1])
        except (IndexError, ValueError):
            print(__doc__, file=sys.stderr)
            return 2
        argv = argv[:at] + argv[at + 2:]
    if len(argv) < 2 or argv[0] not in ("check", "names", "diff", "extract"):
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == "extract":
        if len(argv) < 4:
            print(__doc__, file=sys.stderr)
            return 2
        try:
            save(extract(load(argv[1]), argv[3:]), argv[2], like=argv[1])
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        print(f"{argv[2]}: {', '.join(name for _, name in part_names(load(argv[2])))}")
        return 0
    if argv[0] == "diff":
        if len(argv) != 3:
            print(__doc__, file=sys.stderr)
            return 2
        print("\n".join(diff(argv[1], argv[2])))
        return 0
    if argv[0] == "names":
        if len(argv) != 2:
            print(__doc__, file=sys.stderr)
            return 2
        for pid, name in part_names(load(argv[1])):
            print(f"{pid} | {name}")
        return 0
    for path in argv[1:]:
        print("\n".join(check(path, assume)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
