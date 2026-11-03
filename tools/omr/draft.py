#!/usr/bin/env python3
"""Draft library/<piece>/merge.json from Audiveris's read of the piece.

USAGE (from the repo root):
    python3 tools/omr/draft.py library/<piece>      # writes omr/merge.draft.json

Audiveris makes one instrument into many parts (fragments): one per page or
per name it read, the names garbled by OCR ("Basm n m", "Vln.I", "V1r1.II")
or missing (it calls an unnamed stave "Voice"). The draft works out which
fragment is which instrument:

1. Pages: the runs of bars over which the set of printed staves holds
   (Audiveris marks a fragment's staves unprinted outside its pages).
   Within a page, fragments are in score order, top to bottom.
2. The page with the most fragments sets the instruments and their order.
3. Each fragment's name is read against a table of instruments and their
   abbreviations, forgiving OCR ("1" for "l", "Bl;" for "Bb"); "Voice" is
   weak evidence, as Audiveris gives that name to staves it could not read.
4. Every page's fragments are aligned, in order, to the instruments
   (a page may hide some), by name, clef and number of staves.
5. Each instrument is named by the fragments aligned to it, a vote across
   every page; a fragment whose own name disagrees, or that has no usable
   name, is placed by position and listed as uncertain.

It also proposes: "transpose" from the names ("Clarinet in Bb", "Horn in
F", piccolo, basses); "time" from the first time signature read;
"octave_clefs": false when treble clefs read as "8 below" turn up in parts
that never use them; and "key_changes" where the transposing parts change
key together and the rest got none (Audiveris read the change but its
export lost it). Everything uncertain is in the draft's "_check" list.
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import repair
import score

# Full name, abbreviations, usual clef signs (staff 1), score order: woodwind,
# brass, percussion, keyboards and harp, guitars, voices, strings.
INSTRUMENTS = (
    ("Piccolo", ("Picc.", "Pic."), "G"), ("Flute", ("Fl.",), "G"), ("Oboe", ("Ob.",), "G"),
    ("Cor Anglais", ("C. A.", "E. H."), "G"), ("Clarinet", ("Cl.",), "G"), ("Bass Clarinet", ("B. Cl.",), "GF"),
    ("Bassoon", ("Bsn.", "Bn."), "FC"), ("Alto Saxophone", ("A. Sax.", "Alto Sax."), "G"),
    ("Tenor Saxophone", ("T. Sax.", "Ten. Sax."), "G"), ("Baritone Saxophone", ("Bar. Sax.",), "G"),
    ("Horn", ("Hn.",), "GF"), ("Trumpet", ("Tpt.", "Tp."), "G"), ("Trombone", ("Tbn.", "Trb."), "FC"),
    ("Bass Trombone", ("B. Tbn.",), "F"), ("Tuba", ("Tba.",), "F"), ("Timpani", ("Timp.",), "F"),
    ("Drum Set", ("Dr.", "D. S.", "Drs.", "Drums", "Kit"), "percussion"), ("Percussion", ("Perc.",), "percussion"),
    ("Glockenspiel", ("Glock.",), "G"), ("Xylophone", ("Xyl.",), "G"), ("Vibraphone", ("Vib.",), "G"),
    ("Marimba", ("Mar.",), "GF"), ("Harp", ("Hp.",), "GF"), ("Piano", ("Pno.", "Pf."), "GF"),
    ("Keyboard", ("Kbd.", "Keys"), "GF"), ("Synthesizer", ("Synth.",), "GF"), ("Guitar", ("Gtr.",), "G"),
    ("Electric Guitar", ("E. Gtr.",), "G"), ("Electric Bass", ("E. Bass", "Bass Gtr."), "F"),
    ("Bass Guitar", ("B. Gtr.",), "F"), ("Voice", ("Vox",), "GF"), ("Soprano", ("Sop.",), "G"),
    ("Alto", ("Alt.",), "G"), ("Tenor", ("Ten.",), "G"), ("Baritone", ("Bar.",), "F"),
    ("Violin", ("Vln.", "Vn.", "Vl."), "G"), ("Violins", ("Vlns.",), "G"), ("Viola", ("Vla.", "Va."), "C"),
    ("Violoncello", ("Vc.", "Vlc.", "Cello"), "FC"), ("Contrabass", ("Cb.", "D.B.", "Db.", "Double Bass"), "F"),
)
SURE = 0.75               # a name read this well is the instrument
WEAK = 0.35               # how much a bare "Voice" counts: Audiveris's name for an unread stave
# Written-pitch octave transpositions by instrument; "in X" names give the rest.
OCTAVE_TRANSPOSING = {"Piccolo": "8va", "Contrabass": "8vb", "Electric Bass": "8vb", "Bass Guitar": "8vb",
                      "Guitar": "8vb", "Electric Guitar": "8vb", "Glockenspiel": "8va", "Xylophone": "8va"}
OCTAVE_CLEF_USERS = {"Guitar", "Electric Guitar", "Tenor", "Voice"}
FLAT_MARKS = ("b", "l", "L", ";", "1", "♭", "h")


@dataclass
class Identity:
    instrument: str | None
    numeral: str | None = None
    transposition: str | None = None       # "Bb", "F", "A", "Eb"
    score: float = 0.0                     # how well the name was read, 0..1

    def label(self) -> str | None:
        if self.instrument is None:
            return None
        name = self.instrument
        if self.transposition:
            name += f" in {self.transposition}"
        if self.numeral:
            name += f" {self.numeral}"
        return name


def _letters(text: str) -> str:
    """Lower-case letters only; OCR's usual stand-ins for l and o put back
    (a capital I in a name is an l: "CI." is "Cl."; numerals are already off)."""
    text = text.replace("1", "l").replace("I", "l").replace("0", "o").replace("|", "l")
    return re.sub(r"[^a-z]", "", text.lower())


def identify(name: str) -> Identity:
    """What instrument an OCR'd part name most likely is."""
    raw = name.strip()
    numeral = None
    found = re.search(r"(?<=[\s.])(I{1,3}|IV|[1-4])$", raw)
    if found:
        numeral = {"1": "I", "2": "II", "3": "III", "4": "IV"}.get(found.group(1), found.group(1))
        raw = raw[:found.start()].rstrip(" .") + ("." if raw[found.start() - 1] == "." else "")
    transposition = None
    found = re.search(r"\bin\s+([A-G])\s*(\S*)", raw)
    if found:
        letter, mark = found.group(1), found.group(2)
        flat = bool(mark) and mark[0] in FLAT_MARKS
        transposition = letter + ("b" if flat or letter == "B" else "")
        raw = raw[:found.start()].strip()
    if raw == "Voice":
        return Identity("Voice", numeral, transposition, WEAK)
    core = _letters(raw)
    if len(core) < 2:                                   # "I", "l": a stray mark, not a name
        return Identity(None, numeral, transposition, 0.0)
    best = (0.0, None)
    for full, abbreviations, _ in INSTRUMENTS:
        candidates = [SequenceMatcher(None, core, _letters(full)).ratio()]
        for abbreviation in abbreviations:
            short = _letters(abbreviation)
            candidates.append(1.0 if core == short else SequenceMatcher(None, core, short).ratio() * 0.9)
        if len(core) >= 3 and _letters(full).startswith(core[:3]):
            candidates.append(0.7)
        score_ = max(candidates)
        if score_ > best[0]:
            best = (score_, full)
    if best[0] < 0.55:
        return Identity(None, numeral, transposition, best[0])
    return Identity(best[1], numeral, transposition, best[0])


