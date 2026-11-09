"""Generated MusicXML for the tests: no score from library/ is ever read.

A bar is a list of events. An event is ("C4", 1) for a crotchet C, ("r", 2)
for a minim rest, ("E4+", 1) for a note chorded onto the one before, or
("back", 4) / ("fwd", 2) for a <backup>/<forward>. Durations are in
crotchets; DIVISIONS ticks make one crotchet.
"""
from __future__ import annotations

import zipfile
from fractions import Fraction
from pathlib import Path

DIVISIONS = 4


def _note(event: tuple, voice: str, divisions: int) -> str:
    pitch, beats = event
    ticks = int(Fraction(beats) * divisions)
    if pitch == "back":
        return f"<backup><duration>{ticks}</duration></backup>"
    if pitch == "fwd":
        return f"<forward><duration>{ticks}</duration></forward>"
    chord = "<chord/>" if pitch.endswith("+") else ""
    pitch = pitch.rstrip("+")
    body = "<rest/>" if pitch == "r" else (
        f"<pitch><step>{pitch[0]}</step><octave>{pitch[-1]}</octave></pitch>")
    return f"<note>{chord}{body}<duration>{ticks}</duration><voice>{voice}</voice></note>"


def bar(events: list[tuple], number: int, attributes: str = "", voice: str = "1",
        implicit: bool = False, divisions: int = DIVISIONS) -> str:
    """`divisions` must match the <divisions> in force for this bar."""
    notes = []
    current = voice
    for event in events:
        if event[0] == "voice":
            current = event[1]
            continue
        notes.append(_note(event, current, divisions))
    flag = ' implicit="yes"' if implicit else ""
    return f'<measure number="{number}"{flag}>{attributes}{"".join(notes)}</measure>'


def attributes(time: str = "4/4", clef: str = "G", line: int = 2, fifths: int = 0,
               divisions: int = DIVISIONS, transpose: str = "") -> str:
    beats, beat_type = time.split("/")
    return (f"<attributes><divisions>{divisions}</divisions>"
            f"<key><fifths>{fifths}</fifths></key>"
            f"<time><beats>{beats}</beats><beat-type>{beat_type}</beat-type></time>"
            f"<clef><sign>{clef}</sign><line>{line}</line></clef>{transpose}</attributes>")


def part(pid: str, bars: list[str]) -> str:
    return f'<part id="{pid}">{"".join(bars)}</part>'


def score(parts: list[tuple[str, str, str]]) -> str:
    """parts: (id, name, <part> xml)."""
    listing = "".join(
        f'<score-part id="{pid}"><part-name>{name}</part-name>'
        f'<score-instrument id="{pid}-I1"><instrument-name>{name}</instrument-name></score-instrument>'
        f"</score-part>" for pid, name, _ in parts)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<score-partwise version="4.0"><part-list>{listing}</part-list>'
            f'{"".join(xml for _, _, xml in parts)}</score-partwise>')


def write_mxl(path: Path, xml: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container><rootfiles>'
                   '<rootfile full-path="score.xml"/></rootfiles></container>')
        z.writestr("score.xml", xml)
    return path


def four_crotchets(pitch: str = "C4") -> list[tuple]:
    return [(pitch, 1)] * 4


def whole_rest() -> list[tuple]:
    return [("r", 4)]
