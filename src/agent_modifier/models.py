from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Command:
    source: str
    sender_id: str
    instruction: str
    raw_message_id: str
    # Where a reply should be sent -- the conversation/thread the command was
    # triggered from, which may be a 1:1 or a group chat. Not necessarily the
    # same as sender_id (e.g. a group has multiple senders but one thread).
    chat_id: str
    attachment_paths: tuple[Path, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DispatchResult:
    success: bool
    message: str
    pr_url: str | None = None
    cost_usd: float | None = None