@dataclass
class Fragment:
    pid: str
    name: str
    index: int                     # in the part list
    staves: int
    clef: str                      # first clef sign of staff 1
    bars: set[str]                 # bars where its staves are printed
    identity: Identity = field(default_factory=lambda: Identity(None))
    pitch: float | None = None     # median MIDI pitch of its notes

    @property
    def sure(self) -> bool:
        return self.identity.instrument is not None and self.identity.score >= SURE


def fragments(root: ET.Element) -> list[Fragment]:
    out = []
    for index, ((pid, name), part) in enumerate(zip(score.part_names(root), root.findall("part"))):
        state, bars = "yes", set()
        for measure in part.findall("measure"):
            for details in measure.iter("staff-details"):
                state = details.get("print-object") or state
            if state == "yes":
                bars.add(measure.get("number"))
        staves = max((int(a.findtext("staves")) for a in part.iter("attributes") if a.findtext("staves")), default=1)
        clef = next((c.findtext("sign") or "" for c in part.iter("clef") if c.get("number", "1") == "1"), "")
        pitches = sorted(score.midi(n) for n in part.iter("note") if n.find("pitch") is not None)
        pitch = float(pitches[len(pitches) // 2]) if pitches else None
        out.append(Fragment(pid, name, index, staves, clef, bars, identify(name), pitch))
    return out


def pages(root: ET.Element, frags: list[Fragment]) -> list[tuple[list[str], list[Fragment]]]:
    """Runs of bars with the same printed fragments, each with those fragments in score order."""
    numbers = [m.get("number") for m in root.find("part").findall("measure")]
    runs: list[tuple[list[str], list[Fragment]]] = []
    previous = None
    for number in numbers:
        here = [f for f in frags if number in f.bars]
        key = tuple(f.pid for f in here)
        if key != previous:
            runs.append(([number], here))
            previous = key
        else:
            runs[-1][0].append(number)
    return [run for run in runs if run[1]]


@dataclass
class Slot:
    name: str | None               # the instrument, where the fullest page's name is sure
    clef: str
    staves: int
    pitch: float | None = None     # median pitch of the sure fragments placed here


def _cost(fragment: Fragment, slot: Slot) -> float:
    if not fragment.sure or slot.name is None:
        name_cost = 0.5
        if fragment.pitch is not None and slot.pitch is not None:   # no usable name: the notes say
            name_cost += min(abs(fragment.pitch - slot.pitch) / 24, 0.5)
    else:
        name_cost = 0.0 if fragment.identity.instrument == slot.name else 2.0
    clef_cost = 0.0 if fragment.clef == slot.clef else 0.75
    staves_cost = 0.0 if fragment.staves == slot.staves else 3.0
    return name_cost + clef_cost + staves_cost


def _align(page: list[Fragment], slots: list[Slot]) -> list[int] | None:
    """Each fragment's slot, in order, skipping slots the page hides."""
    n, m = len(page), len(slots)
    if n > m:
        return None
    inf = float("inf")
    best = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        best[0][j] = 0.0
    for i in range(1, n + 1):
        for j in range(i, m + 1):
            skip = best[i][j - 1]
            take = best[i - 1][j - 1] + _cost(page[i - 1], slots[j - 1])
            if take <= skip:
                best[i][j], back[i][j] = take, (i - 1, j - 1)
            else:
                best[i][j], back[i][j] = skip, (i, j - 1)
    result = [0] * n
    i, j = n, m
    while i > 0:
        step = back[i][j]
        if step == (i - 1, j - 1):
            result[i - 1] = j - 1
        i, j = step
    return result


def _first_time(root: ET.Element) -> str | None:
    for time in root.iter("time"):
        beats, beat_type = time.findtext("beats"), time.findtext("beat-type")
        if beats and beat_type:
            return f"{beats}/{beat_type}"
    return None


def _lost_key_changes(root: ET.Element, slot_of: dict[str, int], names: list[str],
                      transpose: dict[str, str]) -> dict[str, int]:
    """Bars where every transposing part that changes key changes to one
    concert key, and the parts at concert pitch got no key at all."""
    keys: dict[str, dict[str, int]] = {}
    for part in root.findall("part"):
        found = {}
        for measure in part.findall("measure"):
            for key in measure.iter("key"):
                text = key.findtext("fifths")
                if text not in (None, "null"):
                    found[measure.get("number")] = int(text)
        keys[part.get("id")] = found
    changes: dict[str, int] = {}
    latest = None
    numbers = [m.get("number") for m in root.find("part").findall("measure")]
    for position, number in enumerate(numbers[1:], start=1):
        concert, plain_silent, plain_count = set(), True, 0
        for pid, found in keys.items():
            name = names[slot_of[pid]] if pid in slot_of else None
            kind = transpose.get(name) if name else None
            before = [found[b] for b in numbers[:position] if b in found]
            if kind in ("Bb", "A", "F", "Eb"):
                if number in found and before and found[number] != before[-1]:
                    concert.add(found[number] - repair.TRANSPOSITIONS[kind][1])
            elif name is not None:
                plain_count += 1
                if number in found:
                    plain_silent = False
        if len(concert) == 1 and plain_silent and plain_count:
            key = concert.pop()
            if key != latest:                   # a new page's fragment restating it is no change
                changes[number] = key
                latest = key
    return changes


def draft(root: ET.Element) -> tuple[dict, list[str]]:
    frags = fragments(root)
    runs = pages(root, frags)
    if not runs:
        return {}, ["no printed staves found"]
    bars, reference = max(runs, key=lambda run: len(run[1]))
    slots = [Slot(f.identity.instrument if f.sure else None, f.clef, f.staves) for f in reference]
    slot_of: dict[str, int] = {}
    unaligned: list[str] = []
    # Twice: the first placing gives each slot the pitch of its surely named
    # fragments, which places the unnamed ones better the second time.
    for final in (False, True):
        slot_of, unaligned = {}, []
        for run_bars, page in runs:
            placed = _align(page, slots)
            if placed is None:
                unaligned.append(f"bars {run_bars[0]}-{run_bars[-1]}: more staves than the fullest page")
                continue
            for fragment, slot in zip(page, placed):
                if fragment.pid in slot_of and slot_of[fragment.pid] != slot:
                    unaligned.append(f"{fragment.pid} {fragment.name!r} sits in two places on different pages")
                slot_of.setdefault(fragment.pid, slot)
        if not final:
            for index, slot in enumerate(slots):
                pitches = sorted(f.pitch for f in frags if slot_of.get(f.pid) == index and f.sure
                                 and f.pitch is not None)
                slot.pitch = pitches[len(pitches) // 2] if pitches else None

    # Name each slot by a vote of its fragments.
    by_slot: dict[int, list[Fragment]] = defaultdict(list)
    for fragment in frags:
        if fragment.pid in slot_of:
            by_slot[slot_of[fragment.pid]].append(fragment)
    names: list[str] = []
    for slot in range(len(reference)):
        votes: Counter = Counter()
        detail: dict[str, Identity] = {}
        for fragment in by_slot[slot]:
            identity = fragment.identity
            if identity.instrument is None:
                continue
            weight = identity.score * max(len(fragment.bars), 1)
            votes[identity.instrument] += weight
            best = detail.get(identity.instrument)
            if best is None or (identity.transposition and not best.transposition) or \
                    (identity.numeral and not best.numeral):
                detail[identity.instrument] = identity
        if votes:
            winner = votes.most_common(1)[0][0]
            names.append(detail[winner].label() or winner)
        else:
            names.append(f"Unknown {slot + 1}")
    for name, count in Counter(names).items():
        if count > 1:
            numbered = 0
            for i, other in enumerate(names):
                if other == name:
                    numbered += 1
                    names[i] = f"{name} {'I' * numbered if numbered <= 3 else numbered}"

    # Aliases: by name where a name always means one instrument, else by id.
    check: list[str] = list(unaligned)
    aliases: dict[str, str | None] = {}
    by_name: dict[str, set[int]] = defaultdict(set)
    for fragment in frags:
        if fragment.pid in slot_of:
            by_name[fragment.name].add(slot_of[fragment.pid])
    for fragment in frags:
        if fragment.pid not in slot_of:
            aliases[fragment.pid] = None
            check.append(f"{fragment.pid} {fragment.name!r}: not placed; left as its own part")
            continue
        target = names[slot_of[fragment.pid]]
        if len(by_name[fragment.name]) == 1:
            aliases[fragment.name] = target
        else:
            aliases[fragment.pid] = target
        identity = fragment.identity
        if identity.instrument is None or not target.startswith(identity.instrument):
            pages_of = sorted({b for run_bars, page in runs if fragment in page for b in run_bars[:1]}, key=int)
            check.append(f"{fragment.pid} {fragment.name!r} (bars from {', '.join(pages_of)}): "
                         f"{target}, by its place on the page" +
                         (f"; its name reads as {identity.label()}" if identity.instrument else ""))

    # Overlaps: two fragments of one instrument printed in the same bar.
    for slot, members in by_slot.items():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                shared = a.bars & b.bars
                if shared:
                    check.append(f"{names[slot]}: {a.pid} and {b.pid} are both printed in "
                                 f"{len(shared)} bar(s), from bar {min(shared, key=int)}")

    transpose = {}
    for name in names:
        found = re.search(r" in (Bb|A|F|Eb)\b", name)
        if found:
            transpose[name] = found.group(1)
        else:
            base = re.sub(r" (I{1,3}|IV)$", "", name)
            if base in OCTAVE_TRANSPOSING:
                transpose[name] = OCTAVE_TRANSPOSING[base]

    table: dict = {"_drafted": "by draft.py from the read; check the _check list, then save as "
                               "library/<piece>/merge.json"}
    time = _first_time(root)
    if time:
        table["time"] = time
    else:
        check.append("no time signature read: add \"time\"")
    octave_users = set()
    for fragment, part in zip(frags, root.findall("part")):
        if any(c.find("clef-octave-change") is not None for c in part.iter("clef")):
            slot = slot_of.get(fragment.pid)
            octave_users.add(names[slot] if slot is not None else fragment.name)
    strangers = sorted(n for n in octave_users if re.sub(r" (I{1,3}|IV)$", "", n) not in OCTAVE_CLEF_USERS)
    if strangers:
        table["octave_clefs"] = False
        check.append(f"treble clefs read as '8 below' in {', '.join(strangers)}: octave_clefs set false "
                     "(undoes them); remove it if the score really has octave clefs")
    if transpose:
        table["transpose"] = transpose
    changes = _lost_key_changes(root, slot_of, names, transpose)
    if changes:
        table["key_changes"] = changes
        check.append(f"key changes the transposing parts show and the rest lost: "
                     f"{', '.join(f'bar {b} to {k:+d} fifths' for b, k in changes.items())}")
    table["canonical_order"] = names
    table["aliases"] = aliases
    table["_check"] = check
    report = [f"merge.json drafted: {len(names)} instruments from {len(frags)} parts over {len(runs)} page(s); "
              f"{len(check)} thing(s) to check"] + [f"  {line}" for line in check]
    return table, report


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    piece = Path(argv[0])
    sharp = piece / "omr" / "sharp" / f"{piece.name}.mxl"
    read = sharp if sharp.exists() else piece / "omr" / f"{piece.name}.mxl"
    if not read.exists():
        print(f"no read of {piece}: run process.py first")
        return 1
    table, report = draft(score.load(read))
    out = piece / "omr" / "merge.draft.json"
    out.write_text(json.dumps(table, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("\n".join(report))
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
