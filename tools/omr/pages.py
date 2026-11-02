#!/usr/bin/env python3
"""Get sheet music into the one PDF Audiveris reads best.

USAGE (from the repo root):
    python3 tools/omr/pages.py merge    <folder-of-page-pdfs> <out.pdf>
    python3 tools/omr/pages.py images   <folder-of-images> <out.pdf> [--dpi N]
    python3 tools/omr/pages.py rerender <in.pdf> <out.pdf> [--dpi N] [--max-pixels N]
    python3 tools/omr/pages.py split    <spread.png> <out-folder> [--upscale N] [--density F]

merge     Per-page PDFs into one, in page-number order. Audiveris treats one
          file as one book: one PDF carries the key and time signature across
          its pages, separate files each start from nothing.
images    Page images (PNG, JPEG) into one PDF, in page-number order. --dpi is
          a declaration, not a resize: see rerender.
rerender  Re-renders each page at a higher DPI. Audiveris rasterizes every
          PDF page at a fixed 300 DPI from its declared physical size, so the
          only way to give it more pixels is a page declared physically
          larger: this renders at --dpi and declares the pixels 300 DPI, so
          a page rendered at 450 comes out 1.5 times its size. Audiveris refuses
          a sheet over 20 megapixels ("Too large image"), so the DPI is capped
          per page to stay under --max-pixels.
split     A screenshot of a two-page spread into two cropped, upscaled pages.
          A spread halves each page's resolution, which puts the staff-line
          spacing (interline) under the ~11 px Audiveris will trust.

"Page-number order" sorts on the last number in each file name, so page_9
comes before page_10.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
AUDIVERIS_PDF_DPI = 300
AUDIVERIS_MAX_PIXELS = 20_000_000


def page_order(path: Path) -> tuple[int, int, str]:
    numbers = re.findall(r"\d+", path.stem)
    return (0, int(numbers[-1]), path.name) if numbers else (1, 0, path.name)


def merge_pdfs(pdfs: list[Path], out: Path) -> None:
    from pypdf import PdfWriter
    if not pdfs:
        raise ValueError("no PDFs to merge")
    writer = PdfWriter()
    for pdf in sorted(pdfs, key=page_order):
        writer.append(str(pdf))
    with open(out, "wb") as handle:
        writer.write(handle)


def images_to_pdf(images: list[Path], out: Path, dpi: float = AUDIVERIS_PDF_DPI) -> None:
    from PIL import Image
    if not images:
        raise ValueError("no images to wrap")
    pages = [Image.open(p).convert("RGB") for p in sorted(images, key=page_order)]
    pages[0].save(out, save_all=True, append_images=pages[1:], resolution=float(dpi))


def capped_dpi(width_pt: float, height_pt: float, dpi: int, max_pixels: int) -> int:
    """`dpi`, lowered if needed so the page renders under `max_pixels`."""
    width_in, height_in = width_pt / 72.0, height_pt / 72.0
    if width_in * dpi * height_in * dpi <= max_pixels:
        return dpi
    return int((max_pixels / (width_in * height_in)) ** 0.5)


def rerender(pdf: Path, out: Path, dpi: int = 450,
             max_pixels: int = AUDIVERIS_MAX_PIXELS) -> list[str]:
    """Re-render every page; returns one line per page saying what it did."""
    import pymupdf
    from PIL import Image
    notes, pages = [], []
    with pymupdf.open(pdf) as doc:
        for number, page in enumerate(doc, start=1):
            use = capped_dpi(page.rect.width, page.rect.height, dpi, max_pixels)
            zoom = use / 72.0
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append(image)
            capped = f" (capped from {dpi} to stay under {max_pixels / 1e6:.0f} MP)" if use < dpi else ""
            notes.append(f"page {number}: {pix.width}x{pix.height} at {use} DPI{capped}")
    if not pages:
        raise ValueError(f"{pdf} has no pages")
    pages[0].save(out, save_all=True, append_images=pages[1:], resolution=float(AUDIVERIS_PDF_DPI))
    return notes


def _dense_bbox(image, threshold: int = 128, stride: int = 2, density: float = 0.5):
    """The box of rows and columns that are mostly bright: the page, not the
    viewer's dark chrome around it. None if nothing is."""
    gray = image.convert("L")
    width, height = gray.size
    px = gray.load()
    rows, cols = range(0, height, stride), range(0, width, stride)
    dense_cols = [x for x in range(width)
                  if sum(px[x, y] > threshold for y in rows) / len(rows) > density]
    dense_rows = [y for y in range(height)
                  if sum(px[x, y] > threshold for x in cols) / len(cols) > density]
    if not dense_cols or not dense_rows:
        return None
    return min(dense_cols), min(dense_rows), max(dense_cols) + 1, max(dense_rows) + 1


def split_spread(spread: Path, out_dir: Path, upscale: int = 3, density: float = 0.5) -> list[Path]:
    from PIL import Image
    image = Image.open(spread).convert("RGB")
    box = _dense_bbox(image, density=density)
    if box is None:
        raise ValueError(f"{spread}: no page found (blank, or under the density threshold)")
    x0, _, x1, _ = box
    mid = (x0 + x1) // 2
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for half, (left, right) in enumerate(((x0, mid), (mid, x1)), start=1):
        crop = image.crop((left, 0, right, image.height))
        inner = _dense_bbox(crop, density=density)
        if inner:
            crop = crop.crop(inner)
        crop = crop.resize((crop.width * upscale, crop.height * upscale), Image.LANCZOS)
        path = out_dir / f"{spread.stem}-{half}.png"
        crop.save(path)
        written.append(path)
    return written


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="verb", required=True)
    m = sub.add_parser("merge")
    m.add_argument("folder", type=Path)
    m.add_argument("out", type=Path)
    i = sub.add_parser("images")
    i.add_argument("folder", type=Path)
    i.add_argument("out", type=Path)
    i.add_argument("--dpi", type=float, default=AUDIVERIS_PDF_DPI)
    r = sub.add_parser("rerender")
    r.add_argument("pdf", type=Path)
    r.add_argument("out", type=Path)
    r.add_argument("--dpi", type=int, default=450)
    r.add_argument("--max-pixels", type=int, default=AUDIVERIS_MAX_PIXELS)
    s = sub.add_parser("split")
    s.add_argument("spread", type=Path)
    s.add_argument("out_dir", type=Path)
    s.add_argument("--upscale", type=int, default=3)
    s.add_argument("--density", type=float, default=0.5)
    args = parser.parse_args(argv)

    try:
        if args.verb == "merge":
            merge_pdfs([p for p in args.folder.iterdir() if p.suffix.lower() == ".pdf"], args.out)
        elif args.verb == "images":
            images_to_pdf([p for p in args.folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES],
                          args.out, args.dpi)
        elif args.verb == "rerender":
            print("\n".join(rerender(args.pdf, args.out, args.dpi, args.max_pixels)))
        else:
            for path in split_spread(args.spread, args.out_dir, args.upscale, args.density):
                print(path)
    except ValueError as err:
        print(f"pages.py: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
