# Reading sheet music (OMR)

Sheet-music PDFs to MusicXML with Audiveris, which the image carries when
`WITH_AUDIVERIS=1`. Every score and everything made from one stays in the
gitignored `library/`. The steps for reading a new piece, in order, are in
the `read-sheet-music` skill (`.claude/skills/read-sheet-music/SKILL.md`).

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

## Reading in chunks

One page Audiveris crashes on stops it exporting the whole book, and a
whole book is held in memory. So `book.py` reads five pages at a time, each
chunk its own book, and joins the chunks into one score shaped as a single
read would be. A page that crashes is drawn 7% smaller and read again. A
chunk that still fails is halved until only the bad page is left, and that
page is reported LOST. Finished chunks are kept in `omr/chunks/`, so a run
that is stopped carries on where it left off. `keep_reading.py` reruns
`process.py` after each interruption, for reads left unattended.

## Reading the PDF's own drawing

A PDF exported from notation software is a drawing, not pixels: every
notehead, beam, rest and tie is an exact shape at an exact place. Where the
PDFs are such drawings, the drawing is the check on Audiveris, and the
fix for most of its mistakes. Once per piece, name the shapes:

    python3 tools/omr/vector.py survey library/<piece>

writes `library/<piece>/glyphs.json`, every distinct shape with a guessed
name, and `omr/views/glyphs.png`, one of each numbered. Correct the names
that are wrong (an unnamed shape is `other`), then run `process.py`. After
the merge and repairs, `drawing_check.py` matches each drawn stave to its
part and reads every bar from the drawing (`vector_read.py`); where that
reading adds up and differs from the score, it wins:

- the key drawn after each clef, or at a bar line, is the key;
- a stave's bar with a wrong note, a lost note or wrong rhythm is replaced
  by the drawing's reading, x and slash heads as drawn; each lyric moves to
  the new note at its place on the page, and dynamics keep their beat;
- an accent, marcato, tenuto or staccato goes to the nearest notehead, even
  one on the next stave;
- rests missing from an otherwise right bar are put back;
- ties are set as drawn;
- a fingered tremolo (two minims a beam's strokes apart, together lasting
  one) is written as one;
- a drum bar is written from the drawing, voices as drawn, a voice the page
  leaves without rests padded with invisible ones.

A bar whose drawing does not add up (tuplets, grace notes) is reported, not
changed. Text is drawn as shapes too, and not read: lyrics are what
Audiveris read. Bars patched by hand are left alone; after the patches they
are checked against the drawing, and only their ties are corrected. A
scanned PDF has no drawing, and no `glyphs.json` means no check.

To check a bar by eye, `vector.py picture library/<piece> <page> <stave>
<first bar> <last bar>` writes a PNG of it to `omr/views/bars/`.
`merge-report.txt` says which stave is which part on each page.

## Doublings

Parts that play the same notes (harp and piano, bass guitar and double
bass, a trumpet doubling the oboe, octaves apart or not) check each other.
`doublings.py` finds every pair of staves that agree in most bars where
both play, and lists them in `merge-report.txt`. A bar of one that does
not add up while its double's does is given its double's rhythm. It keeps
its own pitches when it has as many notes, since a reader gets rhythm wrong
far more often than pitch. Otherwise it takes the double's pitches too,
respelt into its own transposition, and the report says to check them.
This runs after the drawing check, so for a score with a drawing there is
usually nothing left for it; for a scan it is the main second opinion.

## Merging the parts Audiveris split

On a score of several pages Audiveris makes one instrument into many parts,
one per page or per name it read. Which parts are one instrument is your
call:

1. `process.py` drafts it: `omr/merge.draft.json` (`draft.py`). The page
   with the most staves sets the instruments and their order; every other
   page's parts are matched to them in order, by name (forgiving OCR), clef
   and number of staves; each instrument is named by a vote of its parts.
   It also proposes `time`, `transpose` (from "in Bb", "in F", piccolo,
   basses), `octave_clefs` and lost `key_changes`. Its `_check` list is
   every part it placed by position alone, or whose name disagreed.
