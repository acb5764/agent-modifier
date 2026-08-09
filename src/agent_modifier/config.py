from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class TargetRepoConfig:
    path: Path
    base_branch: str
    auto_push_patterns: tuple[str, ...] = ()
    # Extra environment variables merged into the `claude` subprocess's env
    # (on top of this process's own env). A dispatched command always runs
    # in a brand-new git worktree, which only contains committed files --
    # any gitignored `.env` the target repo's own tooling (e.g. an MCP
    # server) relies on for secrets won't exist there. Use this to supply
    # those secrets instead of committing them.
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ClaudeConfig:
    permission_mode: str
    max_budget_usd: float


@dataclass(frozen=True)
class Config:
    trigger: str
    target_repo: TargetRepoConfig
    allowlist_imessage: list[str]
    poll_interval_seconds: int
    claude: ClaudeConfig


REQUIRED_TOP_LEVEL_KEYS = (
    "trigger",
    "target_repo",
    "allowlist",
    "poll_interval_seconds",
    "claude",
)


def load_config(path: str | Path) -> Config:
    raw_path = Path(path).expanduser()
    with raw_path.open("r") as f:
        raw = yaml.safe_load(f) or {}

    missing = [key for key in REQUIRED_TOP_LEVEL_KEYS if key not in raw]
    if missing:
        raise ValueError(f"config {raw_path} is missing required keys: {missing}")

    target_repo_raw = raw["target_repo"]
    for key in ("path", "base_branch"):
        if key not in target_repo_raw:
            raise ValueError(f"config target_repo is missing required key: {key}")

    claude_raw = raw["claude"]
    for key in ("permission_mode", "max_budget_usd"):
        if key not in claude_raw:
            raise ValueError(f"config claude is missing required key: {key}")

    env_raw = target_repo_raw.get("env") or {}
    if not isinstance(env_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env_raw.items()
    ):
        raise ValueError("config target_repo.env must be a mapping of string to string")

    trigger = raw["trigger"]
    if not isinstance(trigger, str) or not trigger.strip():
        raise ValueError("config trigger must be a non-empty string")

    return Config(
        trigger=trigger,
        target_repo=TargetRepoConfig(
            path=Path(target_repo_raw["path"]).expanduser(),
            base_branch=target_repo_raw["base_branch"],
            auto_push_patterns=tuple(target_repo_raw.get("auto_push_patterns") or ()),
            env=dict(env_raw),
        ),
        allowlist_imessage=list(raw["allowlist"].get("imessage") or []),
        poll_interval_seconds=int(raw["poll_interval_seconds"]),
        claude=ClaudeConfig(
            permission_mode=claude_raw["permission_mode"],
            max_budget_usd=float(claude_raw["max_budget_usd"]),
        ),
    )
