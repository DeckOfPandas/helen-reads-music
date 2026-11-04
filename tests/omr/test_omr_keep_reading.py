"""tools/omr/keep_reading.py: run process.py again after interruptions, not after failures."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

import keep_reading  # noqa: E402


class Runs:
    """Stands in for subprocess.run: returns each exit status in turn."""

    def __init__(self, *codes):
        self.codes, self.calls = list(codes), []

    def __call__(self, command, stdout=None, stderr=None):
        self.calls.append(command[2:])
        stdout.write(f"printed by run {len(self.calls)}\n")
        return SimpleNamespace(returncode=self.codes.pop(0))


def test_interruptions_are_run_again_without_force(tmp_path):
    runs, waits = Runs(3, -9, 137, 0), []
    code = keep_reading.keep_reading(tmp_path, ["--time", "2/2", "--force"], run=runs, sleep=waits.append)
    assert code == 0 and len(waits) == 3
    assert runs.calls[0] == [str(tmp_path), "--time", "2/2", "--force"]
    assert runs.calls[1:] == [[str(tmp_path), "--time", "2/2"]] * 3
    assert (tmp_path / "omr" / "keep-reading.status").read_text().startswith("done")
    log = (tmp_path / "omr" / "keep-reading.log").read_text()
    assert "printed by run 4" in log and "run 2 ended" in log


def test_a_real_failure_is_not_retried(tmp_path):
    runs = Runs(1)
    assert keep_reading.keep_reading(tmp_path, [], run=runs, sleep=lambda s: None) == 1
    assert len(runs.calls) == 1
    assert (tmp_path / "omr" / "keep-reading.status").read_text().startswith("failed")


def test_it_gives_up_after_max_runs(tmp_path):
    runs = Runs(3, 3)
    assert keep_reading.keep_reading(tmp_path, [], max_runs=2, run=runs, sleep=lambda s: None) == 1
    assert (tmp_path / "omr" / "keep-reading.status").read_text().startswith("gave up")


def test_main_wants_a_piece_folder(tmp_path):
    assert keep_reading.main([]) == 2
    assert keep_reading.main([str(tmp_path / "missing")]) == 2
