"""guard-music-in-git.py and .gitignore keep scores out of git."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import KIT_ROOT, denied, run_hook

HOOK = KIT_ROOT / ".claude" / "hooks" / "guard-music-in-git.py"

MUSIC_FILES = ["score.pdf", "Score.PDF", "part.mxl", "a.musicxml", "book.omr",
               "library/notes.txt", "library/piece/merge.json"]


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "feat")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    return tmp_path


@pytest.mark.parametrize("command", [
    "git add -- score.pdf",
    "git add Score.PDF",
    'git add -- "library/under the sea/3score_0.pdf"',
    "git add -- library",
    "git add -- library/",
    "git add -- ./library/piece/report.txt",
    "git add -- out/part.mxl",
    "git stage x.musicxml",
    "git add -- book.omr",
    "git add -f -- tools/x.py",
    "git add --force .",
    "git add -Af",
    "git -c a=b add -- score.pdf",
    "git status; git add -- score.pdf",
    "sh scripts/git-nested.sh sub add -- score.pdf",
])
def test_adding_music_is_refused(repo, command):
    assert denied(run_hook(HOOK, command, repo)), command


@pytest.mark.parametrize("command", [
    "git add -- tools/omr/run.py",
    "git add -A",
    "git add .",
    "git add -- README.md .gitignore",
    "git status",
    "git log -- score.pdf",
    'git commit -F tmp/msg.txt',
    "sh scripts/git-nested.sh sub add -- notes.md",
    "ls library",
])
def test_ordinary_commands_pass(repo, command):
    assert not denied(run_hook(HOOK, command, repo)), command


@pytest.mark.parametrize("name", MUSIC_FILES)
def test_commit_with_music_staged_is_refused(repo, name):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    git(repo, "add", "-f", "--", name)
    verdict = run_hook(HOOK, "git commit -F tmp/msg.txt", repo)
    assert denied(verdict)
    assert name in verdict["hookSpecificOutput"]["permissionDecisionReason"]


def test_commit_with_only_code_staged_passes(repo):
    (repo / "x.py").write_text("x", encoding="utf-8")
    git(repo, "add", "--", "x.py")
    assert not denied(run_hook(HOOK, "git commit -F tmp/msg.txt", repo))


def test_commit_all_sees_tracked_music_it_would_stage(repo):
    (repo / "old.pdf").write_text("x", encoding="utf-8")
    git(repo, "add", "-f", "--", "old.pdf")
    git(repo, "commit", "-q", "-m", "seed")
    (repo / "old.pdf").write_text("changed", encoding="utf-8")
    assert not denied(run_hook(HOOK, "git commit -F tmp/msg.txt", repo))
    assert denied(run_hook(HOOK, "git commit -a -F tmp/msg.txt", repo))
    assert denied(run_hook(HOOK, "git commit -am x", repo))


def test_nested_commit_is_judged_in_the_nested_clone(repo):
    nested = repo / "sub"
    nested.mkdir()
    git(nested, "init", "-q", "-b", "feat")
    (nested / "s.mxl").write_text("x", encoding="utf-8")
    git(nested, "add", "-f", "--", "s.mxl")
    assert not denied(run_hook(HOOK, "git commit -F tmp/msg.txt", repo))
    assert denied(run_hook(HOOK, "sh scripts/git-nested.sh sub commit -F tmp/msg.txt", repo))


def test_a_heredoc_may_discuss_music_files(repo):
    command = "cat > tmp/m.txt <<EOF\ngit add -- score.pdf\nEOF"
    assert not denied(run_hook(HOOK, command, repo))


@pytest.mark.parametrize("name", MUSIC_FILES + ["deep/dir/x.Mxl"])
def test_gitignore_ignores_music(name):
    done = subprocess.run(["git", "check-ignore", "-q", "--no-index", "--", name],
                          cwd=KIT_ROOT, capture_output=True)
    assert done.returncode == 0, f".gitignore does not ignore {name}"


@pytest.mark.parametrize("name", ["tools/omr/run.py", "README.md", "tests/kit/conftest.py"])
def test_gitignore_leaves_code_alone(name):
    done = subprocess.run(["git", "check-ignore", "-q", "--no-index", "--", name],
                          cwd=KIT_ROOT, capture_output=True)
    assert done.returncode == 1, f".gitignore ignores {name}"


def test_wired_into_settings_without_an_if():
    settings = json.loads((KIT_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    hooks = [h for entry in settings["hooks"]["PreToolUse"] if entry.get("matcher") == "Bash"
             for h in entry["hooks"]]
    wired = [h for h in hooks if h["command"].endswith('/.claude/hooks/guard-music-in-git.py"')]
    assert wired, "guard-music-in-git.py exists but no PreToolUse Bash hook runs it"
    # An `if` of Bash(git *) would miss `sh scripts/git-nested.sh ... add`.
    assert "if" not in wired[0]
