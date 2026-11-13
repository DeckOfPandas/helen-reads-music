#!/usr/bin/env python3
"""Build a score from a percussion transcription made by eye.

USAGE (from the repo root):
    python3 tools/omr/transcribed.py library/<piece>/transcription.json <out.musicxml|.mxl>

A poor scan reads badly, and a drum or percussion part worst: there is no
drawing to check against, and Audiveris reads few x heads or slashes. Such
parts are transcribed bar by bar into a JSON file, and this writes the
score from it, with nothing from Audiveris. Then it runs score.py check.

    {"title": "...", "bars": 200, "time": {"1": "2/2"},
     "tempo": {"1": [2, 98, "Buoyant Calypso Beat"]},   (beat in crotchets, per minute, words)
     "parts": [{"name": "Drums", "abbreviation": "Dr.",
                "kit": {"bd": ["F4", 36], "hh": ["xG5", 42], "sl": ["sB4"]},
                "pitched": {"75": ["Timpani", 48]},      (sound of p-notes from that bar: name, GM program 1-128)
                "clef": {"75": "bass", "80": "percussion"},
                "grooves": {"A": {"up": "hh:1 ...", "down": "bd:1 ..."}},
                "bars": {"9-11": "@A", "12": "hh:1 r:3", "13": {"up": "...", "down": "...", "up2": "..."}},
                "marks": {"9": ["0 mf", "0 Lite time on Hat"]}}]}

A voice is repair.parse_groove_voice's syntax, with the kit's names in
place of positions: "hh" becomes its position (and flags) and sounds as
its General MIDI drum note; a name with no sound ("sl", a slash) is
silent. A string bar is one voice (up); "@A" is a groove. A bar with no
entry is a whole-bar rest. Marks are repair's: "<crotchets in> <text>".
"""
from __future__ import annotations

import json
import math
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import repair
import score

CLEFS = {"percussion": ("percussion", None), "treble": ("G", "2"), "bass": ("F", "4")}
BEAT_UNITS = {Fraction(4): "whole", Fraction(2): "half", Fraction(1): "quarter", Fraction(1, 2): "eighth"}


def _bar_range(key: str) -> range:
    first, _, last = key.partition("-")
    return range(int(first), int(last or first) + 1)


def _by_bar(table: dict) -> dict[int, object]:
    """{"9-11": x} -> {9: x, 10: x, 11: x}; keys starting with _ are notes."""
    out: dict[int, object] = {}
    for key, value in (table or {}).items():
        if str(key).startswith("_"):
            continue
        for bar in _bar_range(str(key)):
            if bar in out:
                raise ValueError(f"bar {bar} is given twice")
            out[bar] = value
    return out


def _in_force(table: dict, bar: int):
    keys = [int(k) for k in table if not str(k).startswith("_") and int(k) <= bar]
    return table[str(max(keys))] if keys else None


def expand_voice(text: str, kit: dict) -> list[repair.DrumEvent]:
    """Parse one voice, putting each kit name's position in its place and
    giving the head its sound."""
    tokens, sounds = [], []
    for token in text.split():
        heads_text, sep, length = token.rpartition(":")
        if heads_text == "r" or not sep:
            tokens.append(token)
            sounds.append([])
            continue
        heads, token_sounds = [], []
        for head in heads_text.split("+"):
            tie = "~" if head.startswith("~") else ""
            name = head[len(tie):]
            if name in kit:
                position, *sound = kit[name]
                heads.append(tie + position)
                token_sounds.append(sound[0] if sound else None)
            else:
                heads.append(head)
                token_sounds.append(None)
        tokens.append("+".join(heads) + ":" + length)
        sounds.append(token_sounds)
    events = repair.parse_groove_voice(" ".join(tokens))
    for event, token_sounds in zip(events, sounds):
        for head, sound in zip(event.heads, token_sounds):
            head.sound = sound
    return events


