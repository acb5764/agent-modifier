from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Command:
    source: str
    sender_id: str
    instruction: str
    raw_message_id: str
    attachment_paths: tuple[Path, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DispatchResult:
    success: bool
    message: str
    pr_url: str | None = None
    cost_usd: float | None = None
