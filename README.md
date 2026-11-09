# Helen Contains Claude: 

This template repository is a Claude Code in Docker quickstart kit. 

Only the project folder is mounted (at `/workspace`), so Claude can't read anything else on the host. There are also guard hooks for scary commands, an agent-account GitHub workflow, and a `CLAUDE.md` starter.

There are easy togglable options for toolchains to include in the Docoker image. 

## What's in the box

```
.devcontainer/
  run.sh                 build-if-stale, then start the container
  kit.env                toggles: what goes in the image, ports, firewall extras
  kit.local.example.env  a name kept out of git (see "Working on the kit itself")
  Dockerfile             the image; every toggle is a build argument
  init-firewall.sh       optional default-deny egress allowlist
  requirements.txt       pip installs, when WITH_PY_REQS=1
  project.bashrc         project aliases, read live from the mount
  container.bashrc       loads project.bashrc and host dotfiles, once per shell,
                         and seeds Claude's memory on a new project
  seed_memory.py         copies .claude/memory-seed/ into the memory, once
  devcontainer.json      VS Code Dev Containers, using the same image
  build_inputs_hash.py   the build stamp (see "Staying current")
  claude_code_latest.py  asks npm for the newest Claude Code
  read_local_token.py    reads AGENT_GH_TOKEN for one docker run
  set-project-name.py    sets PROJECT_NAME in kit.env and devcontainer.json
.claude/
  settings.json          permissions + hooks
  hooks/                 guards (see .claude/hooks/README.md)
  memory-seed/           notes a new project's Claude memory starts with: no
                         `cd`, relative paths, complex commands as literal
                         scripts, and the same rules passed to subagents.
                         Copied once per config volume; delete one there and
                         it stays deleted
scripts/
  github.conf            placeholder template for github.local.conf (gitignored):
                         owner, repos, bot account, the wrappers' whole world
  gh-*.sh, git-*-agent.sh, git-fetch-main.sh, github-public-status.sh
  git-diff-main-checkout.sh  the main checkout's uncommitted changes, from a worktree
  check-link.sh          does one https link resolve: status and final address
  git-nested.sh          a fixed list of git verbs inside a nested clone named
                         in NESTED_DIRS, hooks off
  main-ci-status.sh      is CI on main green, running, cancelled or red
                         (main_ci_status.py reads the answer)
tools/dev/
  shoot.js               full-page screenshots, phone and desktop, light or dark,
                         Chromium or WebKit
  slice-png.js           cuts a tall screenshot into readable pieces
  crop.js                one element at 2x, with its box, optionally after typing
  styles.js              each match's box and computed styles
CLAUDE.md                rules starter: fill in <PROJECT> and the build commands
tests/kit/               tests for the wrappers, hooks, stamp and config
```

## Setting up a new project

You need Docker and `python3` on the host (WSL2 + Docker Desktop is fine).

1. **Name it.** This names the image, the container and the volumes:

       python3 .devcontainer/set-project-name.py myproject

2. **Pick toolchains** in `.devcontainer/kit.env`:

   | Toggle | Adds |
   |---|---|
   | *(always)* | Debian bookworm, git, curl, jq, Python 3, pytest, Node, `gh`, Claude Code |
   | `WITH_RUBY=1` | Ruby `RUBY_VERSION` (base becomes `ruby:<v>-slim-bookworm`), build headers, a persistent bundle cache |
   | `WITH_PLAYWRIGHT=1` | Playwright `PLAYWRIGHT_VERSION` + headless Chromium and WebKit (nearest Safari) under `/opt/playwright`, npm and pip bindings |
   | `WITH_AUDIVERIS=1` | Audiveris `AUDIVERIS_VERSION` (sheet-music PDFs to MusicXML, with its own Java) under `/opt/audiveris`, the full tessdata English OCR model under `/opt/audiveris-tessdata`, and Pillow, PyMuPDF and pypdf. x86_64 only |
   | `WITH_PY_REQS=1` | `pip install -r .devcontainer/requirements.txt`: PyYAML and Pillow, plus whatever you add |

   Also in `kit.env`: `PORTS` (e.g. `"5000:5000 5001:5001"`), `EXTRA_ALLOWED_DOMAINS`
   for the firewall, and `MOUNT_DOTFILES`.