def _bar_voices(spec, grooves: dict, kit: dict) -> list[list[repair.DrumEvent]]:
    if isinstance(spec, str) and spec.startswith("@"):
        if spec[1:] not in grooves:
            raise ValueError(f"no groove {spec[1:]!r}")
        spec = grooves[spec[1:]]
    if isinstance(spec, str):
        spec = {"up": spec}
    unknown = set(spec) - {key for key, _, _ in repair.DRUM_VOICES}
    if unknown:
        raise ValueError(f"unknown voice(s) {sorted(unknown)}")
    return [expand_voice(spec.get(key, ""), kit) for key, _, _ in repair.DRUM_VOICES]


def _bar_length(time: str) -> Fraction:
    beats, beat_type = time.split("/")
    return Fraction(int(beats) * 4, int(beat_type))


def _clef(name: str) -> ET.Element:
    if name not in CLEFS:
        raise ValueError(f"clef {name!r} is not one of {', '.join(CLEFS)}")
    sign, line = CLEFS[name]
    clef = ET.Element("clef")
    ET.SubElement(clef, "sign").text = sign
    if line:
        ET.SubElement(clef, "line").text = line
    return clef


def _time(text: str) -> ET.Element:
    beats, beat_type = text.split("/")
    time = ET.Element("time")
    ET.SubElement(time, "beats").text = beats
    ET.SubElement(time, "beat-type").text = beat_type
    return time


def _tempo(beat: Fraction, per_minute: int, words: str) -> ET.Element:
    direction = ET.Element("direction", {"placement": "above"})
    if words:
        ET.SubElement(ET.SubElement(direction, "direction-type"), "words").text = words
    metronome = ET.SubElement(ET.SubElement(direction, "direction-type"), "metronome")
    ET.SubElement(metronome, "beat-unit").text = BEAT_UNITS[beat]
    ET.SubElement(metronome, "per-minute").text = str(per_minute)
    ET.SubElement(direction, "sound", {"tempo": str(float(per_minute * beat)).removesuffix(".0")})
    return direction


