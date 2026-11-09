"""tools/omr/draft.py: drafting merge.json from a read split into fragments."""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

import draft  # noqa: E402
from musicxml_maker import score as make_score  # noqa: E402

CLEFS = {"G": "<clef><sign>G</sign><line>2</line></clef>", "F": "<clef><sign>F</sign><line>4</line></clef>",
         "G8": "<clef><sign>G</sign><line>2</line><clef-octave-change>-1</clef-octave-change></clef>"}


def fragment(pid: str, clef: str, shown: range, bars: int = 6, keys: dict[int, int] | None = None,
             pitch: str | None = None) -> str:
    """A part printed only in the `shown` bars, the way Audiveris writes a
    fragment: its staves marked unprinted elsewhere, whole-bar rests there;
    in its own bars a whole note of `pitch` (e.g. "C3"), or rests."""
    keys = keys or {}
    out = []
    for n in range(1, bars + 1):
        children = []
        if n == 1:
            children.append("<divisions>1</divisions>")
        if n == 1 or n in keys:
            children.append(f"<key><fifths>{keys.get(n, -2)}</fifths></key>")
        if n == 1:
            children.append(f"<time><beats>4</beats><beat-type>4</beat-type></time>{CLEFS[clef]}")
        if n in (1, shown.start, shown.stop):
            children.append(f'<staff-details print-object="{"yes" if n in shown else "no"}"/>')
        attributes = f"<attributes>{''.join(children)}</attributes>" if children else ""
        body = (f"<pitch><step>{pitch[0]}</step><octave>{pitch[-1]}</octave></pitch>"
                if pitch and n in shown else '<rest measure="yes"/>')
        out.append(f'<measure number="{n}">{attributes}<note>{body}<duration>4</duration>'
                   f"<voice>1</voice></note></measure>")
    return f'<part id="{pid}">{"".join(out)}</part>'


def read(parts: list[tuple], **keys) -> ET.Element:
    """parts: (id, name, clef, bars shown[, pitch])."""
    xml = make_score([(p[0], p[1], fragment(p[0], p[2], p[3], keys=keys.get(p[0]), pitch=p[4] if len(p) > 4 else None))
                      for p in parts])
    return ET.fromstring(xml)


PAGE1, PAGE2, PAGE3 = range(1, 3), range(3, 5), range(5, 7)


def three_pages() -> ET.Element:
    """Flute, Clarinet in Bb, Cello, Double bass over three pages; page 3
    hides the clarinet and one of the low strings, which only its notes
    tell; names as OCR and Audiveris leave them."""
    return read([
        ("P1", "Flute", "G", PAGE1), ("P2", "Clarinet in Bl;", "G", PAGE1),
        ("P3", "Violoncello", "F", PAGE1, "D3"), ("P4", "Voice", "F", PAGE1, "E2"),
        ("P5", "Fl.", "G", PAGE2), ("P6", "CI.", "G", PAGE2), ("P7", "Vc.", "F", PAGE2, "C3"),
        ("P8", "Cb.", "F", PAGE2, "G2"),
        ("P9", "Fl.", "G", PAGE3), ("P10", "Voice", "F", PAGE3, "E3"),
    ])


def test_names_are_read_through_ocr():
    assert draft.identify("Clarinet in Bl;").label() == "Clarinet in Bb"
    assert draft.identify("Trumpet in BL").label() == "Trumpet in Bb"
    assert draft.identify("Horn in F").label() == "Horn in F"
    assert draft.identify("CI.").instrument == "Clarinet"
    assert draft.identify("V1n.I").label() == "Violin I"
    assert draft.identify("Vln. II").label() == "Violin II"
    assert draft.identify("Trombt me").instrument == "Trombone"
    assert draft.identify("I").instrument is None


def test_bare_voice_is_weak_evidence():
    identity = draft.identify("Voice")
    assert identity.instrument == "Voice" and identity.score < draft.SURE


def test_instruments_and_order_from_the_fullest_page():
    table, _ = draft.draft(three_pages())
    assert table["canonical_order"] == ["Flute", "Clarinet in Bb", "Violoncello", "Contrabass"]


def test_every_fragment_is_placed():
    table, _ = draft.draft(three_pages())
    aliases = table["aliases"]
    assert aliases["Fl."] == aliases["Flute"] == "Flute"
    assert aliases["CI."] == "Clarinet in Bb"
    assert aliases["Vc."] == "Violoncello"
    # "Voice" covers the bass on page 1 and the cello on page 3: by id
    assert aliases["P4"] == "Contrabass" and aliases["P10"] == "Violoncello"


def test_placed_by_position_is_listed_to_check():
    table, _ = draft.draft(three_pages())
    check = " ".join(table["_check"])
    assert "P4 'Voice'" in check and "P10 'Voice'" in check
    assert "P5 'Fl.'" not in check


def test_transpositions_from_names():
    table, _ = draft.draft(three_pages())
    assert table["transpose"] == {"Clarinet in Bb": "Bb", "Contrabass": "8vb"}
    assert table["time"] == "4/4"


def test_octave_clefs_in_parts_that_never_use_them():
    root = read([("P1", "Flute", "G8", range(1, 7)), ("P2", "Violoncello", "F", range(1, 7))])
    table, _ = draft.draft(root)
    assert table["octave_clefs"] is False


def test_no_octave_clef_proposal_for_a_guitar():
    root = read([("P1", "Guitar", "G8", range(1, 7)), ("P2", "Violoncello", "F", range(1, 7))])
    table, _ = draft.draft(root)
    assert "octave_clefs" not in table


def test_a_key_change_only_the_transposing_parts_kept():
    # Concert Bb major to C major at bar 5: the clarinet's written key goes
    # from C to D (2 sharps); the flute's change was lost.
    root = read([("P1", "Flute", "G", range(1, 7)), ("P2", "Clarinet in Bb", "G", range(1, 7))],
                P2={1: 0, 5: 2})
    table, _ = draft.draft(root)
    assert table["key_changes"] == {"5": 0}


def test_no_key_change_when_every_part_has_it():
    root = read([("P1", "Flute", "G", range(1, 7)), ("P2", "Clarinet in Bb", "G", range(1, 7))],
                P1={1: -2, 5: 0}, P2={1: 0, 5: 2})
    table, _ = draft.draft(root)
    assert "key_changes" not in table
