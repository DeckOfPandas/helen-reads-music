"""tools/omr/score.py: reading, writing, and finding bars that do not add up."""
from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

import score  # noqa: E402
from musicxml_maker import (attributes, bar, four_crotchets, part,  # noqa: E402
                            score as make_score, whole_rest, write_mxl)


def _part(bars):
    root = score.ET.fromstring(make_score([("P1", "Flute", part("P1", bars))]))
    return root.find("part")


def test_full_bars_are_fine():
    bars = [bar(four_crotchets(), 1, attributes()), bar(whole_rest(), 2),
            bar([("C4", 2), ("r", 1), ("D4", Fraction(1, 2)), ("E4", Fraction(1, 2))], 3)]
    assert score.bad_bars(_part(bars)) == []


def test_a_bar_missing_a_note_is_found():
    bars = [bar(four_crotchets(), 1, attributes()), bar([("C4", 1)] * 3, 2), bar(four_crotchets(), 3)]
    bad = score.bad_bars(_part(bars))
    assert [(b.number, b.holds, b.expected) for b in bad] == [("2", 3, 4)]
    assert bad[0].describe() == "bar 2: 3 crotchets of 4 crotchets (short)"


def test_an_overfull_bar_is_found():
    bad = score.bad_bars(_part([bar(four_crotchets() + [("r", 1)], 1, attributes())]))
    assert [b.holds for b in bad] == [5]


def test_chords_and_grace_notes_take_no_time():
    events = [("C4", 1), ("E4+", 1), ("G4+", 1), ("D4", 1), ("E4", 1), ("F4", 1)]
    assert score.bad_bars(_part([bar(events, 1, attributes())])) == []


def test_time_signature_and_divisions_changes_are_followed():
    bars = [bar(four_crotchets(), 1, attributes()),
            bar([("C4", 1)] * 3, 2, attributes(time="3/4")),
            bar([("C4", Fraction(3, 2)), ("D4", Fraction(3, 2))], 3,
                attributes(time="6/8", divisions=2), divisions=2)]
    assert score.bad_bars(_part(bars)) == []


def test_a_pickup_bar_may_be_short_but_not_long():
    pickup = bar([("G4", 1)], 0, attributes(), implicit=True)
    assert score.bad_bars(_part([pickup, bar(four_crotchets(), 1)])) == []
    long_pickup = bar(four_crotchets() + [("G4", 1)], 0, attributes(), implicit=True)
    assert len(score.bad_bars(_part([long_pickup]))) == 1


def test_two_voices_each_must_fill_the_bar():
    full = four_crotchets("E5") + [("back", 4), ("voice", "2"), ("C4", 2), ("C4", 2)]
    assert score.bad_bars(_part([bar(full, 1, attributes())])) == []
    short = four_crotchets("E5") + [("back", 4), ("voice", "2"), ("C4", 2)]
    bad = score.bad_bars(_part([bar(short, 1, attributes())]))
    assert len(bad) == 1 and bad[0].voices == {"1": 4, "2": 2}
    assert "voice 2 2 crotchets" in bad[0].describe()


def test_check_reports_bars_and_transposition(tmp_path):
    clarinet = part("P2", [bar(four_crotchets(), 1, attributes(
        transpose="<transpose><diatonic>-1</diatonic><chromatic>-2</chromatic></transpose>"))])
    flute = part("P1", [bar(four_crotchets(), 1, attributes()), bar([("C4", 1)], 2)])
    mxl = write_mxl(tmp_path / "s.mxl", make_score([("P1", "Flute", flute), ("P2", "Clarinet", clarinet)]))
    lines = score.check(mxl)
    assert any("Flute: 2 bars, 5 notes" in l and "concert pitch" in l for l in lines)
    assert any("Clarinet" in l and "transposing" in l for l in lines)
    assert "    bar 2: 1 crotchet of 4 crotchets (short)" in lines
    assert "WARNING: parts have different bar counts: [2, 1]" in lines
    assert lines[-1] == "bars that do not add up: 1 of 3 checked"


