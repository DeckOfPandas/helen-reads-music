#!/usr/bin/env python3
"""Tell a starting session where it actually is, before it assumes.

A bind-mounted checkout can change branch under a running session with no
signal, so the facts go in the first thing a session reads: the checkout's
path, whether this is the container or the bare host, the branch, uncommitted
changes, and the position against `origin/main` as last fetched.

No network call and no fetch: a hook that waits on GitHub delays every start.
It never blocks; anything that fails degrades to a line saying so.

Invoked as `python3 <path>` so it needs no execute bit.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_FALLBACK_ROOT = Path(__file__).resolve().parent.parent.parent


def _git_in(cwd: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def resolve_root(payload: dict) -> Path:
    """The checkout git itself would act on: the hook's `cwd`, then the
    process cwd, then this file's own checkout."""
    candidates = [Path(str(v)) for v in (payload.get("cwd"), os.getcwd()) if v]
    candidates.append(_FALLBACK_ROOT)
    for candidate in candidates:
        if candidate.is_dir():
            top = _git_in(candidate, "rev-parse", "--show-toplevel")
            if top:
                return Path(top)
    return _FALLBACK_ROOT


def _sandbox_line() -> str:
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return "sandbox: inside the container"
    return ("sandbox: NOT in a container -- this session can see the whole "
            "host. Start it with .devcontainer/run.sh")


def _branch_line(root: Path) -> str:
    branch = _git_in(root, "branch", "--show-current")
    if branch is None:
        return "branch: could not be read"
    if not branch:
        return "branch: DETACHED HEAD -- commit nowhere until this is resolved"
    if branch in ("main", "master"):
        return (f"branch: {branch} -- DO NOT COMMIT OR MERGE HERE. Branch first; "
                "guard-main-branch.py will refuse you otherwise")
    return f"branch: {branch}"


def _dirty_line(root: Path) -> str:
    status = _git_in(root, "status", "--porcelain")
    if status is None:
        return "tree: could not be read"
    if not status:
        return "tree: clean"
    n = len(status.splitlines())
    return f"tree: {n} uncommitted change{'s' if n != 1 else ''} already here"


def _position_line(root: Path) -> str:
    counts = _git_in(root, "rev-list", "--left-right", "--count", "origin/main...HEAD")
    if counts is None:
        return "vs origin/main: no origin/main ref -- run sh scripts/git-fetch-main.sh"
    try:
        behind, ahead = (int(x) for x in counts.split())
    except ValueError:
        return "vs origin/main: could not be read"
    if not behind and not ahead:
        return "vs origin/main: level (as last fetched)"
    parts = [f"{ahead} ahead"] if ahead else []
    if behind:
        parts.append(f"{behind} behind")
    tail = " -- sh scripts/git-fetch-main.sh, then git merge origin/main" if behind else ""
    return f"vs origin/main: {', '.join(parts)} (as last fetched){tail}"


def main() -> int:
    try:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, ValueError):
            payload = {}
        root = resolve_root(payload if isinstance(payload, dict) else {})
        lines = [_sandbox_line(), _branch_line(root), _dirty_line(root), _position_line(root)]
        report = f"Where this session actually is -- {root}:\n" + "\n".join(
            f"  {line}" for line in lines
        )
    except Exception as exc:                 # never stop a session starting
        report = f"Ground-truth hook failed harmlessly: {exc!r}"

    print(json.dumps({
        "systemMessage": report,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": report,
        },
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
