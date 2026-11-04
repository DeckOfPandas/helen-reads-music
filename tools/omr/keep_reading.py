#!/usr/bin/env python3
"""Run process.py on one piece until it finishes, for a long read left alone.

USAGE (from the repo root):
    python3 tools/omr/keep_reading.py library/<piece> [process.py options]

process.py keeps every chunk Audiveris finishes (book.py), so a run that is
stopped loses only the chunk it was on. This runs it again after each
interruption, waiting first for memory to recover:
  - exit 3: Audiveris was killed (the system short of memory);
  - killed itself (exit status 128 or more, or by a signal).
A real failure (exit 1: nothing read, a bad merge.json) is not retried.
--force is passed on the first run only: a retry carries on rather than
starting again. It gives up after MAX_RUNS runs.

It writes, in library/<piece>/omr/:
  keep-reading.status   one line: running (run N, since when), done, failed
                        or gave up, with the time; a reader polls this
  keep-reading.log      everything process.py printed, run by run

To leave it running past this shell (a Claude Code session can be stopped
under memory pressure), start it in its own session:
    setsid --fork python3 tools/omr/keep_reading.py library/<piece> ...
If the container itself stops, start it again the same way, without
--force: it carries on from the chunks already read.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_RUNS = 20
WAIT_SECONDS = 120
INTERRUPTED = 3


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def keep_reading(piece: Path, options: list[str], max_runs: int = MAX_RUNS,
                 wait: float = WAIT_SECONDS, run=subprocess.run, sleep=time.sleep) -> int:
    out = piece / "omr"
    out.mkdir(parents=True, exist_ok=True)
    status, log = out / "keep-reading.status", out / "keep-reading.log"
    for number in range(1, max_runs + 1):
        args = options if number == 1 else [o for o in options if o != "--force"]
        status.write_text(f"running (run {number} of at most {max_runs}, since {_stamp()})\n")
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"===== run {number}, {_stamp()}: process.py {piece} {' '.join(args)}\n")
            handle.flush()
            code = run([sys.executable, str(HERE / "process.py"), str(piece), *args],
                       stdout=handle, stderr=subprocess.STDOUT).returncode
            handle.write(f"===== run {number} ended {_stamp()} with exit status {code}\n")
        if code == 0:
            status.write_text(f"done {_stamp()} after {number} run(s); see keep-reading.log\n")
            return 0
        if code != INTERRUPTED and 0 < code < 128:
            status.write_text(f"failed {_stamp()} (exit {code}); see keep-reading.log\n")
            return code
        status.write_text(f"interrupted {_stamp()} (exit {code}); run {number + 1} in {wait:.0f} s\n")
        sleep(wait)
    status.write_text(f"gave up {_stamp()} after {max_runs} interrupted runs; see keep-reading.log\n")
    return 1


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-") or not Path(argv[0]).is_dir():
        print(__doc__, file=sys.stderr)
        return 2
    return keep_reading(Path(argv[0]), argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
