"""tools/omr/process.py, with a stand-in for Audiveris that writes generated music."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

import process  # noqa: E402
import score  # noqa: E402
from musicxml_maker import (attributes, bar, four_crotchets, part,  # noqa: E402
                            score as make_score, whole_rest, write_mxl)

FRAGMENTS = make_score([
    ("P1", "Fl.", part("P1", [bar(four_crotchets(), 1, attributes()), bar(whole_rest(), 2)])),
    ("P2", "Flute", part("P2", [bar(whole_rest(), 1, attributes()), bar([("D5", 1)] * 3, 2)])),
])


class FakeAudiveris:
    """Records each call; writes FRAGMENTS (or `sharp_xml` for a re-rendered
    PDF) as <pdf stem>.mxl, unless told to fail with a log line first or the
    PDF's DPI is in `crash_at`. With `interline`, logs it the way Audiveris does."""

    def __init__(self, fail_with: list[str] | None = None, interline: int | None = None,
                 crash_at: tuple = (), sharp_xml: str | None = None):
        self.fail_with = list(fail_with or [])
        self.interline = interline
        self.crash_at = {str(d) for d in crash_at}
        self.sharp_xml = sharp_xml
        self.calls: list[list[str]] = []

    def __call__(self, args, log=None):
        self.calls.append(args)
        out, pdf = Path(args[args.index("-output") + 1]), Path(args[-1])
        if self.interline:
            log.write(f"INFO ScaleStep 65 | Scale{{ interline({self.interline},{self.interline},13) line(2,2,2) beam(7)}}\n")
        if self.fail_with:
            log.write(self.fail_with.pop(0) + "\n")
            return 0
        dpi = pdf.stem.rsplit("-", 1)[-1]
        if dpi in self.crash_at:
            log.write("java.lang.NullPointerException\n")
            return 1
        sharp = dpi.isdigit()
        write_mxl(out / f"{pdf.stem}.mxl", self.sharp_xml if sharp and self.sharp_xml else FRAGMENTS)
        return 0


def piece_with_pdfs(tmp_path: Path, name: str = "under-the-sea", count: int = 2) -> Path:
    piece = tmp_path / "library" / name
    piece.mkdir(parents=True)
    for n in range(count):
        with pymupdf.open() as doc:
            doc.new_page().insert_text((72, 72), f"page {n}")
            doc.save(piece / f"3score_{n}.pdf")
    return piece


def test_a_pdf_piece_is_read_and_reported(tmp_path):
    piece = piece_with_pdfs(tmp_path)
    fake = FakeAudiveris()
    ok, notes = process.process(piece, run=fake)
    assert ok, notes
    out = piece / "omr"
    pdf = Path(fake.calls[0][-1])
    assert pdf == out / "work" / "under-the-sea.pdf"
    with pymupdf.open(pdf) as doc:
        assert len(doc) == 2
    assert fake.calls[0][:5] == ["-batch", "-export", "-save", "-output", str(out)]
    report = (out / "report.txt").read_text()
    assert "bar 2: 3 crotchets of 4 crotchets (short)" in report
    assert (out / "names.txt").read_text() == "P1 | Fl.\nP2 | Flute\n"
    drafted = json.loads((out / "merge.draft.json").read_text())
    assert drafted["aliases"] == {"Fl.": "Flute I", "Flute": "Flute II"}      # two flute staves, both printed
    assert "2 parts; bars that do not add up: 1 of 4 checked" in notes
    assert "merge.draft.json" in notes[-1]


def test_a_low_interline_is_re_rendered_and_read_again(tmp_path):
    piece = piece_with_pdfs(tmp_path, count=1)
    fake = FakeAudiveris(fail_with=["Sheet#1 too low interline value of 10 pixels"])
    ok, notes = process.process(piece, run=fake)
    assert ok, notes
    assert len(fake.calls) == 2 and fake.calls[1][-1].endswith("under-the-sea-fixed.pdf")
    assert any("re-rendering at up to 600 DPI" in n for n in notes)
    assert (piece / "omr" / "under-the-sea.mxl").is_file()


CRASH = "WARN  [under-the-sea#2]                      Book 2044 | Error processing stub java.lang.RuntimeException"


