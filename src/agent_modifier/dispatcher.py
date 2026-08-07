from __future__ import annotations

import json
import logging
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import ClaudeConfig, TargetRepoConfig
from .models import Command, DispatchResult

logger = logging.getLogger(__name__)

CLAUDE_TIMEOUT_SECONDS = 20 * 60

APPEND_SYSTEM_PROMPT = (
    "After making the requested changes, stage and commit them with git. "
    "Do not push and do not open a pull request -- that is handled outside "
    "this session."
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
    ):
        self._target_repo = target_repo
        self._claude_config = claude_config
        self._log_path = Path(log_path).expanduser()
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def dispatch(self, command: Command) -> DispatchResult:
        worktree_name = _make_worktree_name(command.instruction)
        branch_name = f"worktree-{worktree_name}"
        worktree_path = self._target_repo.path / ".claude" / "worktrees" / worktree_name

        try:
            result = self._run_claude(command, worktree_name)
        except Exception as exc:  # noqa: BLE001 -- any failure here must not crash the runner
            logger.exception("claude invocation failed for %s", command.raw_message_id)
            self._cleanup_worktree(worktree_name)
            outcome = DispatchResult(success=False, message=f"claude invocation failed: {exc}")
            self._audit(command, outcome)
            return outcome

        try:
            if result.get("is_error"):
                message = result.get("result") or "claude reported an error"
                outcome = DispatchResult(success=False, message=message)
                self._audit(command, outcome)
                return outcome

            if not self._has_new_commits(worktree_path):
                outcome = DispatchResult(success=False, message="no changes were made")
                self._audit(command, outcome)
                return outcome

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
            outcome = DispatchResult(success=False, message=f"post-processing failed: {exc}")
            self._audit(command, outcome)
            return outcome
        finally:
            self._cleanup_worktree(worktree_name)

    def _run_claude(self, command: Command, worktree_name: str) -> dict:
        cmd = [
            "claude",
            "-p",
            command.instruction,
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
        proc = subprocess.run(
            cmd,
            cwd=self._target_repo.path,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SECONDS,
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

    def _audit(self, command: Command, result: DispatchResult) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": command.source,
            "sender_id": command.sender_id,
            "instruction": command.instruction,
            "success": result.success,
            "message": result.message,
            "pr_url": result.pr_url,
            "cost_usd": result.cost_usd,
        }
        with self._log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
