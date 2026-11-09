# Hooks

Each hook is one Python file with no imports beyond the standard library. Each is
run as `python3 <path>`, so none needs an execute bit. All are wired in
`.claude/settings.json`.

| Hook | Event | Does |
|---|---|---|
| `session-ground-truth.py` | SessionStart | Reports the checkout, container or host, branch, dirty tree, and position against `origin/main` |
| `guard-main-branch.py` | PreToolUse Bash | Refuses `git commit` / `git merge` on `main` or `master` |
| `guard-music-in-git.py` | PreToolUse Bash | Refuses `git add` of a `.pdf`/`.mxl`/`.musicxml`/`.omr` or `library/` path, any `git add -f`, and `git commit` while the index holds one; also through `scripts/git-nested.sh` |
| `guard-destructive-git.py` | PreToolUse Bash | Refuses `reset --hard`, `checkout --`, `clean -f`, `restore` on a dirty tree; `stash drop/clear` while stashes exist |
| `guard-token-expansion.py` | PreToolUse Bash | Refuses commands that would print a secret, embed one in a URL, or dump the environment |
| `guard-git-output.py` | PreToolUse Bash | Refuses `--output`, `--no-index`, `--ext-diff` and `--textconv` (and prefixes of them) after `git diff`/`log`/`show`, which the allow list lets run unasked as reads |
| `guard-dev-null.py` | PreToolUse Bash | Refuses `/dev/null` as a command argument (`ls /dev/null`, `curl -o /dev/null`), which `blockReadsOutsideWorkingDirectories` turns into a prompt; a redirection to it passes |
| `guard-unanalyzable-bash.py` | PreToolUse Bash | Refuses what the permission checker cannot read statically: heredocs, `$(...)` and backticks, pipes, `&&`/`\|\|`/`;` chains, a lone `&`, globs, a leading `cd`, `git -C`, a leading `NAME=value`, unquoted parentheses, `$[`, an unquoted word starting with `=`, and a quoted `[`/`]`/`\|` in a `sh scripts/...` call |
| `guard-inline-script.py` | PreToolUse Bash | Refuses `python3 -c`, `node -e`, `ruby -e`, `perl -e`, at any length |
| `guard-sed.py` | PreToolUse Bash | Refuses any `sed` |
| `guard-awk.py` | PreToolUse Bash | Refuses any `awk` / `gawk` / `mawk` / `nawk` |

The last five push Claude towards commands the permission checker can analyse,
so it prompts you less: each refusal says what to do instead, which is
usually a script in `tmp/` with its arguments written out literally. A
command that has to reach outside the working directory becomes a committed
script in `scripts/` with a fixed action, validated arguments and an allow
rule; `scripts/git-diff-main-checkout.sh` is the model.

`tests/kit/test_hooks.py`, `tests/kit/test_text_tool_guards.py` and
`tests/kit/test_music_guard.py` cover what
each refuses and lets through, and that each is wired in.
