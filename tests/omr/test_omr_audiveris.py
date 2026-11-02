"""tools/omr/audiveris.py: state under tmp/, the right flags, and a live run."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "omr"))

import audiveris  # noqa: E402


def test_home_is_the_projects_tmp():
    assert audiveris.DEFAULT_HOME == ROOT / "tmp" / "audiveris-home"
    env = audiveris.environment(audiveris.DEFAULT_HOME)
    assert all(value.startswith(str(ROOT / "tmp")) for value in env.values())


def test_command_runs_the_bundled_java_headless():
    cmd = audiveris.command(["-batch", "x.pdf"])
    assert cmd[0] == "/opt/audiveris/lib/runtime/bin/java"
    assert "-Djava.awt.headless=true" in cmd and "-Dsun.java2d.uiScale=1" in cmd
    assert cmd[cmd.index("-cp") + 1] == "/opt/audiveris/lib/app/*"
    assert cmd[-3:] == ["Audiveris", "-batch", "x.pdf"]


def test_print_command_runs_nothing(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(audiveris, "DEFAULT_HOME", tmp_path / "home")
    assert audiveris.main(["--print-command", "--", "-help"]) == 0
    out = capsys.readouterr().out
    assert f"HOME={tmp_path / 'home'}" in out and out.rstrip().endswith("Audiveris -help")
    assert not (tmp_path / "home").exists()


def test_arguments_must_follow_a_double_dash(capsys):
    assert audiveris.main(["-help"]) == 2


def test_prepare_home_copies_the_model_once(tmp_path, monkeypatch):
    model = tmp_path / "eng.traineddata"
    model.write_bytes(b"model")
    monkeypatch.setattr(audiveris, "TESSDATA_SOURCE", model)
    home = tmp_path / "home"
    audiveris.prepare_home(home)
    copied = audiveris.tessdata_dir(home) / "eng.traineddata"
    assert copied.read_bytes() == b"model"
    copied.write_bytes(b"kept")
    audiveris.prepare_home(home)
    assert copied.read_bytes() == b"kept"


@pytest.mark.skipif(not audiveris.JAVA.is_file(), reason="Audiveris is not in this image (WITH_AUDIVERIS=0)")
def test_audiveris_starts_headless_with_ocr_in_place(tmp_path):
    log = tmp_path / "log.txt"
    with open(log, "w") as handle:
        code = audiveris.run(["-help"], home=tmp_path / "home", log=handle)
    text = log.read_text()
    assert code == 0, text
    assert "-batch" in text and "-export" in text
    assert (audiveris.tessdata_dir(tmp_path / "home") / "eng.traineddata").stat().st_size > 10_000_000
