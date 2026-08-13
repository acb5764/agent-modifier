#!/usr/bin/env bash
# Renders launchd/com.agentmodifier.poller.plist from the template (filling
# in this machine's real paths), installs it to ~/Library/LaunchAgents, and
# loads it. Safe to re-run after config or code changes -- it unloads any
# previously installed copy first.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ ! -x .venv/bin/python ]; then
    echo "No .venv found -- run scripts/setup.sh first." >&2
    exit 1
fi

if [ ! -f config/config.yaml ]; then
    echo "No config/config.yaml found -- run scripts/setup.sh first, then edit it." >&2
    exit 1
fi

CLAUDE_PATH="$(command -v claude || true)"
if [ -z "$CLAUDE_PATH" ]; then
    echo "Could not find 'claude' on PATH -- is Claude Code CLI installed?" >&2
    exit 1
fi
CLAUDE_BIN_DIR="$(dirname "$CLAUDE_PATH")"

# launchd must exec the venv's own python symlink, not the fully-resolved
# real interpreter -- Python only finds .venv's site-packages (via
# pyvenv.cfg) when invoked through a path inside the venv. The real,
# resolved path is still what needs Full Disk Access/Automation granted to
# it (macOS/TCC tracks unsigned binaries by their real path), which is why
# we print it separately below rather than using it here.
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
REAL_PYTHON="$(./.venv/bin/python -c 'import os, sys; print(os.path.realpath(sys.executable))')"

# Suffix the label with the target repo's name so multiple agent-modifier
# checkouts on the same machine (each pointed at a different target_repo)
# get distinct launchd labels. Two jobs sharing one label can't both be
# loaded -- loading the second silently evicts the first, which is exactly
# the failure mode this suffix avoids.
REPO_SLUG="$("$VENV_PYTHON" -c "
import re, yaml
cfg = yaml.safe_load(open('config/config.yaml'))
name = cfg['target_repo']['path'].rstrip('/').split('/')[-1]
print(re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-'))
")"
if [ -z "$REPO_SLUG" ]; then
    echo "Could not derive a label suffix from target_repo.path in config.yaml" >&2
    exit 1
fi
LABEL="com.agentmodifier.poller.$REPO_SLUG"
PLIST_PATH="$REPO_ROOT/launchd/$LABEL.plist"

sed \
    -e "s#{{REPO_ROOT}}#$REPO_ROOT#g" \
    -e "s#{{PYTHON_BIN}}#$VENV_PYTHON#g" \
    -e "s#{{CLAUDE_BIN_DIR}}#$CLAUDE_BIN_DIR#g" \
    -e "s#{{LABEL}}#$LABEL#g" \
    launchd/com.agentmodifier.poller.plist.template > "$PLIST_PATH"

plutil -lint "$PLIST_PATH"

mkdir -p ~/Library/LaunchAgents
cp "$PLIST_PATH" ~/Library/LaunchAgents/"$LABEL".plist

launchctl unload ~/Library/LaunchAgents/"$LABEL".plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/"$LABEL".plist

cat <<EOF

Installed and loaded $LABEL.
Logs: $REPO_ROOT/logs/{runner.log,dispatch.log,launchd.out.log,launchd.err.log}

Reminder: Full Disk Access + Automation must be granted to:
  $REAL_PYTHON
(scripts/setup.sh prints this same path -- run it again if you need a
reminder of where to add it in System Settings)

To stop: launchctl unload ~/Library/LaunchAgents/$LABEL.plist
EOF
