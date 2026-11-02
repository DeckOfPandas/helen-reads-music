#!/usr/bin/env python3
"""Turn a piece in library/ into MusicXML, with a report on what to check.

USAGE (from the repo root):
    python3 tools/omr/process.py library/<piece> [library/<piece> ...] [--force] [--time 4/4]

A piece is a folder in library/. What is in it decides what is read:
  - PDFs              merged into one, in page-number order
  - page images       wrapped into one PDF, in page-number order
  - images, in a folder whose name contains "spread"
                      each split into two pages first (pages.py split)
A folder holding both PDFs and images is refused: split it.

Everything made goes in library/<piece>/omr/:
  <piece>.mxl                   what Audiveris read
  <piece>.omr                   Audiveris's own book, to open in its editor
  report.txt                    score.py check: parts, and bars that do not add up
  names.txt                     every part name Audiveris gave
  merge.template.json           a table to fill in (see merge.py)
  <piece>-merged.mxl            once library/<piece>/merge.json exists: merged,
                                then repaired (repair.py: time signature,
                                transposition, whole-bar rests)
  merge-report.txt              what the merge and repairs did, or why the merge
                                refused, and the check of the result
  audiveris.log                 the full Audiveris log
  work/                         the PDF handed to Audiveris

When Audiveris rejects a sheet as too low resolution ("too low interline
value of N pixels") or too large ("Too large image"), the PDF is
re-rendered at a DPI that fixes it (pages.py rerender) and read once more.

A piece whose <piece>.mxl exists is not read again unless --force; a new or
changed merge.json is still applied.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import audiveris
import merge
import pages
import repair
import score

OUT = "omr"


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "piece"


def sources(piece: Path) -> tuple[list[Path], list[Path]]:
    files = [p for p in piece.iterdir() if p.is_file()]
    pdfs = [p for p in files if p.suffix.lower() == ".pdf"]
    images = [p for p in files if p.suffix.lower() in pages.IMAGE_SUFFIXES]
    return pdfs, images


def build_pdf(piece: Path, work: Path, out: Path) -> None:
    """One PDF for Audiveris from the piece's sources. ValueError if it can't."""
    work.mkdir(parents=True, exist_ok=True)
    pdfs, images = sources(piece)
    if pdfs and images:
        raise ValueError("the folder holds both PDFs and images: put them in separate pieces")
    if pdfs:
        pages.merge_pdfs(pdfs, out)
        return
    if not images:
        raise ValueError("no PDFs or images in the folder")
    if "spread" in piece.name.lower():
        halves = []
        for number, image in enumerate(sorted(images, key=pages.page_order)):
            for half in pages.split_spread(image, work / f"spread-{number:03d}"):
                target = work / f"p{number:03d}-{half.stem[-1]}.png"
                shutil.copy(half, target)
                halves.append(target)
        images = halves
    pages.images_to_pdf(images, out)


def diagnose(log: Path) -> dict:
    text = log.read_text(errors="replace") if log.is_file() else ""
    low = [int(n) for n in re.findall(r"too low interline value of (\d+) pixels", text)]
    return {"low_interline": min(low) if low else None, "too_large": "Too large image" in text}


def fix_dpi(found: dict) -> int | None:
    """The DPI to re-render at: aims the interline at 20 px, from the 300 DPI
    Audiveris read it at. 300 for a too-large page, which pages.rerender caps."""
    if found["low_interline"]:
        return int(300 * 20 / found["low_interline"])
    if found["too_large"]:
        return 300
    return None


def read_with_audiveris(pdf: Path, out: Path, log: Path, run=audiveris.run) -> list[Path]:
    # A score left from an earlier read would pass for this one's output.
    for old in out.glob("*.mxl"):
        old.unlink()
    with open(log, "w", encoding="utf-8") as handle:
        run(["-batch", "-export", "-save", "-output", str(out), "--", str(pdf)], log=handle)
    return sorted(out.glob(f"{pdf.stem}*.mxl"))


def process(piece: Path, force: bool = False, run=audiveris.run,
            assume_time=None) -> tuple[bool, list[str]]:
    name = slug(piece.name)
    out = piece / OUT
    out.mkdir(exist_ok=True)
    mxl = out / f"{name}.mxl"
    log = out / "audiveris.log"
    notes: list[str] = []

    if mxl.exists() and not force:
        notes.append("already read (--force to read again)")
    else:
        work = out / "work"
        pdf = work / f"{name}.pdf"
        try:
            build_pdf(piece, work, pdf)
        except ValueError as err:
            return False, [f"could not make a PDF: {err}"]
        made = read_with_audiveris(pdf, out, log, run)
        if not made:
            dpi = fix_dpi(diagnose(log))
            if dpi:
                notes.append(f"Audiveris refused the resolution; re-rendering at up to {dpi} DPI")
                fixed = work / f"{name}-fixed.pdf"
                notes.extend(pages.rerender(pdf, fixed, dpi))
                made = read_with_audiveris(fixed, out, log, run)
                for path in made:
                    path.rename(out / path.name.replace(f"{name}-fixed", name))
                made = sorted(out.glob(f"{name}*.mxl"))
        made = [p for p in made if not p.stem.endswith("-merged")]
        if not made:
            return False, notes + [f"Audiveris made no MusicXML: read {log}"]
        if mxl not in made:
            notes.append(f"Audiveris split the book into {len(made)} movements: "
                         f"{[p.name for p in made]}; the report covers the first")
            mxl = made[0]

    report = score.check(mxl, assume_time)
    (out / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    names = score.part_names(score.load(mxl))
    (out / "names.txt").write_text("".join(f"{pid} | {n}\n" for pid, n in names), encoding="utf-8")
    template = {
        "_help": ("List the real instruments in score order in canonical_order, and set each "
                  "name in aliases to one of them (null leaves it its own part). Where one "
                  "name covers different staves (often 'Voice'), add keys by part id from "
                  "names.txt, e.g. \"P23\": \"Marimba\"; an id wins over its name. Save as "
                  f"library/{piece.name}/merge.json and run process.py again."),
        "canonical_order": [],
        "aliases": {n: None for _, n in names},
    }
    (out / "merge.template.json").write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
    notes.append(f"{len(names)} parts; {report[-1]}")

    config = piece / "merge.json"
    if config.is_file():
        table = json.loads(config.read_text(encoding="utf-8"))
        if assume_time is None and table.get("time"):
            assume_time = score.parse_time(table["time"])
        merged = out / f"{name}-merged.mxl"
        ok, merge_report = merge.merge_file(mxl, merged, config)
        if ok:
            root = score.load(merged)
            try:
                merge_report += ["", "repairs:", *repair.repair(root, table)]
            except ValueError as err:
                return False, notes + [f"merge.json: {err}"]
            score.save(root, merged, like=merged)
            check = score.check(merged, assume_time)
            merge_report += ["", *check]
            notes.append(f"merged and repaired with merge.json -> {merged.name}; {check[-1]}")
        else:
            notes.append("merge REFUSED: it would have lost notes; see merge-report.txt")
        (out / "merge-report.txt").write_text("\n".join(merge_report) + "\n", encoding="utf-8")
    return True, notes


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pieces", nargs="+", type=Path)
    parser.add_argument("--force", action="store_true", help="read with Audiveris again")
    parser.add_argument("--time", type=score.parse_time, metavar="N/N",
                        help="time signature off the printed page, for bars Audiveris gave none")
    args = parser.parse_args(argv)

    failed = False
    for piece in args.pieces:
        if not piece.is_dir():
            print(f"== {piece}: not a folder", flush=True)
            failed = True
            continue
        print(f"== {piece}", flush=True)
        ok, notes = process(piece, args.force, assume_time=args.time)
        for note in notes:
            print(f"   {note}", flush=True)
        print(f"   {'done' if ok else 'FAILED'}: {piece / OUT}", flush=True)
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