def test_a_page_audiveris_crashes_on_is_drawn_smaller_and_read_again(tmp_path):
    piece = piece_with_pdfs(tmp_path)
    fake = FakeAudiveris(fail_with=[CRASH])
    ok, notes = process.process(piece, run=fake)
    assert ok, notes
    retry = Path(fake.calls[1][-1])
    assert retry.name == "under-the-sea-retry.pdf"
    with pymupdf.open(retry) as doc:
        first, second = doc[0].rect, doc[1].rect
    assert round(second.width / first.width, 2) == 0.93
    assert "Audiveris crashed on page(s) 2; reading again with page 2 at 279 DPI" in notes
    assert (piece / "omr" / "under-the-sea.mxl").is_file()


def test_a_page_that_always_crashes_is_named(tmp_path):
    piece = piece_with_pdfs(tmp_path)
    fake = FakeAudiveris(fail_with=[CRASH] * 5)
    ok, notes = process.process(piece, run=fake)
    assert not ok and len(fake.calls) == 1 + process.CRASH_RETRIES
    assert any(n.startswith("Audiveris still crashes on page(s) 2") for n in notes)
    assert "page 2 at 241 DPI" in notes[-3]


def test_nothing_read_is_a_failure_that_names_the_log(tmp_path):
    piece = piece_with_pdfs(tmp_path, count=1)
    ok, notes = process.process(piece, run=FakeAudiveris(fail_with=["boom", "boom"]))
    assert not ok and "audiveris.log" in notes[-1]


def test_a_stale_score_is_not_mistaken_for_a_new_one(tmp_path):
    piece = piece_with_pdfs(tmp_path, count=1)
    assert process.process(piece, run=FakeAudiveris())[0]
    ok, _ = process.process(piece, force=True, run=FakeAudiveris(fail_with=["boom", "boom"]))
    assert not ok


def test_an_existing_read_is_kept_unless_forced(tmp_path):
    piece = piece_with_pdfs(tmp_path, count=1)
    process.process(piece, run=FakeAudiveris())
    again = FakeAudiveris()
    ok, notes = process.process(piece, run=again)
    assert ok and again.calls == [] and notes[0].startswith("already read")
    process.process(piece, force=True, run=again)
    assert len(again.calls) == 1


def test_merge_json_is_applied_and_checked(tmp_path):
    piece = piece_with_pdfs(tmp_path, count=1)
    (piece / "merge.json").write_text(json.dumps(
        {"canonical_order": ["Flute"], "aliases": {"Fl.": "Flute", "Flute": "Flute"}}))
    ok, notes = process.process(piece, run=FakeAudiveris())
    assert ok, notes
    merged = piece / "omr" / "under-the-sea-merged.mxl"
    assert [n for _, n in score.part_names(score.load(merged))] == ["Flute"]
    plain = merged.with_suffix(".musicxml")
    assert plain.read_text(encoding="utf-8").splitlines()[1].startswith(
        '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN"')
    assert [n for _, n in score.part_names(score.load(plain))] == ["Flute"]
    assert "conserved" in (piece / "omr" / "merge-report.txt").read_text()
    assert any("merged and repaired with merge.json" in n for n in notes)


def test_merge_json_time_and_transposition_are_applied(tmp_path):
    piece = piece_with_pdfs(tmp_path, count=1)
    (piece / "merge.json").write_text(json.dumps({
        "canonical_order": ["Flute"], "aliases": {"Fl.": "Flute", "Flute": "Flute"},
        "time": "4/4", "transpose": {"Flute": "8va"}}))
    ok, notes = process.process(piece, run=FakeAudiveris())
    assert ok, notes
    root = score.load(piece / "omr" / "under-the-sea-merged.mxl")
    assert root.find("part/measure/attributes/transpose/octave-change").text == "1"
    report = (piece / "omr" / "merge-report.txt").read_text()
    assert "Flute: transposing in 8va" in report


def test_a_bad_transpose_entry_fails_the_piece(tmp_path):
    piece = piece_with_pdfs(tmp_path, count=1)
    (piece / "merge.json").write_text(json.dumps({
        "canonical_order": ["Flute"], "aliases": {"Fl.": "Flute", "Flute": "Flute"},
        "transpose": {"Flute": "Bb-ish"}}))
    ok, notes = process.process(piece, run=FakeAudiveris())
    assert not ok and "Bb-ish" in notes[-1]


