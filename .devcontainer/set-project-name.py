"""Set PROJECT_NAME in kit.env and every place devcontainer.json repeats it.

USAGE (from the repo root):  python3 .devcontainer/set-project-name.py <name>

The name becomes the image, container and volume names, so it must be lowercase
letters, digits, `.`, `_` and `-`, starting with a letter or digit.
tests/kit/test_kit_config.py checks the two files agree.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

KIT_ENV = Path(".devcontainer/kit.env")
DEVCONTAINER = Path(".devcontainer/devcontainer.json")
VALID = re.compile(r"[a-z0-9][a-z0-9_.-]*")
KEY = re.compile(r"^PROJECT_NAME=(.*)$", re.MULTILINE)


def current_name(kit_env: str) -> str:
    match = KEY.search(kit_env)
    if not match:
        raise SystemExit("set-project-name.py: kit.env has no PROJECT_NAME= line")
    return match.group(1).strip().strip("\"'")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not VALID.fullmatch(argv[1]):
        print(__doc__, file=sys.stderr)
        return 2
    new = argv[1]
    kit_env = KIT_ENV.read_text(encoding="utf-8")
    old = current_name(kit_env)
    KIT_ENV.write_text(KEY.sub(f"PROJECT_NAME={new}", kit_env, count=1), encoding="utf-8")

    # Only whole names are replaced: `old` followed by the suffixes this file
    # uses, or as the display name. A bare replace would rewrite substrings.
    devcontainer = DEVCONTAINER.read_text(encoding="utf-8")
    pattern = re.compile(rf'(?<![a-z0-9_.-]){re.escape(old)}(?=-devcontainer|-claude-config|-bundle-cache| \(sandboxed\))')
    DEVCONTAINER.write_text(pattern.sub(new, devcontainer), encoding="utf-8")
    print(f"PROJECT_NAME: {old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
