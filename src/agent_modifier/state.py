from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class StateStore:
    """Tracks the last-processed message id per source, persisted as JSON.

    Also keeps a short rolling conversation history per sender, so a
    follow-up message can be dispatched with the context of what was just
    said -- without ever replaying a prior instruction (each dispatch still
    happens exactly once, driven by the source's own last-seen tracking).

    Writes are atomic (write to a temp file, then rename) so a crash
    mid-write can't corrupt the state file and cause reprocessing or gaps.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        with self._path.open("r") as f:
            return json.load(f)

    def get_last_seen(self, key: str) -> int | None:
        return self._data.get(key)

    def set_last_seen(self, key: str, value: int) -> None:
        self._data[key] = value
        self._write()

    def get_history(self, key: str) -> list[dict[str, str]]:
        return self._data.get(f"history:{key}", [])

    def append_history(self, key: str, instruction: str, reply: str, limit: int = 2) -> None:
        history_key = f"history:{key}"
        history = self._data.get(history_key, [])
        history.append({"instruction": instruction, "reply": reply})
        self._data[history_key] = history[-limit:]
        self._write()

    # -- Crash recovery: the one gap the cursor alone can't close -------
    #
    # The cursor only ever advances *after* ack(), so a crash between
    # poll() and ack() just means the same command is handed to dispatch()
    # again next run -- safe, since dispatch() hadn't done anything yet.
    # But a crash *during* dispatch() (e.g. the process is killed while
    # Claude is mid-run) is a different case: real side effects -- a
    # database write, a git commit -- may already have happened, and we
    # have no way to know. Silently retrying in that situation is exactly
    # the "redo a completed inventory change" failure this exists to
    # prevent, so instead of guessing, record which command was in flight
    # before we started it, and if it's still marked in-flight at the next
    # startup, treat that as a crash mid-dispatch: skip auto-retrying it
    # and surface it for a human to check instead.

    def get_pending_dispatch(self, key: str) -> dict[str, Any] | None:
        return self._data.get(f"pending:{key}")

    def set_pending_dispatch(self, key: str, info: dict[str, Any]) -> None:
        self._data[f"pending:{key}"] = info
        self._write()

    def clear_pending_dispatch(self, key: str) -> None:
        if self._data.pop(f"pending:{key}", None) is not None:
            self._write()

    # -- One-time cursor bootstrap ---------------------------------------

    def is_bootstrap_applied(self, key: str) -> bool:
        return bool(self._data.get(f"bootstrap_applied:{key}"))

    def mark_bootstrap_applied(self, key: str) -> None:
        self._data[f"bootstrap_applied:{key}"] = True
        self._write()

    def _write(self) -> None:
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp_path.open("w") as f:
            json.dump(self._data, f)
        os.replace(tmp_path, self._path)