2. Check that list against the score and `omr/names.txt` (the primary
   read's parts). Map a part by name, or by part id (`"P23": "Marimba"`)
   where one name covers several staves; an id wins over its name. Save as
   `library/<piece>/merge.json`.
3. Run `process.py` again. Audiveris is not run again.

`merge.json` also carries the repairs (`repair.py --help`):

| Key | Does |
|---|---|
| `"time": "4/4"` | Writes the time signature where Audiveris missed it, and sets whole-bar rests to it |
| `"transpose": {"Clarinet in Bb": "Bb"}` | Marks transposing instruments (Bb, A, F, Eb, 8va, 8vb) and gives them their written key |
| `"octave_clefs": false` | The score has no octave clefs: undoes treble clefs Audiveris read as "8 below" |
| `"key_changes": {"125": 0}` | A key change Audiveris read but failed to export: writes it into the parts that lack it and respells the notes the old key altered |
| `"patches": {"Oboe": {"121": "1/2 1/2 +r:1/2 ..."}}` | Hand corrections to single bars, for what no tool reads (a tuplet, a grace note, a word Audiveris missed), applied last: edit the existing notes (lengths, re-pitch, insert, delete) so lyrics and marks survive, or replace a voice; fingered tremolos as `F4+Bb4/D5:2`; see `repair.py --help` |
| `"vocal_parts": ["Voice"]` | Parts whose lyrics are lyrics; elsewhere dynamics, accents and instructions Audiveris read as lyrics become marks again (the default is just "Voice") |
| `"drum_key": {"F5 x": 51}` | Extra drum sounds by stave position and notehead; every drum note gets its instrument, hands stems up, feet stems down |
| `"drum_grooves": {...}` | Writes whole kit grooves (hi-hats included, which Audiveris misses) into the bars whose kick and snare match, or into listed bars; see `repair.py --help` |
| `"combine": [{"parts": ["Harp", "Piano"], "name": "Harp & Piano"}]` | Two parts onto one part's staves, to fit a template with fewer staves: the same notes written once, different notes as two voices. It copies written pitch as it is, so both parts must be in the same transposition |
| `"labels": {"Double Bass": {"abbreviation": "D.B.", "program": 44}}` | A part's abbreviation and playback sound (General MIDI program), where the built-in table in `repair.py` gets them wrong |
| `"borrow_rests": false` | Turns off putting back rests a short bar is missing, copied from a matching bar elsewhere |
| `"drawing_check": false` | Turns off checking and correcting against the PDF's drawing |
| `"doublings": false` | Turns off filling bars from the parts that double them |

## Checking what came out

`omr/merge-report.txt` says what the merge, the repairs and the second read
did, then checks the result: every bar whose notes and rests do not add up
to the time signature, every note below its instrument's range, and every
bar the two reads got right but differently. Those are the bars to look at.
`omr/<piece>-merged.mxl` is the file to open; `omr/<piece>-merged.musicxml`
is the same score uncompressed, for Sibelius or anything that wants plain
MusicXML.

Before changing a tool or `merge.json`, copy the merged score aside; after,
`score.py diff <copy> <new>` lists every bar whose notes or marks (lyrics,
articulations, noteheads, directions) changed.

## The tools

| File | Does |
|---|---|
| `process.py` | The whole run for a piece |
| `audiveris.py` | Runs Audiveris headless, state in `tmp/audiveris-home/` |
| `book.py` | Reads a PDF in chunks: retries, splits, resumes, joins |
| `keep_reading.py` | Reruns `process.py` after interruptions, for a read left alone |
| `pages.py` | `merge`, `images`, `rerender`, `split`: getting pages into one readable PDF |
| `score.py` | `check`, `names` and `diff` for any `.mxl` |
| `draft.py` | Drafts `merge.json` from a read |
| `merge.py` | The merge, on its own |
| `repair.py` | The repairs, on their own |
| `combine.py` | Matching the second read and filling bars from it |
| `vector.py` | The PDF's drawing: staves, bar lines, stems, named shapes; `survey` makes `glyphs.json`, `picture` shows bars |
| `vector_read.py` | One bar of one stave read from the drawing: notes, lengths, ties, voices, pitches |
| `drawing_check.py` | The score checked and corrected against the drawing |
| `doublings.py` | Parts that double each other, filling each other's broken bars |

Each one's `--help` (or its docstring) says what it does and why. Tests are
in `tests/omr/`, on music they generate.
