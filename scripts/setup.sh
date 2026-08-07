#!/usr/bin/env bash
# One-time setup: create the venv, install dependencies, and print the
# macOS permissions you need to grant by hand (that part can't be scripted).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON_BIN="${AGENT_MODIFIER_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            major="$("$candidate" -c 'import sys; print(sys.version_info[0])')"
            minor="$("$candidate" -c 'import sys; print(sys.version_info[1])')"
            if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
                PYTHON_BIN="$(command -v "$candidate")"
                break
            fi
        fi
    done
fi

if [ -z "$PYTHON_BIN" ]; then
    echo "Could not find a Python 3.10+ interpreter (tried python3.13 down to python3)." >&2
    echo "Install one (e.g. 'brew install python@3.13') and re-run, or set" >&2
    echo "AGENT_MODIFIER_PYTHON=/path/to/python3.x and re-run." >&2
    exit 1
fi

echo "Using $PYTHON_BIN"

if [ ! -d .venv ]; then
    "$PYTHON_BIN" -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

REAL_PYTHON="$(./.venv/bin/python -c 'import os, sys; print(os.path.realpath(sys.executable))')"

if [ ! -f config/config.yaml ]; then
    cp config/config.example.yaml config/config.yaml
    echo "Created config/config.yaml from the template -- edit it before running."
fi

cat <<EOF

Setup complete.

IMPORTANT -- macOS permissions:
Grant Full Disk Access AND Automation to this exact binary (the real
interpreter, not the .venv symlink -- macOS tracks it by path):

  $REAL_PYTHON

  System Settings -> Privacy & Security -> Full Disk Access -> "+"
  (press Cmd+Shift+G in the file picker and paste the path above, since
  it usually isn't searchable by name)
  Repeat the same "+" -> paste path step under -> Automation.

If this interpreter's path ever changes (e.g. a Homebrew Python upgrade),
you'll need to re-grant both permissions to the new path.

Next steps:
  1. Edit config/config.yaml (trigger, target_repo.path, allowlist).
  2. Confirm 'gh auth status' shows access to your target repo.
  3. Test manually first: ./.venv/bin/python -m agent_modifier.runner
  4. When that works, run continuously via: scripts/install_launchd.sh
EOF
