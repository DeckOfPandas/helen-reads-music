"""tools/dev/: the scripts parse, and refuse bad arguments before needing a browser.

Taking a real screenshot needs WITH_PLAYWRIGHT=1, so it is not tested here.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.mark.parametrize("script", ["shoot.js", "slice-png.js", "crop.js", "styles.js"])
def test_parses(kit_root, script):
    subprocess.run([NODE, "--check", str(kit_root / "tools" / "dev" / script)], check=True)


@pytest.mark.parametrize("script,args", [
    ("shoot.js", []),
    ("shoot.js", ["http://127.0.0.1:9000"]),
    ("shoot.js", ["http://127.0.0.1:9000", "a/b"]),
    ("shoot.js", ["http://127.0.0.1:9000", "x", "--scheme", "sepia"]),
    ("shoot.js", ["http://127.0.0.1:9000", "x", "--click"]),
    ("shoot.js", ["http://127.0.0.1:9000", "x", "--type"]),
    ("shoot.js", ["http://127.0.0.1:9000", "x", "--engine", "firefox"]),
    ("shoot.js", ["http://127.0.0.1:9000", "x", "/about/"]),
    ("crop.js", []),
    ("crop.js", ["http://127.0.0.1:9000", "x"]),
    ("crop.js", ["http://127.0.0.1:9000", "a/b", "h1"]),
    ("crop.js", ["http://127.0.0.1:9000", "x", "h1", "/about/"]),
    ("crop.js", ["http://127.0.0.1:9000", "x", "h1", "a/", "b/"]),
    ("crop.js", ["http://127.0.0.1:9000", "x", "h1", "--width", "wide"]),
    ("crop.js", ["http://127.0.0.1:9000", "x", "h1", "--width", "50"]),
    ("crop.js", ["http://127.0.0.1:9000", "x", "h1", "--engine", "firefox"]),
    ("crop.js", ["http://127.0.0.1:9000", "x", "h1", "--type-into", ".box"]),
    ("crop.js", ["http://127.0.0.1:9000", "x", "h1", "--out", "/etc/x.png"]),
    ("styles.js", []),
    ("styles.js", ["http://127.0.0.1:9000"]),
    ("styles.js", ["http://127.0.0.1:9000", "h1", "/about/"]),
    ("styles.js", ["http://127.0.0.1:9000", "h1", "a/", "b/"]),
    ("styles.js", ["http://127.0.0.1:9000", "h1", "--width", "0"]),
    ("styles.js", ["http://127.0.0.1:9000", "h1", "--props", "font-size;x"]),
    ("styles.js", ["http://127.0.0.1:9000", "h1", "--props"]),
    ("styles.js", ["http://127.0.0.1:9000", "h1", "--engine", "firefox"]),
    ("slice-png.js", []),
    ("slice-png.js", ["shot.png"]),
    ("slice-png.js", ["tmp/../x.png"]),
    ("slice-png.js", ["tmp/x.jpg"]),
    ("slice-png.js", ["tmp/x.png", "10"]),
])
def test_refuses(kit_root, tmp_path, script, args):
    done = subprocess.run([NODE, str(kit_root / "tools" / "dev" / script), *args],
                          cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert done.returncode == 2, done.stderr
    assert "usage" in done.stderr or "must be" in done.stderr, done.stderr


@pytest.mark.parametrize("script,args", [
    ("crop.js", ["http://127.0.0.1:9", "x", "h1"]),
    ("crop.js", ["http://127.0.0.1:9", "x", ".a b", "about/", "--width", "390", "--engine", "webkit",
                 "--type-into", "input", "ch"]),
    ("styles.js", ["http://127.0.0.1:9", "h1", ".", "--props", "font-size,line-height", "--width", "390"]),
])
def test_accepts(kit_root, tmp_path, script, args):
    """Past the argument checks: it fails later, on no browser or no server."""
    done = subprocess.run([NODE, str(kit_root / "tools" / "dev" / script), *args],
                          cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert done.returncode != 2 and "usage" not in done.stderr, done.stderr
