This repo is a system designed to sit on top of any other repo.

When a trusted person sends a text with a special trigger word or emoji, a
polling job watching the local iMessage database picks it up, sends the
instruction as a brand-new Claude Code session scoped to an isolated git
worktree of the target repo, and — if Claude made real changes — pushes a
branch and opens a pull request. Nothing lands on the target repo's default
branch without a human merging it. The sender then gets an iMessage reply
with the PR link or an error.

The source/sink model (`agent_modifier/sources/base.py`) is intentionally
generic so other trigger channels (Discord, Telegram, ...) can be added
later without touching the dispatcher.

## How it works

1. `IMessageSource.poll()` reads `~/Library/Messages/chat.db` for new
   incoming messages, keeping a `ROWID` cursor in `state/state.json` so
   nothing is reprocessed. Messages are only acted on if the sender is in
   `config/config.yaml`'s `allowlist.imessage` **and** the message starts
   with `trigger`. The rest of the message becomes the instruction.
2. If the message has photo/file attachments, their on-disk paths (under
   `~/Library/Messages/Attachments/...`) are resolved too. A message can be
   attachment-only (no caption text) as long as it has at least one
   attachment or non-empty text — an empty message with nothing usable is
   skipped.
3. `Dispatcher.dispatch()` copies any attachments into a staging directory
   (`state/attachments/<name>/`, outside the target repo), then runs
   `claude -p "<instruction>" -w <name> --add-dir <staging dir> ...` inside
   `target_repo.path`. `-w` creates an isolated git worktree at
   `<target_repo>/.claude/worktrees/<name>` on branch `worktree-<name>`;
   `--add-dir` is what lets Claude's Read tool actually see the staged
   attachment files, since they live outside the worktree it's confined to.
   Claude is instructed (via `--append-system-prompt`) to commit its changes
   but not push or open a PR. If the target repo has an `.mcp.json` at its
   root, it's passed explicitly via `--mcp-config`/`--strict-mcp-config` so
   the dispatched session gets those MCP tools deterministically -- a fresh
   worktree has never been through the interactive per-checkout approval
   `.mcp.json` servers normally require, so without this they'd sit
   "pending approval" and silently be unavailable. Any secrets that tooling
   needs (e.g. a DB connection string) should go in `target_repo.env` in
   `config.yaml`, since a gitignored `.env` in the target repo also won't
   exist in a fresh worktree.
4. The dispatcher checks that a real commit exists, then pushes the branch
   and runs `gh pr create` itself — this step is deliberately not left to
   the model, so it's deterministic and auditable.
5. The worktree and the attachment staging directory are always cleaned up,
   and every run is appended to `logs/dispatch.log` as a JSON line
   (timestamp, sender, instruction, attachment count, outcome, PR url or
   error, cost).
6. The sender gets an iMessage reply with the result.

## One-time setup

1. **Run the setup script**:
   ```
   scripts/setup.sh
   ```
   This creates `.venv` (needs Python 3.10+ — it searches for
   `python3.13`/`.12`/`.11`/`.10`/`python3` in that order; override with
   `AGENT_MODIFIER_PYTHON=/path/to/python3.x` if needed), installs
   dependencies, and copies `config/config.example.yaml` to
   `config/config.yaml` if that doesn't already exist. It ends by printing
   the exact binary path you need for the next step.

2. **Grant macOS permissions** to the real Python interpreter path the
   script just printed (not the `.venv/bin/python` symlink — macOS tracks
   unsigned binaries by their real path) — this can't be scripted:
   - **System Settings → Privacy & Security → Full Disk Access**: needed to
     read `~/Library/Messages/chat.db` and any attachments in
     `~/Library/Messages/Attachments/`.
   - **System Settings → Privacy & Security → Automation**: needed to let
     the process control Messages.app (to send reply texts).

   In each panel, click **+**, press `Cmd+Shift+G` in the file picker, and
   paste the path (it's usually not searchable by name). If this
   interpreter's path ever changes (e.g. a Homebrew Python upgrade),
   you'll need to re-grant both to the new path — re-run `scripts/setup.sh`
   to get the current path.

3. **Edit `config/config.yaml`**:
   - `trigger`: the word/emoji that starts a command.
   - `target_repo.path`: the repo this should modify. Must already be a git
     repo with a configured remote.
   - `allowlist.imessage`: phone numbers/handles allowed to trigger
     commands — ships empty, which means nothing is ever actioned. Format
     must match `chat.db` exactly; confirm with:
     ```
     sqlite3 ~/Library/Messages/chat.db "SELECT DISTINCT id FROM handle;"
     ```
     (only works once Full Disk Access is granted).

4. **Confirm `gh` auth** works against the target repo:
   ```
   gh auth status
   ```

## Running

Manually (foreground, useful for testing):
```
./.venv/bin/python -m agent_modifier.runner
```

As a background service via launchd:
```
scripts/install_launchd.sh
```
This renders `launchd/com.agentmodifier.poller.plist` from the template
(filling in this machine's real paths), installs it to
`~/Library/LaunchAgents/`, and loads it. Re-run it any time after changing
code or config to restart with the new version. It's `RunAtLoad` +
`KeepAlive`, so it survives reboots and restarts itself if it crashes —
though note it does **not** run while the Mac is asleep (e.g. lid closed);
it just picks up any backlog on next wake, nothing is lost.

Stop it with:
```
launchctl unload ~/Library/LaunchAgents/com.agentmodifier.poller.plist
```

Logs land in `logs/runner.log` (application log), `logs/dispatch.log`
(one JSON line per dispatched command), and `logs/launchd.{out,err}.log`
(launchd's raw stdout/stderr capture).

## Safety notes

- Claude runs with `--permission-mode bypassPermissions` inside the isolated
  worktree, so it has full Bash/Edit/Write access there without prompting.
  The worktree/branch/PR-gate model contains the blast radius to "a PR gets
  opened," not "main gets changed" or "your main working copy gets touched."
- The sender allowlist is the only gate on who can trigger changes — keep it
  tight.
- `claude.max_budget_usd` in the config caps spend per invocation as a
  backstop against runaway loops.