3. **GitHub agent account** (skip this step if Claude won't push):
   - Create a separate GitHub account for the bot. Invite it as a
     collaborator on just the repos you want it to work in.
   - Create it a classic PAT scoped to `repo`, and `workflow` if it
     should edit `.github/workflows/`: without it, GitHub refuses any push
     that touches a workflow file, and `git-push-agent.sh` says so.
   - Paste the token in `.claude/settings.local.json`, which is gitignored. A
     fresh project doesn't have that file yet, so copy the committed example
     to it, leaving the example in place, then paste the token between the
     quotes:

         cp .claude/settings.local.example.json .claude/settings.local.json

     `run.sh` reads the token for each `docker run` and never prints it. An
     empty value means no token, and the container starts without one.
   - Copy `scripts/github.conf` to `scripts/github.local.conf` (gitignored)
     and fill that in: `GH_OWNER`, `GH_REPOS` (the first is the default),
     `GH_AGENT_USER`, and `MAIN_PUSH_REPOS` if any repo may take direct
     pushes to `main`. The wrappers read the local copy; from a worktree, they
     read the main checkout's.
   - Optionally, protect `main` with a ruleset as well. The wrappers refuse
     to push there, but a ruleset is a belt for the braces.

4. **Fill in `CLAUDE.md`**: the project name and the build/test commands. Add
   any project rules.

5. **Run it:**

       bash .devcontainer/run.sh            # or --firewall
       claude                               # first time: log in (below)

   The first build takes a few minutes after you change anything.

   On Windows, you probably need to set execute permissions for the script before it will run:
       `chmod u+x .devcontainer/run.sh`


### First login

`claude`, then do the Claude login thing.

### First deploy

A GitHub Pages workflow (for example Jekyll's) builds but publishes nothing
until you set the repository's **Settings → Pages → Source** to **GitHub
Actions**. That is a repository setting, which the agent must not change, so
it is yours to do once.

## Using it

- `bash .devcontainer/run.sh` starts the container. `--rebuild` rebuilds from
  scratch, pulling the base image fresh. `--build-only` builds and stops.
- **One container per project.** The fixed container name refuses a second
  `run.sh`, so two Claudes never edit one working tree. To run several
  sessions at once, use git worktrees inside the one container.
- `run.sh` always mounts the primary clone, so all worktrees share one
  container. It builds from the `.devcontainer/` it sits in, so to try a
  branch's `kit.env` or Dockerfile before it is merged, run that worktree's
  copy from the host:

      bash .claude/worktrees/<name>/.devcontainer/run.sh
- `.devcontainer/project.bashrc` holds project aliases. It is read from the
  mount at shell start, so edits don't need a rebuild. A server must listen on
  `0.0.0.0` to be reachable through a published port.

### The firewall

`run.sh --firewall` adds `NET_ADMIN`/`NET_RAW` and runs `init-firewall.sh`
before the shell starts. Outbound traffic is then dropped except to:
Anthropic, npm, GitHub, `storage.googleapis.com`, rubygems (when Ruby is on),
PyPI (when the requirements toggle is on), and `EXTRA_ALLOWED_DOMAINS`. The
list is written into the image at build time, in a root-owned file, so nothing
inside the container can widen it. Hosts are resolved once at start; a
rotating CDN can drop out mid-session, and re-running the script re-resolves
them.

### VS Code

Dev Containers → **Reopen in Container**. `devcontainer.json` runs
`run.sh --build-only` on the host first, then uses the same image, so the two
routes cannot drift. Export `AGENT_GH_TOKEN` in the host shell VS Code starts
from if the wrappers need it there. For the firewall, add
`"runArgs": ["--cap-add=NET_ADMIN", "--cap-add=NET_RAW"]` and
`"postStartCommand": "sudo /usr/local/bin/init-firewall.sh"`.

## Staying current

**The image is checked against the files on every start.** `run.sh` stamps
each image with a SHA-256 of its build context and build arguments. When the
stamp no longer matches, it rebuilds. The check compares content, not
timestamps: `touch` costs nothing, and reverting an edit goes back to the image
that already matches. Files in `.devcontainer/.dockerignore` (run.sh, kit.env's
comments, these helpers) are outside the stamp, so editing them doesn't
rebuild.

**Claude Code updates by rebuild.** Every start asks npm for the newest
version. If the image has an older one, only the last layer rebuilds. When
offline, the image starts as it is. To pin a version for one run:
`PIN_CLAUDE_CODE=2.1.0 bash .devcontainer/run.sh`.

**What the stamp can't see:** the base image or apt packages moving upstream.
Use `--rebuild` when that matters.

## What this does and doesn't protect against

**Does:** it stops Claude, or a mistaken command it runs, from reading anything
outside the project, structurally rather than by convention. With
`--firewall`, it also stops traffic to hosts outside the allowlist. The hooks
stop commits to `main`, destructive git on a dirty tree, and the common ways a
token ends up printed.

**Doesn't:** it is not a defence against a deliberate, sophisticated container
escape. The threat model is an honest mistake. Also, the token is in the
container's environment: the hooks make printing it hard, not impossible.

## Working on the kit itself

The kit is a template, so what it commits is what every new project starts
from. Keep the committed copies as placeholders (`PROJECT_NAME=changeme`,
`your-org`, `<PROJECT>`) and put your own values in the gitignored local files:

    cp .devcontainer/kit.local.example.env .devcontainer/kit.local.env   # PROJECT_NAME
    cp scripts/github.conf scripts/github.local.conf                     # GitHub
    cp .claude/settings.local.example.json .claude/settings.local.json   # token

`tests/kit/` fails if `scripts/github.conf` holds anything but the
placeholders. Nothing checks the name or `CLAUDE.md`'s title, because a
project made from the kit commits its own.

## Tests

    python3 -m pytest

These cover the wrappers' refusals (dry-run, no network), the hooks, the build
stamp, and that `kit.env` and `devcontainer.json` agree. The image always has
`pytest`, and Claude may run `python3 -m pytest` or `python3 -m pytest -q`
unasked; anything with more arguments asks, because `--basetemp` empties the
folder it is given.