def build(spec: dict) -> ET.Element:
    bars = int(spec["bars"])
    times = {k: v for k, v in (spec.get("time") or {"1": "4/4"}).items() if not k.startswith("_")}
    if "1" not in times:
        raise ValueError('"time" needs bar 1')
    parts = []
    for index, part_spec in enumerate(spec["parts"], 1):
        kit = part_spec.get("kit") or {}
        grooves = part_spec.get("grooves") or {}
        given = _by_bar(part_spec.get("bars"))
        late = [b for b in given if not 1 <= b <= bars]
        if late:
            raise ValueError(f"{part_spec['name']}: bars {late} are outside 1-{bars}")
        voices = {}
        for bar, bar_spec in given.items():
            try:
                voices[bar] = _bar_voices(bar_spec, grooves, kit)
            except ValueError as error:
                raise ValueError(f"{part_spec['name']} bar {bar}: {error}") from None
            expected = _bar_length(_in_force(times, bar))
            for (label, _, _), events in zip(repair.DRUM_VOICES, voices[bar]):
                if events and repair._span(events) != expected:
                    raise ValueError(f"{part_spec['name']} bar {bar} {label} lasts "
                                     f"{repair._span(events)} crotchets, the bar {expected}")
        parts.append((f"P{index}", part_spec, voices))

    lengths = [e.length for _, _, voices in parts for vs in voices.values() for v in vs for e in v]
    lengths += [_bar_length(t) for t in times.values()]
    divisions = Fraction(math.lcm(*(length.denominator for length in lengths)))

    root = ET.Element("score-partwise", {"version": score.MUSICXML_VERSION})
    if spec.get("title"):
        ET.SubElement(ET.SubElement(root, "work"), "work-title").text = spec["title"]
    part_list = ET.SubElement(root, "part-list")
    for pid, part_spec, voices in parts:
        score_part = ET.SubElement(part_list, "score-part", {"id": pid})
        ET.SubElement(score_part, "part-name").text = part_spec["name"]
        if part_spec.get("abbreviation"):
            ET.SubElement(score_part, "part-abbreviation").text = part_spec["abbreviation"]
        sounds = sorted({h.sound for vs in voices.values() for v in vs for e in v for h in e.heads
                         if h.sound is not None})
        pitched = [(bar, name, program) for bar, (name, program) in
                   sorted((int(b), v) for b, v in (part_spec.get("pitched") or {}).items())]
        for sound in sounds:
            instrument = ET.SubElement(score_part, "score-instrument", {"id": f"{pid}-I{sound}"})
            ET.SubElement(instrument, "instrument-name").text = repair.GM_DRUMS.get(sound, f"Drum {sound}")
        for _, name, program in pitched:
            instrument = ET.SubElement(score_part, "score-instrument", {"id": f"{pid}-P{program}"})
            ET.SubElement(instrument, "instrument-name").text = name
        for sound in sounds:
            midi = ET.SubElement(score_part, "midi-instrument", {"id": f"{pid}-I{sound}"})
            ET.SubElement(midi, "midi-channel").text = "10"
            ET.SubElement(midi, "midi-unpitched").text = str(sound + 1)
        for channel, (_, _, program) in enumerate(pitched, 1):
            midi = ET.SubElement(score_part, "midi-instrument", {"id": f"{pid}-P{program}"})
            ET.SubElement(midi, "midi-channel").text = str(channel)
            ET.SubElement(midi, "midi-program").text = str(program)

    for number_of_part, (pid, part_spec, voices) in enumerate(parts):
        part = ET.SubElement(root, "part", {"id": pid})
        clefs = {int(k): v for k, v in (part_spec.get("clef") or {}).items() if not k.startswith("_")}
        marks = _by_bar(part_spec.get("marks"))
        pitched = {int(k): v for k, v in (part_spec.get("pitched") or {}).items() if not k.startswith("_")}
        instruments = {sound: f"{pid}-I{sound}" for sound in range(128)}
        for bar in range(1, bars + 1):
            measure = ET.SubElement(part, "measure", {"number": str(bar)})
            attributes = ET.Element("attributes")
            if bar == 1:
                ET.SubElement(attributes, "divisions").text = str(divisions)
                ET.SubElement(ET.SubElement(attributes, "key"), "fifths").text = "0"
            if str(bar) in times:
                attributes.append(_time(times[str(bar)]))
            if bar == 1 or bar in clefs:
                attributes.append(_clef(clefs.get(bar, "percussion")))
            if len(attributes):
                measure.append(attributes)
            if number_of_part == 0 and str(bar) in (spec.get("tempo") or {}):
                beat, per_minute, *words = spec["tempo"][str(bar)]
                measure.append(_tempo(Fraction(beat), per_minute, words[0] if words else ""))
            length = _bar_length(_in_force(times, bar))
            if bar in voices and any(voices[bar]):
                up, down, up2 = voices[bar]
                repair._write_drum_voices(measure, up, down, divisions, up2, instruments)
            else:
                note = ET.SubElement(measure, "note")
                ET.SubElement(note, "rest", {"measure": "yes"})
                ET.SubElement(note, "duration").text = str(int(length * divisions))
                ET.SubElement(note, "voice").text = "1"
            in_force = _in_force({str(k): v for k, v in pitched.items()}, bar)
            if in_force:
                for note in measure.iter("note"):
                    if note.find("pitch") is not None:
                        repair._place(note, ET.Element("instrument", {"id": f"{pid}-P{in_force[1]}"}))
            for text in marks.get(bar, []):
                offset, words = repair._parse_mark(text)
                repair._place_mark(measure, offset, words, divisions)
        barline = ET.SubElement(part[-1], "barline", {"location": "right"})
        ET.SubElement(barline, "bar-style").text = "light-heavy"
    return root


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    spec = json.loads(Path(argv[0]).read_text())
    try:
        root = build(spec)
    except ValueError as error:
        print(f"transcription error: {error}", file=sys.stderr)
        return 1
    score.save(root, argv[1])
    print(f"wrote {argv[1]}")
    problems = score.check(argv[1])
    for line in problems:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
