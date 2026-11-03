---
name: read-sheet-music
description: Turn a sheet-music PDF (or scans or photos of pages) in library/<piece>/ into MusicXML for Sibelius with Audiveris and this repo's OMR tools, then check it bar by bar. Use when Helen gives you a new piece to read, ingest, transcribe or convert, or asks you to fix a piece already read.
---

# Reading a piece of sheet music

The tools are in `tools/omr/`; `tools/omr/README.md` explains each step and
each `--help` gives the details. This skill is the order to work in, and the
judgement calls. The goal: a `<piece>-merged.mxl` that Helen can open in
Sibelius with every bar as printed. Derive everything from the page. Patch by
hand only what no tool can read, and never guess.

## Ground rules

- **Music never reaches git.** The PDFs and everything made from them
  (MusicXML, `merge.json`, `glyphs.json`, reports, pictures) live in
  `library/<piece>/`, which is gitignored. Never in `tmp/`, never anywhere
  tracked. A test uses music it generates (`tests/omr/musicxml_maker.py`,
  `tests/omr/vector_maker.py`), never a piece.
- Audiveris is in the image only with `WITH_AUDIVERIS=1`. If `process.py`
  can't find it, tell Helen; don't install it.
- Every step writes a report. Read it in full before moving on.
- Before you change a tool or `merge.json` on a piece that already has
  output, copy `omr/<piece>-merged.mxl` to `library/<piece>/<name>.mxl`.
  Then run again, and run `python3 tools/omr/score.py diff <copy> <new>`.
  Look at every bar it lists, on the page.

## 1. The pages

Helen puts the PDFs (or page images) in `library/<piece>/`. Pages are taken
in the order of the last number in each file name (`score_2` before
`score_10`). Ask her which pages and which
piece if it isn't obvious from what's there. Don't go looking outside
`library/`.

Find out whether the PDF is a **drawing** (exported from notation software)
or a **scan**:

    python3 tools/omr/vector.py show library/<piece> 1

Staves listed: a drawing, and the drawing check below applies. "no staves",
or nothing: a scan. A scan reads less well; see "Scans" at the end.

## 2. Name the shapes (drawings only)

    python3 tools/omr/vector.py survey library/<piece>

This writes `library/<piece>/glyphs.json`, every distinct shape in the PDFs
with a guessed name, and `library/<piece>/omr/views/glyphs.png`, one of each,
numbered in the same order. Look at the picture (Read shows images). Then
correct each wrong `"name"` in `glyphs.json`. The names the code knows are
listed in `tools/omr/vector.py`'s docstring.

- Get the common shapes right first: noteheads, rests, flags, dots,
  accidentals, clefs. A misnamed notehead breaks every bar it is in.
- `dot` is an augmentation dot (beside a head); `dot-small` is a staccato
  (above or below one). Name marks too: `accent`, `marcato`, `tenuto`,
  `open` (the hi-hat circle).
- Text, dynamics, slurs, numbers and anything you can't tell stay `other`.
  Most shapes are `other`, and that is fine.
- When the picture is too small to tell, look at an example in place:
  `vector.py picture`, as in step 5.

## 3. Read

    python3 tools/omr/process.py library/<piece>

This runs Audiveris twice: at 300 DPI, then sharper. That takes minutes per
page (about 20 for a 23-page score), so run it in the background. It writes
everything to `library/<piece>/omr/` (`process.py --help` lists the files).
Audiveris is not run again unless you pass `--force`. Later runs only redo
the merge and checks, which is far quicker.

## 4. Which part is which instrument

Audiveris splits each instrument into a part per page, under names it often
misreads. `process.py` drafts the mapping as `omr/merge.draft.json`, with:

- the instruments in order;
- each fragment assigned to one;
- `time`, `transpose`, `octave_clefs`, and `key_changes` that Audiveris
  lost.

Its `_check` list holds the fragments it placed by position alone, or whose
name disagreed. Check each one against the page and `omr/names.txt`. Then
save the table as `library/<piece>/merge.json`.

Ask Helen, don't decide:
- **`combine`:** how many staves her Sibelius template has, and which parts
  share one. `merge-report.txt` lists doublings, which are good candidates.
- Anything on the page you can't read with confidence: an instrument's
  name, a transposition, a key change.

`tools/omr/README.md` has the table of every `merge.json` key.

## 5. Merge, then check

    python3 tools/omr/process.py library/<piece>

Then read `omr/merge-report.txt` from top to bottom:

- **Bars that do not add up** (the last line of `process.py`'s output, and
  the check at the end of the report): must be 0.
- **drawing check:** every difference between the drawing and the read,
  and what was done. Lines ending `FIXED` were corrected from the page.
  Check a handful anyway: wrong noteheads in `glyphs.json` show up here as
  confident nonsense. Lines that don't end `FIXED` are bars the drawing
  can't read (tuplets, grace notes), or bars the stave matching missed.
  Look at every one.
- **drawn staves matched to parts:** which stave on each page is which
  part. If a part is matched to the wrong stave, its whole page goes wrong.
- **reads DISAGREE / neither read adds up:** bars to look at when the piece
  is a scan.
- **BELOW THE INSTRUMENT'S RANGE:** usually a stave read an octave low
  (`octave_clefs`).
- **lyrics dropped as unreadable:** text Audiveris read badly; check that no
  real lyric is among them.

To see a bar as printed:

    python3 tools/omr/vector.py picture library/<piece> <page> <stave> <first bar> <last bar>

Page counts from 1, stave from 0 (top down), bars on the page from 0. The
"drawn staves matched to parts" lines give each page's bar numbers and
staves. The PNG goes to `omr/views/bars/`; look at it with Read. To see what
the score holds for that bar, write a small script in `tmp/` that loads
`omr/<piece>-merged.mxl` with `score.load` and prints the measure.

## 6. What is left

For each bar still wrong, decide where the fix belongs:

- **The same misread could happen in another piece:** fix the tool. Add a
  test on generated music that fails without the fix. Then diff the whole
  piece before and after, as in the ground rules, and check every bar the
  diff lists on the page. A fix that helps one bar and quietly breaks three
  others is common.
- **Only this piece, and no tool could read it:** a hand patch in
  `merge.json` (`"patches"`, syntax in `python3 tools/omr/repair.py
  --help`). Examples: a tuplet, a grace note, a word Audiveris never read.
  Give each patch a `"_page"` note saying what is printed. Patch only bars
  you have looked at.

Things the tools cannot do yet:
- read tuplets or grace notes from the drawing;
- read text from the drawing (the PDF draws its letters as shapes), so
  lyrics are only what Audiveris read;
- read fingered tremolos from a scan.

Say which of these you met.

## 7. Report to Helen

Follow the end-of-work list in `CLAUDE.md`, and add:
- where the merged score is;
- bars that don't add up (0, or which and why);
- what the drawing check fixed;
- every bar you checked by eye, and what you found;
- each hand patch and why no tool could do it;
- anything you were unsure of, with a picture in `omr/views/bars/` she can
  open.

## Scans

There is no drawing to check against, so the second read and doublings
(parts that play the same line) are the only second opinions. Expect more
bars that don't add up, and more patches. Drum parts suffer most:
`"drum_grooves"` in `merge.json` writes a whole groove into every bar whose
kick and snare match (`repair.py --help`). Look at every bar you patch.
