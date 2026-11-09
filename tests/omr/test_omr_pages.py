"""tools/omr/pages.py, on PDFs and images the tests draw themselves."""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

import pages  # noqa: E402

A4 = (595, 842)                      # points


def one_page_pdf(path: Path, text: str, size=A4) -> Path:
    with pymupdf.open() as doc:
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((72, 72), text)
        for y in range(200, 260, 10):               # a staff, for something to render
            page.draw_line((72, y), (size[0] - 72, y))
        doc.save(path)
    return path


def texts(pdf: Path) -> list[str]:
    with pymupdf.open(pdf) as doc:
        return [page.get_text().strip() for page in doc]


def test_page_order_is_numeric_and_never_mixes_types():
    names = [Path(n) for n in ("p10.pdf", "p2.pdf", "cover.pdf", "p1.pdf")]
    assert [p.name for p in sorted(names, key=pages.page_order)] == ["p1.pdf", "p2.pdf", "p10.pdf", "cover.pdf"]


def test_merge_pdfs_in_page_order(tmp_path):
    files = [one_page_pdf(tmp_path / f"score_{n}.pdf", f"page {n}") for n in (10, 2, 1)]
    out = tmp_path / "out.pdf"
    pages.merge_pdfs(files, out)
    assert texts(out) == ["page 1", "page 2", "page 10"]


def test_capped_dpi():
    width_in, height_in = A4[0] / 72, A4[1] / 72
    dpi = pages.capped_dpi(*A4, 2000, 20_000_000)
    assert width_in * dpi * height_in * dpi <= 20_000_000 < width_in * (dpi + 1) * height_in * (dpi + 1)
    assert pages.capped_dpi(*A4, 300, 20_000_000) == 300


def test_rerender_gives_more_pixels_at_a_declared_300_dpi(tmp_path):
    source = one_page_pdf(tmp_path / "in.pdf", "x")
    out = tmp_path / "out.pdf"
    notes = pages.rerender(source, out, dpi=450)
    assert notes[0].startswith("page 1: ") and "at 450 DPI" in notes[0]
    with pymupdf.open(out) as doc:
        rect = doc[0].rect
    # 450 DPI pixels declared at 300 DPI: the page is 450/300 of its size,
    # so Audiveris's fixed 300 DPI render gets all 450 DPI of pixels.
    assert rect.width == pytest.approx(A4[0] * 450 / 300, abs=1)
    assert rect.height == pytest.approx(A4[1] * 450 / 300, abs=1)


def test_rerender_caps_under_the_pixel_limit(tmp_path):
    source = one_page_pdf(tmp_path / "in.pdf", "x")
    notes = pages.rerender(source, tmp_path / "out.pdf", dpi=900, max_pixels=4_000_000)
    width, height = (int(v) for v in notes[0].split(": ")[1].split(" ")[0].split("x"))
    assert width * height <= 4_000_000 and "capped from 900" in notes[0]


def test_images_to_pdf_in_page_order(tmp_path):
    for n, shade in ((2, 100), (1, 50), (10, 200)):
        Image.new("RGB", (300, 400), (shade, shade, shade)).save(tmp_path / f"img{n}.png")
    out = tmp_path / "out.pdf"
    pages.images_to_pdf(sorted(tmp_path.glob("*.png")), out)
    with pymupdf.open(out) as doc:
        shades = [doc[i].get_pixmap().pixel(10, 10)[0] for i in range(len(doc))]
    assert shades == [50, 100, 200]


def test_split_spread_finds_two_pages_inside_viewer_chrome(tmp_path):
    spread = Image.new("RGB", (1000, 600), (30, 30, 30))       # dark viewer
    for left in (100, 520):
        spread.paste((255, 255, 255), (left, 50, left + 380, 550))   # two white pages
    path = tmp_path / "spread.png"
    spread.save(path)
    halves = pages.split_spread(path, tmp_path / "out", upscale=2)
    assert [h.name for h in halves] == ["spread-1.png", "spread-2.png"]
    for half in halves:
        width, height = Image.open(half).size
        assert 2 * 370 <= width <= 2 * 390 and 2 * 495 <= height <= 2 * 505


def test_split_spread_refuses_a_blank_image(tmp_path):
    path = tmp_path / "blank.png"
    Image.new("RGB", (200, 100), (0, 0, 0)).save(path)
    with pytest.raises(ValueError):
        pages.split_spread(path, tmp_path / "out")


def test_cli_merge_and_refusal(tmp_path, capsys):
    folder = tmp_path / "piece"
    folder.mkdir()
    one_page_pdf(folder / "a1.pdf", "one")
    assert pages.main(["merge", str(folder), str(tmp_path / "out.pdf")]) == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert pages.main(["merge", str(empty), str(tmp_path / "x.pdf")]) == 1
    assert "no PDFs" in capsys.readouterr().err
