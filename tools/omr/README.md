# Reading sheet music (OMR)

Sheet-music PDFs to MusicXML with Audiveris, which the image carries when
`WITH_AUDIVERIS=1`. Every score and everything made from one stays in the
gitignored `library/`.

    python3 tools/omr/process.py library/<piece>          # read a piece
    python3 tools/omr/process.py library/<piece> --force  # read it again

A piece is a folder of PDFs, page images, or (folder name containing
"spread") screenshots of two-page spreads. The output goes in
`library/<piece>/omr/`; `process.py --help` lists every file.

## Checking what came out

`report.txt` lists each part and every bar whose notes and rests do not add
up to the time signature. Those bars are where Audiveris dropped or invented
something: fix them first. A part marked "concert pitch (no `<transpose>`)"
holds the pitches as printed, so a transposing instrument's part reads in
its written key.

## Merging the parts Audiveris split

On a score of several pages Audiveris often makes one instrument into many
parts, one per name it read. Which names are one instrument is your call:

1. Read `omr/names.txt` beside the score's own list of instruments.
2. Copy `omr/merge.template.json` to `library/<piece>/merge.json`. List the
   real instruments in `canonical_order` and set each alias to one of them,
   or `null` to leave that part alone.
3. Run `process.py` again. Audiveris is not run again; the merge writes
   `omr/<piece>-merged.mxl` and checks it, or refuses and says why in
   `omr/merge-report.txt`.

## The tools

| File | Does |
|---|---|
| `process.py` | The whole run for a piece |
| `audiveris.py` | Runs Audiveris headless, state in `tmp/audiveris-home/` |
| `pages.py` | `merge`, `images`, `rerender`, `split`: getting pages into one readable PDF |
| `score.py` | `check` and `names` for any `.mxl` |
| `merge.py` | The merge, on its own |

Each one's `--help` (or its docstring) says what it does and why. Tests are
in `tests/omr/`, on music they generate.
