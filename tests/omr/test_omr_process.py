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
    """Records each call; writes FRAGMENTS as <pdf stem>.mxl unless told to
    fail with a log line first."""

    def __init__(self, fail_with: list[str] | None = None):
        self.fail_with = list(fail_with or [])
        self.calls: list[list[str]] = []

    def __call__(self, args, log=None):
        self.calls.append(args)
        out, pdf = Path(args[args.index("-output") + 1]), Path(args[-1])
        if self.fail_with:
            log.write(self.fail_with.pop(0) + "\n")
            return 0
        write_mxl(out / f"{pdf.stem}.mxl", FRAGMENTS)
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
    template = json.loads((out / "merge.template.json").read_text())
    assert template["aliases"] == {"Fl.": None, "Flute": None}
    assert notes[-1] == "2 parts; bars that do not add up: 1"


def test_a_low_interline_is_re_rendered_and_read_again(tmp_path):
    piece = piece_with_pdfs(tmp_path, count=1)
    fake = FakeAudiveris(fail_with=["Sheet#1 too low interline value of 10 pixels"])
    ok, notes = process.process(piece, run=fake)
    assert ok, notes
    assert len(fake.calls) == 2 and fake.calls[1][-1].endswith("under-the-sea-fixed.pdf")
    assert any("re-rendering at up to 600 DPI" in n for n in notes)
    assert (piece / "omr" / "under-the-sea.mxl").is_file()


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
    assert "conserved" in (piece / "omr" / "merge-report.txt").read_text()
    assert any("merged with merge.json" in n for n in notes)


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


@pytest.mark.parametrize("found,dpi", [
    ({"low_interline": 10, "too_large": False}, 600),
    ({"low_interline": 15, "too_large": True}, 400),
    ({"low_interline": None, "too_large": True}, 300),
    ({"low_interline": None, "too_large": False}, None),
])
def test_fix_dpi(found, dpi):
    assert process.fix_dpi(found) == dpi
