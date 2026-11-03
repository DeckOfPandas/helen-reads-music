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

Each piece is read twice. First at 300 DPI; then, from the staff-line spacing
(interline) Audiveris measured, again at the DPI that brings it to about 18 px,
stepping down (17, 16, 15) while Audiveris fails at a resolution. At 300 DPI a
dense score reads at about 12 px, where Audiveris drops small symbols, the
time signature among them, and with no time signature it cannot correct its
rhythms. The sharper read is the primary: names.txt, the report and
merge.json are about it. After the merge, its bars that do not add up are
taken from the 300 DPI read where that one's add up (combine.py).
--single skips the sharper read.

Everything made goes in library/<piece>/omr/:
  sharp/<piece>.mxl             the sharper read (the primary, when it worked)
  sharp/audiveris.log           its log; audiveris-<dpi>dpi-failed.log for each
                                resolution Audiveris failed at
  <piece>.mxl                   the 300 DPI read
  <piece>.omr                   Audiveris's own book, to open in its editor
  report.txt                    score.py check: parts, and bars that do not add up
  names.txt                     every part name Audiveris gave
  merge.draft.json              merge.json drafted from the read (draft.py): which
                                parts are which instrument, in what order,
                                transposed how; its "_check" list is what it
                                was unsure of
  <piece>-merged.mxl            once library/<piece>/merge.json exists: merged,
                                then repaired (repair.py: time signature,
                                transposition, whole-bar rests)
  merge-report.txt              what the merge and repairs did, or why the merge
                                refused; how the second read was matched and
                                which bars came from it; what the PDF's drawing
                                corrected (drawing_check.py, when
                                library/<piece>/glyphs.json exists); the parts
                                that double each other and the bars filled
                                from a double (doublings.py); the check of the
                                result
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
import combine
import doublings
import draft
import merge
import pages
import repair
import score

OUT = "omr"
SHARP = "sharp"
# Staff-line spacing, in pixels, the sharper read aims for, in the order
# tried. Audiveris trusts a sheet from about 11 px; at 12 it drops small
# symbols (quaver rests, time signatures). Some page geometries crash it at
# one resolution and not another, hence a ladder rather than one target.
SHARP_TARGETS = (18, 17, 16, 15)


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


def interline(log: Path) -> int | None:
    """The smallest typical staff-line spacing Audiveris measured on any sheet."""
    text = log.read_text(errors="replace") if log.is_file() else ""
    found = [int(m) for m in re.findall(r"Scale\{ interline\(\d+,(\d+),\d+\)", text)]
    return min(found) if found else None


def sharp_read(pdf: Path, out: Path, measured: int, read_dpi: int, name: str,
               run=audiveris.run) -> tuple[Path | None, list[str]]:
    """Read again at the DPI that puts the interline at SHARP_TARGETS, trying
    each in turn until Audiveris succeeds. (score or None, notes)."""
    sharp = out / SHARP
    sharp.mkdir(exist_ok=True)
    notes = []
    tried = set()
    for target in SHARP_TARGETS:
        dpi = round(read_dpi * target / measured)
        if dpi < read_dpi * 1.1:
            notes.append(f"interline already {measured} px; no sharper read needed")
            return None, notes
        render = out / "work" / f"{name}-{dpi}.pdf"
        rendered = pages.rerender(pdf, render, dpi)
        size = tuple(line.split(" at ")[0] for line in rendered)
        if size in tried:                       # capped to a size already tried
            continue
        tried.add(size)
        made = read_with_audiveris(render, sharp, sharp / "audiveris.log", run)
        if made:
            target_mxl = sharp / f"{name}.mxl"
            made[0].rename(target_mxl)
            notes.append(f"sharper read at {dpi} DPI (aiming for {target} px interline): {SHARP}/{name}.mxl")
            return target_mxl, notes
        failed_log = sharp / f"audiveris-{dpi}dpi-failed.log"
        (sharp / "audiveris.log").rename(failed_log)
        notes.append(f"at {dpi} DPI Audiveris failed; log kept as {SHARP}/{failed_log.name}")
    return None, notes


