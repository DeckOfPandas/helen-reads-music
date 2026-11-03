# Reading sheet music (OMR)

Sheet-music PDFs to MusicXML with Audiveris, which the image carries when
`WITH_AUDIVERIS=1`. Every score and everything made from one stays in the
gitignored `library/`.

    python3 tools/omr/process.py library/<piece>          # read a piece
    python3 tools/omr/process.py library/<piece> --force  # read it again
    python3 tools/omr/process.py library/<piece> --single # one 300 DPI read only

A piece is a folder of PDFs, page images, or (folder name containing
"spread") screenshots of two-page spreads. The output goes in
`library/<piece>/omr/`; `process.py --help` lists every file.

## Two reads

A dense score read at 300 DPI has its staff lines about 12 px apart, just
over the ~11 px Audiveris accepts. There it drops small symbols: quaver
rests, and the time signature, without which it cannot correct its own
rhythms. So each piece is read again at the resolution that puts the lines
about 18 px apart. Some page layouts crash Audiveris at one resolution and
not another, so it steps down (17, 16, 15 px) until a read succeeds. The
sharper read is the primary, and `merge.json` is written for it. Where one
of its bars does not add up, the bar comes from the 300 DPI read if that
one's does (`combine.py`, which matches that read's parts itself).

## Merging the parts Audiveris split

On a score of several pages Audiveris makes one instrument into many parts,
one per page or per name it read. Which parts are one instrument is your
call:

1. Read `omr/names.txt` (the primary read's parts) beside the score's own
   list of instruments.
2. Copy `omr/merge.template.json` to `library/<piece>/merge.json`. List the
   real instruments in `canonical_order` and map each part to one of them, by
   name, or by part id (`"P23": "Marimba"`) where one name covers several
   staves or OCR garbled it.
3. Run `process.py` again. Audiveris is not run again.

`merge.json` also carries the repairs (`repair.py --help`):

| Key | Does |
|---|---|
| `"time": "4/4"` | Writes the time signature where Audiveris missed it, and sets whole-bar rests to it |
| `"transpose": {"Clarinet in Bb": "Bb"}` | Marks transposing instruments (Bb, A, F, Eb, 8va, 8vb) and gives them their written key |
| `"octave_clefs": false` | The score has no octave clefs: undoes treble clefs Audiveris read as "8 below" |
| `"drum_grooves": {...}` | Writes whole kit grooves (hi-hats included, which Audiveris misses) into the bars whose kick and snare match, or into listed bars; see `repair.py --help` |
| `"borrow_rests": false` | Turns off putting back rests a short bar is missing, copied from a matching bar elsewhere |

## Checking what came out

`omr/merge-report.txt` says what the merge, the repairs and the second read
did, then checks the result: every bar whose notes and rests do not add up
to the time signature, every note below its instrument's range, and every
bar the two reads got right but differently. Those are the bars to look at.
`omr/<piece>-merged.mxl` is the file to open.

## The tools

| File | Does |
|---|---|
| `process.py` | The whole run for a piece |
| `audiveris.py` | Runs Audiveris headless, state in `tmp/audiveris-home/` |
| `pages.py` | `merge`, `images`, `rerender`, `split`: getting pages into one readable PDF |
| `score.py` | `check` and `names` for any `.mxl` |
| `merge.py` | The merge, on its own |
| `repair.py` | The repairs, on their own |
| `combine.py` | Matching the second read and filling bars from it |

Each one's `--help` (or its docstring) says what it does and why. Tests are
in `tests/omr/`, on music they generate.