def _untimed_attributes():
    return "<attributes><divisions>4</divisions><clef><sign>G</sign><line>2</line></clef></attributes>"


def test_bars_with_no_time_signature_are_counted_never_passed(tmp_path):
    flute = part("P1", [bar([("C4", 1)] * 3, 1, _untimed_attributes()), bar(four_crotchets(), 2)])
    root = score.ET.fromstring(make_score([("P1", "Flute", flute)]))
    assert score.judge(root.find("part")) == ([], 2)
    mxl = write_mxl(tmp_path / "s.mxl", make_score([("P1", "Flute", flute)]))
    lines = score.check(mxl)
    assert any(l.startswith("NO TIME SIGNATURE") for l in lines)
    assert "    2 bar(s) NOT CHECKED: no time signature or divisions" in lines
    assert lines[-1] == "bars that do not add up: 0 of 0 checked; 2 NOT CHECKED (no time signature)"


def test_an_assumed_time_signature_checks_untimed_bars(tmp_path):
    flute = part("P1", [bar([("C4", 1)] * 3, 1, _untimed_attributes()), bar(four_crotchets(), 2)])
    mxl = write_mxl(tmp_path / "s.mxl", make_score([("P1", "Flute", flute)]))
    lines = score.check(mxl, score.parse_time("4/4"))
    assert "    bar 1: 3 crotchets of 4 crotchets (short)" in lines
    assert lines[-1] == "bars that do not add up: 1 of 2 checked"


def test_a_printed_time_signature_wins_over_the_assumed_one():
    flute = part("P1", [bar([("C4", 1)] * 3, 1, attributes(time="3/4"))])
    root = score.ET.fromstring(make_score([("P1", "Flute", flute)]))
    assert score.judge(root.find("part"), score.parse_time("4/4")) == ([], 0)


def test_parse_time():
    assert score.parse_time("6/8") == Fraction(3)
    for bad in ("4", "x/4", "0/4", "4/0"):
        try:
            score.parse_time(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_save_round_trips_and_keeps_other_members(tmp_path):
    source = write_mxl(tmp_path / "in.mxl", make_score([("P1", "Flute", part("P1", [bar(four_crotchets(), 1, attributes())]))]))
    root = score.load(source)
    out = tmp_path / "out.mxl"
    score.save(root, out, like=source)
    assert score.part_names(score.load(out)) == [("P1", "Flute")]
    plain = tmp_path / "out.musicxml"
    score.save(root, plain)
    assert score.part_names(score.load(plain)) == [("P1", "Flute")]
    assert not list(tmp_path.glob("*.partial"))


def test_diff_lists_bars_whose_notes_or_lyrics_changed(tmp_path):
    def version(name, third_bar, lyric=""):
        bars = [bar(four_crotchets(), 1, attributes()), bar(four_crotchets(), 2), bar(third_bar, 3)]
        xml = make_score([("P1", "Flute", part("P1", bars))])
        if lyric:
            xml = xml.replace("</note>", f"<lyric><text>{lyric}</text></lyric></note>", 1)
        return write_mxl(tmp_path / name, xml)

    old = version("old.mxl", four_crotchets())
    new = version("new.mxl", [("C4", 2), ("C4", 2)], lyric="la")
    lines = score.diff(old, new)
    assert lines[0] == "Flute bar 1 marks: old -; new lyric la"
    assert lines[1].startswith("Flute bar 3 notes:")
    assert lines[2:] == ["2 difference(s)"]
    assert score.diff(old, old) == ["0 difference(s)"]


def test_cli_names(tmp_path, capsys):
    mxl = write_mxl(tmp_path / "s.mxl", make_score([("P1", "Fl.", part("P1", [])), ("P2", "Ob.", part("P2", []))]))
    assert score.main(["names", str(mxl)]) == 0
    assert capsys.readouterr().out == "P1 | Fl.\nP2 | Ob.\n"
    assert score.main(["bogus"]) == 2
