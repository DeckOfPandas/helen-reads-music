#!/usr/bin/env python3
"""Read a PDF with Audiveris in chunks of pages, so that a failure costs one
chunk, and a run that is stopped carries on where it left off.

Audiveris exports nothing for a book when one sheet fails, and keeps a whole
book in memory. So the pages are read CHUNK_PAGES at a time, each chunk its
own book in its own folder, and the chunks' scores joined into one score
shaped as Audiveris would have written it (join_scores).

A chunk that fails:
  - crashed on some sheets ("Error processing stub"): those pages are drawn
    CRASH_SHRINK smaller and the chunk read again, up to CRASH_RETRIES times;
  - still failing, or out of Java heap: it is split in two and each half
    read on its own, down to single pages. A single page that fails is lost,
    and named in the notes; its bars are missing from the score.
  - refused for resolution while being read as drawn: the whole read is
    abandoned with `refused_dpi`, for the caller to read again at that DPI.

Each finished chunk (or half) writes done.json last, atomically, so on the
next run it is taken as it is. Java killed by a signal (the system out of
memory, the container stopping) raises Interrupted and writes nothing for
that chunk: the next run reads it again. progress.txt in the folder logs
each chunk as it finishes, with the time.

Folder layout, under the `folder` given:
  p001-005/            one chunk: source.pdf (its pages), the PDF read,
                       audiveris.log, the .mxl, done.json; a/ and b/ if split
  audiveris.log        every chunk's log, in page order
  <name>.mxl           the joined score
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import audiveris
import pages
import score

CHUNK_PAGES = 5
# DPIs to try on a page whose bars don't match the printed count, in order.
# On a dense scan Audiveris splits one system into several, or misses bar
# lines, at one scale and not another; lower ones often read the layout
# right where the sharpest read does not.
PAGE_LADDER = (330, 360, 400, 300)
# Crashes depend on a page's geometry in pixels; 7% smaller is a different
# page to Audiveris and still close to the resolution asked for.
CRASH_SHRINK = 0.93
CRASH_RETRIES = 3
DONE = "done.json"
AS_DRAWN_DPI = 300                 # Audiveris rasterizes a PDF page at 300 DPI


class Interrupted(RuntimeError):
    """Audiveris was killed from outside; nothing was decided about the pages."""


@dataclass
class Result:
    mxls: list[Path] = field(default_factory=list)
    lost: list[int] = field(default_factory=list)
    logs: list[Path] = field(default_factory=list)
    refused_dpi: int | None = None


@dataclass
class Book:
    mxl: Path | None
    lost: list[int]
    notes: list[str]
    refused_dpi: int | None = None
    log: Path | None = None


def diagnose(text: str) -> dict:
    low = [int(n) for n in re.findall(r"too low interline value of (\d+) pixels", text)]
    return {"low_interline": min(low) if low else None, "too_large": "Too large image" in text}


def fix_dpi(found: dict) -> int | None:
    """The DPI to re-render at: aims the interline at 20 px, from the 300 DPI
    Audiveris read it at. 300 for a too-large page, which pages.rerender caps."""
    if found["low_interline"]:
        return int(AS_DRAWN_DPI * 20 / found["low_interline"])
    if found["too_large"]:
        return AS_DRAWN_DPI
    return None


def crashed_sheets(text: str) -> list[int]:
    """The sheets (from 1, within the book) Audiveris failed on with an exception."""
    found = re.findall(r"\[[^\]\s]*#(\d+)\]\s+Book\s+\d+\s+\|\s+Error processing stub", text)
    return sorted({int(n) for n in found})


def _write_atomic(path: Path, text: str) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)


def _progress(folder: Path, line: str) -> None:
    with open(folder / "progress.txt", "a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")


def _span(numbers: list[int]) -> str:
    return f"page {numbers[0]}" if len(numbers) == 1 else f"pages {numbers[0]}-{numbers[-1]}"


def _extract(pdf: Path, numbers: list[int], out: Path) -> None:
    import pymupdf
    with pymupdf.open(pdf) as source, pymupdf.open() as chunk:
        for number in numbers:
            chunk.insert_pdf(source, from_page=number - 1, to_page=number - 1)
        chunk.save(out)


def _load_done(folder: Path) -> Result:
    data = json.loads((folder / DONE).read_text(encoding="utf-8"))
    return Result([folder / p for p in data["mxls"]], data["lost"], [folder / p for p in data["logs"]])


def _save_done(folder: Path, result: Result) -> None:
    rel = lambda paths: [str(p.relative_to(folder)) for p in paths]  # noqa: E731
    _write_atomic(folder / DONE, json.dumps({"mxls": rel(result.mxls), "lost": result.lost,
                                             "logs": rel(result.logs)}, indent=1))


def read_pages(pdf: Path, numbers: list[int], folder: Path, dpi: int | None, run, top: Path,
               one_try: bool = False) -> Result:
    """Read pages `numbers` (from 1) of `pdf` as one book in `folder`, at
    `dpi`, or as drawn when None. Retries, splits and resumes as the module
    docstring says."""
    if (folder / DONE).is_file():
        return _load_done(folder)
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "source.pdf"
    _extract(pdf, numbers, source)
    stem = f"pages-{numbers[0]:03d}-{numbers[-1]:03d}"
    book_pdf, log = folder / f"{stem}.pdf", folder / "audiveris.log"
    page_dpi: dict[int, int] = {}
    for attempt in range(1, 2 + CRASH_RETRIES):
        if attempt > 1:                         # keep the failed attempt's log
            log.rename(folder / f"audiveris-attempt{attempt - 1}.log")
        if dpi is None and not page_dpi:
            shutil.copy(source, book_pdf)
        else:
            pages.rerender(source, book_pdf, dpi or AS_DRAWN_DPI, page_dpi=page_dpi)
        for old in folder.glob("*.mxl"):
            old.unlink()
        # No -save: Audiveris keeps its .omr book up to date as it goes, and
        # the final save races export's reloading of the sheets from it
        # (FileSystemAlreadyExistsException), failing most books of 5 pages.
        with open(log, "w", encoding="utf-8") as handle:
            code = run(["-batch", "-export", "-output", str(folder), "--", str(book_pdf)], log=handle)
        # Killed by a signal: a negative status, or 128 + the signal when
        # Java catches it (SIGTERM exits 143).
        if code is not None and (code < 0 or code >= 128):
            signal = -code if code < 0 else code - 128
            raise Interrupted(f"Audiveris was killed (signal {signal}) reading {_span(numbers)}")
        made = sorted(folder.glob(f"{stem}*.mxl"), key=pages.page_order)
        if made:
            result = Result(made, [], [log])
            _save_done(folder, result)
            shrunk = f", shrunk {sorted(page_dpi.items())}" if page_dpi else ""
            _progress(top, f"{_span(numbers)} read at {dpi or 'as drawn'}{shrunk}")
            return result
        text = log.read_text(errors="replace")
        fix = fix_dpi(diagnose(text))
        if dpi is None and fix:
            return Result(refused_dpi=fix, logs=[log])
        crashed = [s for s in crashed_sheets(text) if s <= len(numbers)]
        if not crashed or "OutOfMemoryError" in text:
            break
        now = pages.rendered_dpis(source, dpi or AS_DRAWN_DPI, page_dpi=page_dpi)
        for sheet in crashed:
            page_dpi[sheet] = round(now[sheet] * CRASH_SHRINK)
        _progress(top, f"{_span(numbers)}: Audiveris crashed on page(s) "
                       f"{[numbers[s - 1] for s in crashed]}; drawing them smaller")
    if len(numbers) == 1:
        result = Result([], list(numbers), [log])
        if one_try:                             # read_page_checked tries other DPIs next
            _progress(top, f"page {numbers[0]} failed at {dpi or 'as drawn'} DPI; see {log}")
        else:
            _progress(top, f"page {numbers[0]} LOST: Audiveris fails on it at every size tried; see {log}")
    else:
        half = len(numbers) // 2
        _progress(top, f"{_span(numbers)} failed as one book; reading each half alone")
        first = read_pages(pdf, numbers[:half], folder / "a", dpi, run, top)
        if first.refused_dpi:
            return first
        second = read_pages(pdf, numbers[half:], folder / "b", dpi, run, top)
        if second.refused_dpi:
            return second
        result = Result(first.mxls + second.mxls, first.lost + second.lost, first.logs + second.logs)
    _save_done(folder, result)
    return result


def bar_count(mxls: list[Path]) -> int:
    return sum(len(score.load(m).find("part").findall("measure")) for m in mxls)


def read_page_checked(pdf: Path, page: int, folder: Path, dpi: int | None, run, top: Path,
                      want: int) -> Result:
    """Read one page at `dpi`, then at each of PAGE_LADDER until Audiveris
    finds the `want` bars printed on it; keep the first that does, else the
    closest. Each try is its own folder (at-<dpi>), so it resumes."""
    if (folder / DONE).is_file():
        return _load_done(folder)
    tries = []
    for each in [dpi] + [d for d in PAGE_LADDER if d != dpi]:
        read = read_pages(pdf, [page], folder / f"at-{each or 'drawn'}", each, run, top, one_try=True)
        if read.refused_dpi or not read.mxls:
            continue
        got = bar_count(read.mxls)
        tries.append((abs(got - want), each, got, read))
        if got == want:
            break
    if not tries:
        result = Result([], [page], [])
        _progress(top, f"page {page} LOST: no DPI gave a score")
    else:
        miss, each, got, result = min(tries, key=lambda t: t[0])
        tried = ", ".join(f"{d or 'drawn'}: {g}" for _, d, g, _ in tries)
        verdict = "matches" if not miss else "CLOSEST, does NOT match"
        _progress(top, f"page {page}: {got} bars at {each or 'as drawn'} DPI, printed {want}, "
                       f"{verdict} (tried {tried})")
    _save_done(folder, result)
    return result


def read_book(pdf: Path, folder: Path, name: str, dpi: int | None = None,
              run=audiveris.run, chunk_pages: int = CHUNK_PAGES,
              printed: dict[int, int] | None = None) -> Book:
    """Read all of `pdf` in chunks into `folder`, then join them into
    `folder`/<name>.mxl. With `printed` ({page: bars printed on it}), each
    page is read alone and checked (read_page_checked). Raises Interrupted
    if Audiveris is killed."""
    import pymupdf
    folder.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(pdf) as doc:
        count = len(doc)
    results = []
    for first in range(1, count + 1, 1 if printed else chunk_pages):
        if printed and first in printed:
            result = read_page_checked(pdf, first, folder / f"p{first:03d}", dpi, run, folder, printed[first])
            results.append(result)
            continue
        numbers = list(range(first, min(first + (1 if printed else chunk_pages) - 1, count) + 1))
        result = read_pages(pdf, numbers, folder / f"p{numbers[0]:03d}-{numbers[-1]:03d}", dpi, run, folder)
        if result.refused_dpi:
            return Book(None, [], [], refused_dpi=result.refused_dpi, log=result.logs[0])
        results.append(result)
    log = folder / "audiveris.log"
    with open(log, "w", encoding="utf-8") as handle:
        for result in results:
            for path in result.logs:
                handle.write(f"===== {path.relative_to(folder)}\n")
                handle.write(path.read_text(errors="replace"))
    lost = sorted(p for r in results for p in r.lost)
    notes = [f"page {p} LOST: Audiveris fails on it at every size tried; its bars are missing" for p in lost]
    mxls = [m for r in results for m in r.mxls]
    if not mxls:
        return Book(None, lost, notes, log=log)
    out = folder / f"{name}.mxl"
    join_scores(mxls, out)
    return Book(out, lost, notes, log=log)


def _show(measure: score.ET.Element, shown: bool) -> None:
    """Say at `measure` whether its part's staves are printed from here on."""
    from repair import _insert_in_order
    attributes = measure.find("attributes")
    if attributes is None:
        attributes = score.ET.Element("attributes")
        after = [i for i, c in enumerate(measure) if c.tag == "print"]
        measure.insert(after[-1] + 1 if after else 0, attributes)
    if attributes.find("staff-details") is None:
        _insert_in_order(attributes, score.ET.Element("staff-details",
                                                      {"print-object": "yes" if shown else "no"}))


