# Claude Code Rules for Helen Reads Music

@.claude/kit/RULES.md

## This project
The rules above come from the kit and change only through `/kit-sync`. This
project's own rules go here. A rule that deliberately differs from the kit's
says so: "this project overrides the kit's <rule>: …".

- Tests: `python3 -m pytest -q`. Read a piece of music: the
  `read-sheet-music` skill has the steps (`tools/omr/README.md` the tools).
- **Music never reaches git.** Every score, and everything made from one
  (MusicXML, `.mxl`, `.omr`, merge configs, reports), lives in the gitignored
  `library/<piece>/`, never in `tmp/` and never anywhere tracked. The scores
  are licensed to me; a push publishes them. Tests use public-domain or
  generated music, made at run time. `guard-music-in-git.py` enforces it,
  wired in `.claude/settings.project.json`.
