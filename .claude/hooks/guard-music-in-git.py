#!/usr/bin/env python3
"""Refuse to put a score, or anything made from one, into git.

The music in library/ is licensed to the user, not ours to publish, and a
push sends it to GitHub. .gitignore keeps it out of a plain `git add`; this
hook stops the ways round that:

- `git add` / `git stage` naming a music file (.pdf, .mxl, .musicxml, .omr,
  any case) or anything under a `library` folder;
- `git add -f` / `--force` (alone or in a bundle such as `-Af`), which adds
  ignored files: in this repo that is how music would get in;
- `git commit` while the index holds a music or library/ file, however it got
  staged; with `-a` / `--all`, also the tracked files it would stage.

The same checks apply to `sh scripts/git-nested.sh <dir> add|commit ...`,
judged in that nested clone.

Quoted paths count: a quoted file name is still a file name. Heredoc bodies
are blanked, so a script or commit message may discuss these files.

Invoked as `python3 <path>` so it needs no execute bit. Tested in
`tests/kit/test_music_guard.py`.
"""
from __future__ import annotations

import json
import pathlib
import re
import shlex
import subprocess
import sys

MUSIC_SUFFIXES = {".pdf", ".mxl", ".musicxml", ".omr"}
LIBRARY = "library"

HEREDOC = re.compile(
    r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1.*?^\2$",
    re.DOTALL | re.MULTILINE,
)
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|\n()]")

# git's global options that take a separate value (`git -c k=v add`).
GIT_OPTS_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}
ADD_VERBS = {"add", "stage"}


def is_music(path: str) -> bool:
    p = pathlib.PurePosixPath(path.strip())
    if p.suffix.lower() in MUSIC_SUFFIXES:
        return True
    return LIBRARY in p.parts


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _parse(tokens: list[str]) -> tuple[str, str | None, list[str]] | None:
    """(verb, repo dir or None, arguments) for a git or git-nested.sh call."""
    if not tokens:
        return None
    if (len(tokens) >= 4 and tokens[0] == "sh"
            and tokens[1].endswith("scripts/git-nested.sh")):
        return tokens[3], tokens[2], tokens[4:]
    i = 0
    while i < len(tokens) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[i]):
        i += 1                      # leading NAME=value assignments
    if i >= len(tokens) or tokens[i].rsplit("/", 1)[-1] != "git":
        return None
    i += 1
    directory = None
    while i < len(tokens) and tokens[i].startswith("-"):
        opt = tokens[i]
        if opt in GIT_OPTS_WITH_VALUE and i + 1 < len(tokens):
            if opt == "-C":
                directory = tokens[i + 1] if directory is None else str(
                    pathlib.Path(directory) / tokens[i + 1])
            i += 2
        else:
            i += 1
    if i >= len(tokens):
        return None
    return tokens[i], directory, tokens[i + 1:]


def _add_problems(args: list[str]) -> list[str]:
    problems = []
    options_over = False
    for arg in args:
        if not options_over and arg == "--":
            options_over = True
            continue
        if not options_over and arg.startswith("--"):
            if arg == "--force":
                problems.append("--force (adds files .gitignore keeps out)")
            continue
        if not options_over and arg.startswith("-") and len(arg) > 1:
            if "f" in arg[1:]:
                problems.append(f"{arg} (forces past .gitignore)")
            continue
        if is_music(arg):
            problems.append(arg)
    return problems


def _git_lines(directory: pathlib.Path, *args: str) -> list[str]:
    try:
        done = subprocess.run(
            ["git", *args], cwd=directory, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [line for line in done.stdout.split("\0") if line]


def _commit_problems(directory: pathlib.Path, args: list[str]) -> list[str]:
    if not directory.is_dir():
        return []
    files = _git_lines(directory, "diff", "--cached", "--name-only", "-z")
    commit_all = any(a in ("-a", "--all") or (
        a.startswith("-") and not a.startswith("--") and "a" in a[1:]
        and not a.startswith(("-m", "-F", "-c", "-C"))) for a in args)
    if commit_all:
        files += _git_lines(directory, "diff", "--name-only", "-z")
    return sorted({f for f in files if is_music(f)})


def _deny(reason: str) -> int:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0                    # nothing to judge; never break the tool call

    command = (payload.get("tool_input") or {}).get("command") or ""
    command = HEREDOC.sub(" ", command)
    cwd = pathlib.Path.cwd()

    for segment in SEGMENT_SPLIT.split(command):
        parsed = _parse(_tokens(segment))
        if parsed is None:
            continue
        verb, raw_dir, args = parsed
        directory = cwd / raw_dir if raw_dir else cwd
        if verb in ADD_VERBS:
            problems = _add_problems(args)
        elif verb == "commit":
            problems = _commit_problems(directory, args)
        else:
            continue
        if not problems:
            continue
        listed = "\n".join(f"  {p}" for p in problems)
        return _deny(
            f"BLOCKED: `git {verb}` would put music into git:\n{listed}\n\n"
            "CLAUDE.md: music never reaches git. Scores and everything made "
            "from them (PDF, MusicXML, .mxl, .omr, merge configs, reports) "
            "live in the gitignored library/ folder. They are licensed to the "
            "user and a push would publish them.\n\n"
            "WHAT TO DO INSTEAD: stage the code files by name and leave the "
            "music where it is. If a music file is already staged, unstage it "
            "with `git rm --cached -- <file>` (the file stays on disk). A test "
            "fixture is generated by the test at run time, never committed.\n\n"
            "If you believe a particular file genuinely belongs in git, say so "
            "and let the human decide."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
