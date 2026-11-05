#!/usr/bin/env python3
"""Run the Audiveris in /opt/audiveris headless, with its state under tmp/.

USAGE (from the repo root):
    python3 tools/omr/audiveris.py [--print-command] -- <Audiveris arguments>
e.g.
    python3 tools/omr/audiveris.py -- -batch -export -save -output library/x/omr -- library/x/omr/work/x.pdf

It calls the bundled Java directly rather than /opt/audiveris/bin/Audiveris:
that launcher is a jpackage binary that is not guaranteed to pass -D flags on
to the JVM.

  -Djava.awt.headless=true   there is no display
  -Dsun.java2d.uiScale=1     skips Audiveris's GTK HiDPI probe, which throws
                             UnsatisfiedLinkError with no display even when
                             libgtk-3 is installed

Audiveris keeps its settings, logs and OCR models under XDG_CONFIG_HOME, and
writes there on every run. HOME and the XDG folders point at
tmp/audiveris-home/ so nothing lands outside the project. So does Java's
user.home (-Duser.home): Java reads it from the user database, not $HOME, and
the bundled Tesseract and Leptonica unpack their native libraries into
<user.home>/.javacpp/cache. It reads OCR models
from $XDG_CONFIG_HOME/AudiverisLtd/audiveris/tessdata (not XDG_DATA_HOME);
the full-tessdata English model is copied there from /opt/audiveris-tessdata.
The tessdata_fast model would leave OCR silently off.

--print-command prints the java command line and the environment changes
without running anything (or creating the home folder).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUDIVERIS = Path("/opt/audiveris")
JAVA = AUDIVERIS / "lib" / "runtime" / "bin" / "java"
APP_JARS = AUDIVERIS / "lib" / "app"
TESSDATA_SOURCE = Path("/opt/audiveris-tessdata") / "eng.traineddata"
DEFAULT_HOME = ROOT / "tmp" / "audiveris-home"
HEAP = os.environ.get("AUDIVERIS_HEAP", "6G")


def environment(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
    }


def tessdata_dir(home: Path) -> Path:
    return home / "config" / "AudiverisLtd" / "audiveris" / "tessdata"


def command(args: list[str], home: Path = DEFAULT_HOME) -> list[str]:
    return [
        str(JAVA),
        "-Djava.awt.headless=true",
        "-Dsun.java2d.uiScale=1",
        f"-Duser.home={home}",
        "-Xms512m", f"-Xmx{HEAP}",
        "-cp", f"{APP_JARS}/*",
        "Audiveris", *args,
    ]


def prepare_home(home: Path) -> None:
    env = environment(home)
    for key in ("XDG_DATA_HOME", "XDG_CACHE_HOME"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    tess = tessdata_dir(home)
    tess.mkdir(parents=True, exist_ok=True)
    target = tess / TESSDATA_SOURCE.name
    if not target.is_file() and TESSDATA_SOURCE.is_file():
        target.write_bytes(TESSDATA_SOURCE.read_bytes())


def run(args: list[str], home: Path = DEFAULT_HOME, log=None) -> int:
    """Run Audiveris; its output to `log` (an open file) or inherited. Exit code."""
    import subprocess
    if not JAVA.is_file():
        sys.exit(f"audiveris.py: no Java at {JAVA}. Is WITH_AUDIVERIS=1 in this image?")
    prepare_home(home)
    env = {**os.environ, **environment(home)}
    return subprocess.run(command(args, home), env=env, cwd=ROOT, stdout=log,
                          stderr=subprocess.STDOUT if log else None).returncode


def main(argv: list[str]) -> int:
    print_only = False
    if argv and argv[0] == "--print-command":
        print_only, argv = True, argv[1:]
    if not argv or argv[0] != "--":
        print(__doc__, file=sys.stderr)
        return 2
    args = argv[1:]
    if print_only:
        for key, value in environment(DEFAULT_HOME).items():
            print(f"{key}={value}")
        print(" ".join(command(args, DEFAULT_HOME)))
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