def join_scores(paths: list[Path], out: Path) -> None:
    """One score from chunks read one after another: every chunk's parts,
    renumbered P1.. in order, each padded with empty bars, staves marked
    unprinted, where another chunk is playing; bars numbered on from the
    chunk before."""
    roots = [score.load(p) for p in paths]
    joined = copy.deepcopy(roots[0])
    for child in [c for c in joined if c.tag in ("part-list", "part")]:
        joined.remove(child)
    numbers: list[list[str]] = []
    last = 0
    for k, root in enumerate(roots):
        first_part = root.find("part")
        own = [m.get("number") for m in first_part.findall("measure")] if first_part is not None else []
        if k == 0:
            numbers.append(own)
            digits = [int(n) for n in own if n.isdigit()]
            last = max(digits) if digits else len(own)
        else:
            numbers.append([str(last + i + 1) for i in range(len(own))])
            last += len(own)
    part_list = score.ET.SubElement(joined, "part-list")
    new_parts = []
    serial = 0
    for k, root in enumerate(roots):
        parts = {p.get("id"): p for p in root.findall("part")}
        for entry in root.find("part-list"):
            entry = copy.deepcopy(entry)
            part_list.append(entry)
            if entry.tag != "score-part":
                continue
            serial += 1
            old, new = entry.get("id"), f"P{serial}"
            part = copy.deepcopy(parts[old])
            part.set("id", new)
            for element in [entry, *entry.iter(), *part.iter("instrument")]:
                ident = element.get("id")
                if ident == old:
                    element.set("id", new)
                elif ident and ident.startswith(old + "-"):
                    element.set("id", new + ident[len(old):])
            measures = part.findall("measure")
            for measure in measures:
                part.remove(measure)
            for measure, number in zip(measures, numbers[k]):
                measure.set("number", number)
            before = [score.ET.Element("measure", {"number": n}) for j in range(k) for n in numbers[j]]
            after = [score.ET.Element("measure", {"number": n}) for j in range(k + 1, len(roots))
                     for n in numbers[j]]
            if before and measures:
                _show(before[0], False)
                _show(measures[0], True)
            if after:
                _show(after[0], False)
            part.extend(before + measures + after)
            new_parts.append(part)
    joined.extend(new_parts)
    score.save(joined, out)
