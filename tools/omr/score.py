#!/usr/bin/env python3
"""Read, check and write MusicXML (.mxl or .musicxml/.xml).

USAGE (from the repo root):
    python3 tools/omr/score.py check <score.mxl> [<score.mxl> ...] [--time 4/4]
    python3 tools/omr/score.py names <score.mxl>

check   Per part: bars, notes, rests, octave range, whether it carries a
        <transpose> (a part without one is read as concert pitch), and every
        bar whose notes and rests do not add up to its time signature. Those
        bars are where Audiveris dropped or invented a note or rest; the
        report gives the bar number, what it holds and what it should.
        A pickup bar (implicit="yes") is allowed to be short.
        Audiveris can miss a time signature altogether. A bar with none in
        force is judged against --time (read it off the printed page); with
        no --time it is reported NOT CHECKED, never passed.
names   Every part name, in score order: the raw material for a merge config.

This reads structure, not notation: it says where to look, it is not a
proofread.
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
import zipfile
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


def save(root: ET.Element, out: Path | str, like: Path | str | None = None) -> None:
    """Write `root` to `out` (.mxl or plain XML). For .mxl, every other member
    of `like` (container, images) is copied across. Written to a side file and
    renamed, so a failure never leaves half a score."""
    out = Path(out)
    xml_bytes = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
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
    for measure in part.findall("measure"):
        cursor = longest = Fraction(0)
        voices: dict[str, Fraction] = {}
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
        if expected is None or divisions is None:
            unjudged += 1
            continue
        if measure.get("implicit") == "yes" and longest <= expected:
            continue
        uneven = len(voices) > 1 and any(d != expected for d in voices.values())
        if longest != expected or uneven:
            bad.append(BadBar(measure.get("number") or "?", longest, expected, voices))
    return bad, unjudged


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
    if counts and len(set(counts)) > 1:
        lines.append(f"WARNING: parts have different bar counts: {counts}")
    total = f"bars that do not add up: {total_bad} of {sum(counts) - total_unjudged} checked"
    if total_unjudged:
        total += f"; {total_unjudged} NOT CHECKED (no time signature)"
    lines.append(total)
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
    if len(argv) < 2 or argv[0] not in ("check", "names"):
        print(__doc__, file=sys.stderr)
        return 2
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
