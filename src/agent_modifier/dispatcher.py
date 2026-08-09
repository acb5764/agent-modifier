from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import ClaudeConfig, TargetRepoConfig
from .models import Command, DispatchResult
from .state import StateStore

logger = logging.getLogger(__name__)

CLAUDE_TIMEOUT_SECONDS = 20 * 60

HISTORY_LIMIT = 2

APPEND_SYSTEM_PROMPT = (
    "After making the requested changes, stage and commit them with git. "
    "Do not push and do not open a pull request -- that is handled outside "
    "this session. "
    "Your final message will be sent as a text message to a non-technical "
    "family member, so write it like a quick, warm text: short, plain "
    "English, no git/file/code jargon (don't say 'committed', don't show "
    "file paths or JSON). Just say plainly what changed. If you couldn't "
    "make the change, explain why in that same casual way."
)


def _make_worktree_name(instruction: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", instruction.lower()).strip("-")[:30] or "task"
    suffix = uuid.uuid4().hex[:6]
    return f"mango-{ts}-{slug}-{suffix}"


class Dispatcher:
    def __init__(
        self,
        target_repo: TargetRepoConfig,
        claude_config: ClaudeConfig,
        log_path: str | Path,
        attachments_dir: str | Path,
        state: StateStore,
    ):
        self._target_repo = target_repo
        self._claude_config = claude_config
        self._log_path = Path(log_path).expanduser()
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._attachments_dir = Path(attachments_dir).expanduser()
        self._state = state

    def dispatch(self, command: Command) -> DispatchResult:
        worktree_name = _make_worktree_name(command.instruction)
        branch_name = f"worktree-{worktree_name}"
        worktree_path = self._target_repo.path / ".claude" / "worktrees" / worktree_name

        try:
            # Attachments are staged in a directory outside the repo entirely
            # (not inside the worktree) because the worktree doesn't exist
            # yet -- `claude -w` creates it as part of the invocation below.
            staged_paths = self._stage_attachments(command, worktree_name)
            result = self._run_claude(command, worktree_name, staged_paths)
        except Exception as exc:  # noqa: BLE001 -- any failure here must not crash the runner
            logger.exception("claude invocation failed for %s", command.raw_message_id)
            self._cleanup_worktree(worktree_name)
            self._cleanup_attachments(worktree_name)
            outcome = DispatchResult(
                success=False,
                message="Ran into a technical hiccup and couldn't finish that one -- try again in a bit.",
            )
            self._audit(command, outcome)
            return outcome

        try:
            if result.get("is_error"):
                message = result.get("result") or "claude reported an error"
                outcome = DispatchResult(success=False, message=message)
                self._audit(command, outcome)
                return outcome

            if not self._has_new_commits(worktree_path):
                outcome = DispatchResult(
                    success=False,
                    message=result.get("result") or "no changes were made",
                    cost_usd=result.get("total_cost_usd"),
                )
                self._audit(command, outcome)
                return outcome

            changed_files = self._changed_files(worktree_path)
            if self._is_auto_push_eligible(changed_files):
                try:
                    self._push_to_base_branch(worktree_path)
                    outcome = DispatchResult(
                        success=True,
                        message=result.get("result") or "done",
                        cost_usd=result.get("total_cost_usd"),
                    )
                    self._audit(command, outcome)
                    return outcome
                except subprocess.CalledProcessError:
                    logger.warning(
                        "auto-push to %s failed for %s, falling back to PR",
                        self._target_repo.base_branch,
                        command.raw_message_id,
                    )

            pr_url = self._push_and_open_pr(command, worktree_path, branch_name)
            outcome = DispatchResult(
                success=True,
                message=result.get("result") or "done",
                pr_url=pr_url,
                cost_usd=result.get("total_cost_usd"),
            )
            self._audit(command, outcome)
            return outcome
        except Exception as exc:  # noqa: BLE001
            logger.exception("post-processing failed for %s", command.raw_message_id)
            outcome = DispatchResult(
                success=False,
                message="Made the change but had trouble saving it -- try again in a bit.",
            )
            self._audit(command, outcome)
            return outcome
        finally:
            self._cleanup_worktree(worktree_name)
            self._cleanup_attachments(worktree_name)

    def _history_key(self, command: Command) -> str:
        return f"{command.source}:{command.sender_id}"

    def _stage_attachments(self, command: Command, worktree_name: str) -> tuple[Path, ...]:
        if not command.attachment_paths:
            return ()
        staging_dir = self._attachments_dir / worktree_name
        staging_dir.mkdir(parents=True, exist_ok=True)
        staged = []
        for i, src in enumerate(command.attachment_paths):
            dest = staging_dir / f"{i}-{src.name}"
            shutil.copy2(src, dest)
            staged.append(dest)
        return tuple(staged)

    def _run_claude(self, command: Command, worktree_name: str, staged_paths: tuple[Path, ...]) -> dict:
        prompt = command.instruction or (
            "Look at the attached file(s) and use them as context for a small, "
            "sensible change to this repo."
        )
        if staged_paths:
            prompt += "\n\nAttached file(s) (use the Read tool to view them):\n" + "\n".join(
                f"- {p}" for p in staged_paths
            )

        history = self._state.get_history(self._history_key(command))
        if history:
            recap = "\n".join(
                f'- They said: "{turn["instruction"]}" / You replied: "{turn["reply"]}"'
                for turn in history
            )
            prompt = (
                "For background, here are your last couple of exchanges with "
                "this same person. Those are already done and committed -- do "
                "NOT redo, re-apply, or re-commit anything from them. Only use "
                "them to understand what their new message below might be "
                "referring to (e.g. answering a question you asked).\n"
                f"{recap}\n\n"
                f"Their new message:\n{prompt}"
            )

        cmd = [
            "claude",
            "-p",
            prompt,
            "-w",
            worktree_name,
            "--permission-mode",
            self._claude_config.permission_mode,
            "--append-system-prompt",
            APPEND_SYSTEM_PROMPT,
            "--output-format",
            "json",
            "--max-budget-usd",
            str(self._claude_config.max_budget_usd),
            "--no-session-persistence",
        ]
        if staged_paths:
            cmd += ["--add-dir", str(staged_paths[0].parent)]

        # Project-scoped MCP servers (.mcp.json) normally require a one-time
        # interactive approval, recorded in the gitignored, per-checkout
        # .claude/settings.local.json -- which a brand-new worktree never
        # has. Passing the config explicitly loads it deterministically for
        # this session instead of leaving it "pending approval" and silently
        # unavailable. --strict-mcp-config keeps the dispatched session
        # scoped to only what the target repo itself declares.
        mcp_config_path = self._target_repo.path / ".mcp.json"
        if mcp_config_path.is_file():
            cmd += ["--mcp-config", str(mcp_config_path), "--strict-mcp-config"]

        proc = subprocess.run(
            cmd,
            cwd=self._target_repo.path,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SECONDS,
            env={**os.environ, **self._target_repo.env},
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude exited {proc.returncode}: {proc.stderr.strip()[:2000]}"
            )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"could not parse claude JSON output: {exc}") from exc

    def _has_new_commits(self, worktree_path: Path) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(worktree_path), "log", f"{self._target_repo.base_branch}..HEAD", "--oneline"],
            capture_output=True,
            text=True,
        )
        proc.check_returncode()
        return bool(proc.stdout.strip())

    def _changed_files(self, worktree_path: Path) -> list[str]:
        proc = subprocess.run(
            ["git", "-C", str(worktree_path), "diff", "--name-only",
             f"{self._target_repo.base_branch}..HEAD"],
            capture_output=True,
            text=True,
        )
        proc.check_returncode()
        return [line for line in proc.stdout.splitlines() if line]

    def _is_auto_push_eligible(self, changed_files: list[str]) -> bool:
        if not changed_files or not self._target_repo.auto_push_patterns:
            return False
        return all(
            any(fnmatch.fnmatch(f, pattern) for pattern in self._target_repo.auto_push_patterns)
            for f in changed_files
        )

    def _push_to_base_branch(self, worktree_path: Path) -> None:
        subprocess.run(
            ["git", "-C", str(worktree_path), "push", "origin",
             f"HEAD:refs/heads/{self._target_repo.base_branch}"],
            capture_output=True,
            text=True,
            check=True,
        )

    def _push_and_open_pr(self, command: Command, worktree_path: Path, branch_name: str) -> str:
        subprocess.run(
            ["git", "-C", str(worktree_path), "push", "-u", "origin", branch_name],
            capture_output=True,
            text=True,
            check=True,
        )

        title = command.instruction.strip()[:72]
        body = (
            f"Requested via {command.source} by `{command.sender_id}`:\n\n"
            f"> {command.instruction}\n"
        )
        proc = subprocess.run(
            [
                "gh", "pr", "create",
                "--base", self._target_repo.base_branch,
                "--head", branch_name,
                "--title", title,
                "--body", body,
            ],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip().splitlines()[-1]

    def _cleanup_worktree(self, worktree_name: str) -> None:
        subprocess.run(
            ["git", "-C", str(self._target_repo.path), "worktree", "remove", "-f", "-f", worktree_name],
            capture_output=True,
            text=True,
        )

    def _cleanup_attachments(self, worktree_name: str) -> None:
        shutil.rmtree(self._attachments_dir / worktree_name, ignore_errors=True)

    def _audit(self, command: Command, result: DispatchResult) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": command.source,
            "sender_id": command.sender_id,
            "instruction": command.instruction,
            "attachment_count": len(command.attachment_paths),
            "success": result.success,
            "message": result.message,
            "pr_url": result.pr_url,
            "cost_usd": result.cost_usd,
        }
        with self._log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        self._state.append_history(
            self._history_key(command), command.instruction, result.message, limit=HISTORY_LIMIT
        )
