from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def kit_root() -> Path:
    return KIT_ROOT


def run_hook(hook: Path, command: str, cwd: Path) -> dict | None:
    """Feed a Bash PreToolUse payload to a hook; its JSON verdict, or None."""
    done = subprocess.run(
        ["python3", str(hook)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True, cwd=cwd, timeout=30,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(cwd)},
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout) if done.stdout.strip() else None


def denied(verdict: dict | None) -> bool:
    return bool(verdict) and verdict["hookSpecificOutput"]["permissionDecision"] == "deny"