def process(piece: Path, force: bool = False, run=audiveris.run,
            assume_time=None, sharp: bool = True) -> tuple[bool, list[str]]:
    name = slug(piece.name)
    out = piece / OUT
    out.mkdir(exist_ok=True)
    mxl = out / f"{name}.mxl"
    log = out / "audiveris.log"
    sharp_mxl = out / SHARP / f"{name}.mxl"
    notes: list[str] = []

    if mxl.exists() and not force:
        notes.append("already read (--force to read again)")
    else:
        if sharp_mxl.exists():
            sharp_mxl.unlink()
        work = out / "work"
        pdf = work / f"{name}.pdf"
        try:
            build_pdf(piece, work, pdf)
        except ValueError as err:
            return False, [f"could not make a PDF: {err}"]
        read_dpi = 300
        made = read_with_audiveris(pdf, out, log, run)
        if not made:
            dpi = fix_dpi(diagnose(log))
            if dpi:
                notes.append(f"Audiveris refused the resolution; re-rendering at up to {dpi} DPI")
                fixed = work / f"{name}-fixed.pdf"
                notes.extend(pages.rerender(pdf, fixed, dpi))
                read_dpi = dpi
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
        measured = interline(log)
        if sharp and measured:
            _, sharp_notes = sharp_read(pdf, out, measured, read_dpi, name, run)
            notes.extend(sharp_notes)
        elif sharp:
            notes.append("no interline in the Audiveris log; no sharper read")

    # The sharper read, when there is one, is the one the table is written for.
    primary, second = (sharp_mxl, mxl) if sharp_mxl.exists() else (mxl, None)
    if second:
        notes.append(f"primary read: {primary.relative_to(piece)}; second read: {second.relative_to(piece)}")

    config = piece / "merge.json"
    table = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
    if assume_time is None and table.get("time"):
        assume_time = score.parse_time(table["time"])
    mxl = primary
    report = score.check(mxl, assume_time)
    (out / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    names = score.part_names(score.load(mxl))
    (out / "names.txt").write_text("".join(f"{pid} | {n}\n" for pid, n in names), encoding="utf-8")
    drafted, draft_report = draft.draft(score.load(mxl))
    (out / "merge.draft.json").write_text(json.dumps(drafted, indent=2, ensure_ascii=False) + "\n",
                                          encoding="utf-8")
    notes.append(f"{len(names)} parts; {report[-1]}")
    if not config.is_file():
        notes.append(f"{draft_report[0]}: omr/merge.draft.json; check its _check list, then save it as "
                     f"library/{piece.name}/merge.json and run process.py again")

    if config.is_file():
        merged = out / f"{name}-merged.mxl"
        ok, merge_report = merge.merge_file(mxl, merged, config)
        if ok:
            root = score.load(merged)
            try:
                merge_report += ["", "repairs:", *repair.repair(root, table)]
            except ValueError as err:
                return False, notes + [f"merge.json: {err}"]
            if second is not None:
                merge_report += ["", *second_opinion(root, second, table, assume_time)]
            if table.get("borrow_rests", True):
                borrowed = repair.borrow_rests(root, assume_time)
                merge_report += ["", f"rests put back from matching bars: {len(borrowed)}",
                                 *(f"  {line}" for line in borrowed)]
            if table.get("drawing_check", True):
                merge_report += ["", *drawing_pass(root, piece, table, assume_time)]
            if table.get("doublings", True):
                merge_report += ["", *doublings.doublings(root, assume_time, skip=_patched(table))]
            if table.get("patches"):
                try:
                    patched = repair.apply_patches(root, table["patches"], assume_time)
                except ValueError as err:
                    return False, notes + [f"merge.json: {err}"]
                merge_report += ["", f"bars patched by hand: {len(patched)}",
                                 *(f"  {line}" for line in patched)]
                if table.get("drawing_check", True):
                    merge_report += ["", *drawing_pass(root, piece, table, assume_time, patched_only=True)]
            merge_report += ["", *repair.voice_drum_parts(root, table.get("drum_key"))]
            labels = dict(table.get("labels") or {})
            try:
                for spec in table.get("combine") or []:
                    merge_report += repair.combine_parts(root, spec)
                    name = spec.get("name", spec["parts"][0])
                    labels.setdefault(name, {k: spec[k] for k in ("abbreviation", "program") if k in spec})
            except ValueError as err:
                return False, notes + [f"merge.json: {err}"]
            merge_report += repair.fix_labels_and_sounds(root, labels)
            score.save(root, merged, like=merged)
            check = score.check(merged, assume_time)
            merge_report += ["", *check]
            notes.append(f"merged and repaired with merge.json -> {merged.name}; {check[-1]}")
        else:
            notes.append("merge REFUSED: it would have lost notes; see merge-report.txt")
        (out / "merge-report.txt").write_text("\n".join(merge_report) + "\n", encoding="utf-8")
    return True, notes


def _patched(table: dict) -> set[tuple[str, str]]:
    """(part, bar) for every bar merge.json patches by hand."""
    return {(part, bar) for part, bars in (table.get("patches") or {}).items() if not part.startswith("_")
            for bar in bars if not bar.startswith("_")}


def drawing_pass(root: score.ET.Element, piece: Path, table: dict, assume_time,
                 patched_only: bool = False) -> list[str]:
    """Check and correct the score against the PDFs' own drawing
    (drawing_check.py), leaving the bars merge.json patches by hand alone;
    or, `patched_only`, check just those bars, correcting only their ties.
    Report lines."""
    import drawing_check
    import vector
    pdfs = vector.piece_pdfs(piece)
    if not pdfs:
        return ["drawing check: no PDFs in the piece; skipped"]
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        return ["drawing check: PyMuPDF is not installed; skipped"]
    skip = _patched(table)
    if patched_only:
        findings, _ = drawing_check.check(root, piece, pdfs, fix=True, assume=assume_time, only=skip)
        return [f"hand patches checked against the drawing: {len(findings)} disagree, "
                f"{sum(f.fixed for f in findings)} in their ties only (set as drawn)",
                *(f"  {f.describe()}" for f in findings)]
    _, report = drawing_check.check(root, piece, pdfs, fix=True, assume=assume_time, skip=skip)
    return report


def second_opinion(primary: score.ET.Element, second: Path, table: dict, assume_time) -> list[str]:
    """Merge and repair the second read to match `primary`, then fill
    `primary`'s bad bars from it (combine.py). Report lines."""
    raw = score.load(second)
    mapping, report = combine.align(raw, primary, table.get("aliases", {}))
    report.insert(0, "second read, matched to the merged instruments:")
    order = [name for _, name in score.part_names(primary)]
    ok, merge_lines = merge.merge(raw, order, mapping)
    if not ok:
        return report + ["second read not used: its merge did not conserve notes", *merge_lines]
    present = {name for _, name in score.part_names(raw)}
    settings = {**table, "transpose": {n: k for n, k in (table.get("transpose") or {}).items() if n in present}}
    try:
        repair.repair(raw, settings)
    except ValueError as err:
        return report + [f"second read not used: {err}"]
    return report + ["", *combine.fill(primary, raw, assume_time)]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pieces", nargs="+", type=Path)
    parser.add_argument("--force", action="store_true", help="read with Audiveris again")
    parser.add_argument("--time", type=score.parse_time, metavar="N/N",
                        help="time signature off the printed page, for bars Audiveris gave none")
    parser.add_argument("--single", action="store_true",
                        help="read once at 300 DPI: no sharper second read")
    args = parser.parse_args(argv)

    failed = False
    for piece in args.pieces:
        if not piece.is_dir():
            print(f"== {piece}: not a folder", flush=True)
            failed = True
            continue
        print(f"== {piece}", flush=True)
        ok, notes = process(piece, args.force, assume_time=args.time, sharp=not args.single)
        for note in notes:
            print(f"   {note}", flush=True)
        print(f"   {'done' if ok else 'FAILED'}: {piece / OUT}", flush=True)
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
