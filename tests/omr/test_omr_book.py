"""tools/omr/book.py: reading in chunks, surviving failures, carrying on after
an interruption, and joining the chunks into one score."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

import book  # noqa: E402
import draft  # noqa: E402
import score  # noqa: E402
from musicxml_maker import attributes, bar, four_crotchets, part, score as make_score, write_mxl  # noqa: E402

A4_WIDTH = 595


class FakeReader:
    """Stands in for Audiveris. Each PDF it reads is named pages-<first>-<last>;
    it writes a score of one part, one bar per page. A page in `crash` crashes
    its book until it is drawn smaller; in `always`, at any size; in `oom`,
    Java runs out of heap. `kill_on` (a call number, from 1) is killed by a
    signal."""

    def __init__(self, crash=(), always=(), oom=(), kill_on=None, refuse=False):
        self.crash, self.always, self.oom = set(crash), set(always), set(oom)
        self.kill_on, self.refuse = kill_on, refuse
        self.calls: list[tuple[int, int]] = []

    def __call__(self, args, log=None):
        out, pdf = Path(args[args.index("-output") + 1]), Path(args[-1])
        first, last = map(int, re.findall(r"\d+", pdf.stem))
        self.calls.append((first, last))
        if len(self.calls) == self.kill_on:
            return -9
        if self.refuse:
            log.write("Sheet#1 too low interline value of 10 pixels\n")
            return 0
        with pymupdf.open(pdf) as doc:
            widths = [page.rect.width for page in doc]
        for sheet, (number, width) in enumerate(zip(range(first, last + 1), widths), start=1):
            if number in self.oom:
                log.write("java.lang.OutOfMemoryError: Java heap space\n")
                return 1
            shrunk = width < A4_WIDTH * 0.95
            if number in self.always or (number in self.crash and not shrunk):
                log.write(f"WARN  [{pdf.stem}#{sheet}]   Book 2044 | Error processing stub boom\n")
                return 0
        bars = [bar(four_crotchets(), n - first + 1, attributes() if n == first else "")
                for n in range(first, last + 1)]
        write_mxl(out / f"{pdf.stem}.mxl", make_score([("P1", f"Fl. {first}", part("P1", bars))]))
        log.write(f"read {first}-{last}\n")
        return 0


def a_pdf(tmp_path: Path, count: int) -> Path:
    pdf = tmp_path / "piece.pdf"
    with pymupdf.open() as doc:
        for n in range(count):
            doc.new_page().insert_text((72, 72), f"page {n + 1}")
        doc.save(pdf)
    return pdf


def test_pages_are_read_in_chunks_and_joined(tmp_path):
    reader = FakeReader()
    read = book.read_book(a_pdf(tmp_path, 5), tmp_path / "read", "piece", run=reader, chunk_pages=2)
    assert reader.calls == [(1, 2), (3, 4), (5, 5)]
    assert read.lost == [] and read.mxl == tmp_path / "read" / "piece.mxl"
    root = score.load(read.mxl)
    assert score.part_names(root) == [("P1", "Fl. 1"), ("P2", "Fl. 3"), ("P3", "Fl. 5")]
    for part in root.findall("part"):
        assert [m.get("number") for m in part.findall("measure")] == ["1", "2", "3", "4", "5"]
    assert sum(score.count_part_notes(p) for p in root.findall("part")) == 5 * 4
    # Each chunk's part is printed only over its own pages, as Audiveris marks a fragment.
    assert [sorted(f.bars, key=int) for f in draft.fragments(root)] == [["1", "2"], ["3", "4"], ["5"]]
    assert "pages 3-4 read at as drawn" in (tmp_path / "read" / "progress.txt").read_text()
    assert "read 3-4" in read.log.read_text()


def test_a_page_that_crashes_is_drawn_smaller_and_read_again(tmp_path):
    reader = FakeReader(crash={4})
    read = book.read_book(a_pdf(tmp_path, 5), tmp_path / "read", "piece", run=reader, chunk_pages=5)
    assert reader.calls == [(1, 5), (1, 5)] and read.lost == []
    with pymupdf.open(tmp_path / "read" / "p001-005" / "pages-001-005.pdf") as doc:
        assert round(doc[3].rect.width / doc[0].rect.width, 2) == book.CRASH_SHRINK
    assert "Audiveris crashed on page(s) [4]" in (tmp_path / "read" / "progress.txt").read_text()
    assert "Error processing stub" in (tmp_path / "read" / "p001-005" / "audiveris-attempt1.log").read_text()


def test_a_page_that_always_fails_is_lost_alone(tmp_path):
    reader = FakeReader(always={4})
    read = book.read_book(a_pdf(tmp_path, 5), tmp_path / "read", "piece", run=reader, chunk_pages=5)
    assert read.lost == [4]
    assert read.notes == ["page 4 LOST: Audiveris fails on it at every size tried; its bars are missing"]
    root = score.load(read.mxl)
    assert sum(score.count_part_notes(p) for p in root.findall("part")) == 4 * 4
    assert len(root.find("part").findall("measure")) == 4
    assert "page 4 LOST" in (tmp_path / "read" / "progress.txt").read_text()


def test_out_of_memory_splits_the_chunk_at_once(tmp_path):
    reader = FakeReader(oom={1})
    read = book.read_book(a_pdf(tmp_path, 2), tmp_path / "read", "piece", run=reader, chunk_pages=2)
    assert reader.calls == [(1, 2), (1, 1), (2, 2)] and read.lost == [1]


def test_an_interrupted_read_carries_on_where_it_stopped(tmp_path):
    pdf = a_pdf(tmp_path, 5)
    killed = FakeReader(kill_on=2)
    with pytest.raises(book.Interrupted, match="signal 9"):
        book.read_book(pdf, tmp_path / "read", "piece", run=killed, chunk_pages=2)
    again = FakeReader()
    read = book.read_book(pdf, tmp_path / "read", "piece", run=again, chunk_pages=2)
    assert again.calls == [(3, 4), (5, 5)]          # pages 1-2 were finished and kept
    assert len(score.part_names(score.load(read.mxl))) == 3


def test_a_refused_resolution_stops_the_read_for_the_caller(tmp_path):
    read = book.read_book(a_pdf(tmp_path, 3), tmp_path / "read", "piece", run=FakeReader(refuse=True))
    assert read.mxl is None and read.refused_dpi == 600


def test_crashed_sheets_and_fix_dpi():
    log = ("WARN  [x-fixed#39]                      Book 2044 | Error processing stub java.lang.Runtime\n"
           "INFO  [x-fixed#40]                KeyBuilder 1380 | Staff#9 key replication failed.\n")
    assert book.crashed_sheets(log) == [39]
    assert book.fix_dpi(book.diagnose("too low interline value of 10 pixels")) == 600