def test_images_become_one_pdf_and_spreads_are_split(tmp_path):
    piece = tmp_path / "library" / "song-spread"
    piece.mkdir(parents=True)
    spread = Image.new("RGB", (800, 500), (20, 20, 20))
    for left in (60, 420):
        spread.paste((255, 255, 255), (left, 40, left + 320, 460))
    spread.save(piece / "shot1.png")
    fake = FakeAudiveris()
    ok, notes = process.process(piece, run=fake)
    assert ok, notes
    with pymupdf.open(fake.calls[0][-1]) as doc:
        assert len(doc) == 2


def test_a_folder_of_pdfs_and_images_is_refused(tmp_path):
    piece = piece_with_pdfs(tmp_path, count=1)
    Image.new("RGB", (10, 10)).save(piece / "page.png")
    fake = FakeAudiveris()
    ok, notes = process.process(piece, run=fake)
    assert not ok and "both PDFs and images" in notes[0] and fake.calls == []


def test_a_sharper_read_steps_down_past_a_crash_and_becomes_primary(tmp_path):
    piece = piece_with_pdfs(tmp_path, count=1)
    fake = FakeAudiveris(interline=12, crash_at=(450,))
    ok, notes = process.process(piece, run=fake)
    assert ok, notes
    pdfs = [Path(c[-1]).name for c in fake.calls]
    assert pdfs == ["under-the-sea.pdf", "under-the-sea-450.pdf", "under-the-sea-425.pdf"]
    out = piece / "omr"
    assert (out / "sharp" / "under-the-sea.mxl").is_file()
    assert (out / "sharp" / "audiveris-450dpi-failed.log").is_file()
    assert any(n.startswith("at 450 DPI Audiveris failed") for n in notes)
    assert any(n.startswith("sharper read at 425 DPI") for n in notes)
    assert "primary read: omr/sharp/under-the-sea.mxl; second read: omr/under-the-sea.mxl" in notes


def test_no_sharper_read_when_the_first_is_already_sharp_or_single(tmp_path):
    piece = piece_with_pdfs(tmp_path, count=1)
    fake = FakeAudiveris(interline=19)
    process.process(piece, run=fake)
    assert len(fake.calls) == 1
    other = piece_with_pdfs(tmp_path, name="other", count=1)
    fake = FakeAudiveris(interline=12)
    process.process(other, run=fake, sharp=False)
    assert len(fake.calls) == 1


def test_the_second_read_fills_the_primarys_bad_bars(tmp_path):
    # Primary (sharp) misses a crotchet in Flute bar 2; the 300 DPI read has it.
    sharp_xml = FRAGMENTS
    full = make_score([
        ("P1", "Fl.", part("P1", [bar(four_crotchets(), 1, attributes()), bar(whole_rest(), 2)])),
        ("P2", "Flute", part("P2", [bar(whole_rest(), 1, attributes()), bar([("D5", 1)] * 4, 2)])),
    ])
    piece = piece_with_pdfs(tmp_path, count=1)
    (piece / "merge.json").write_text(json.dumps(
        {"canonical_order": ["Flute"], "aliases": {"Fl.": "Flute", "Flute": "Flute"}}))

    class TwoReads(FakeAudiveris):
        def __call__(self, args, log=None):
            code = super().__call__(args, log)
            out, pdf = Path(args[args.index("-output") + 1]), Path(args[-1])
            if not pdf.stem.rsplit("-", 1)[-1].isdigit():
                write_mxl(out / f"{pdf.stem}.mxl", full)      # the 300 DPI read
            return code

    ok, notes = process.process(piece, run=TwoReads(interline=12, sharp_xml=sharp_xml))
    assert ok, notes
    report = (piece / "omr" / "merge-report.txt").read_text()
    assert "bars taken from the second read: 1" in report
    merged = score.load(piece / "omr" / "under-the-sea-merged.mxl")
    assert score.bad_bars(merged.find("part")) == []


def test_interline_reads_the_smallest_sheet(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("Scale{ interline(12,13,13) x}\nScale{ interline(11,12,12) x}\n")
    assert process.interline(log) == 12
    assert process.interline(tmp_path / "none.log") is None


@pytest.mark.parametrize("found,dpi", [
    ({"low_interline": 10, "too_large": False}, 600),
    ({"low_interline": 15, "too_large": True}, 400),
    ({"low_interline": None, "too_large": True}, 300),
    ({"low_interline": None, "too_large": False}, None),
])
def test_fix_dpi(found, dpi):
    assert process.fix_dpi(found) == dpi
