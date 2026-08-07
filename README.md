This repo is a system designed to sit on top of any other repo.

When a trusted person sends a text with a special trigger word, a polling
job watching the local iMessage database picks it up, sends the instruction
as a brand-new Claude Code session scoped to an isolated git worktree of the
target repo, and — if Claude made real changes — pushes a branch and opens a
pull request. Nothing lands on the target repo's default branch without a
human merging it. The sender then gets an iMessage reply with the PR link or
an error.

The source/sink model (`agent_modifier/sources/base.py`) is intentionally
generic so other trigger channels (Discord, Telegram, ...) can be added
later without touching the dispatcher.

## How it works

1. `IMessageSource.poll()` reads `~/Library/Messages/chat.db` for new
   incoming messages, keeping a `ROWID` cursor in `state/state.json` so
   nothing is reprocessed. Messages are only acted on if the sender is in
   `config/config.yaml`'s `allowlist.imessage` **and** the message
   (case-insensitively) starts with `trigger`. The rest of the message
   becomes the instruction.
2. `Dispatcher.dispatch()` runs `claude -p "<instruction>" -w <name> ...`
   inside `target_repo.path`, which creates an isolated git worktree at
   `<target_repo>/.claude/worktrees/<name>` on branch `worktree-<name>`.
   Claude is instructed (via `--append-system-prompt`) to commit its changes
   but not push or open a PR.
3. The dispatcher checks that a real commit exists, then pushes the branch
   and runs `gh pr create` itself — this step is deliberately not left to
   the model, so it's deterministic and auditable.
4. The worktree is always cleaned up (`git worktree remove -f -f`), and
   every run is appended to `logs/dispatch.log` as a JSON line (timestamp,
   sender, instruction, outcome, PR url or error, cost).
5. The sender gets an iMessage reply with the result.

## One-time setup

1. **Install dependencies**:
   ```
   python3.13 -m venv .venv
   ./.venv/bin/pip install -r requirements.txt
   ```
   (Needs Python 3.10+; the system `python3` on this Mac is 3.9, which is
   why the venv pins to `python3.13` via Homebrew.)

2. **Grant macOS permissions** to whatever process runs the poller
   (Terminal.app, or the specific Python interpreter in `.venv/bin/python`
   if you run it directly via launchd) — these cannot be scripted:
   - **System Settings → Privacy & Security → Full Disk Access**: needed to
     read `~/Library/Messages/chat.db`.
   - **System Settings → Privacy & Security → Automation**: needed to let
     the process control Messages.app (to send reply texts).

3. **Fill in the allowlist** in `config/config.yaml` — it ships empty, which
   means nothing is ever actioned. Add the phone number(s)/handle(s) that
   should be allowed to trigger changes, in the same format they appear in
   `chat.db` (e.g. `+15551234567`).

4. **Confirm `gh` auth** works against the target repo:
   ```
   gh auth status
   ```

5. **Point `target_repo.path`** in `config/config.yaml` at the repo you want
   this to modify (defaults to `~/projects/EMM`). It must already be a git
   repo with a configured remote.

## Running

Manually (foreground, useful for testing):
```
./.venv/bin/python -m agent_modifier.runner
```

As a background service via launchd:
```
cp launchd/com.agentmodifier.poller.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.agentmodifier.poller.plist
```

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
