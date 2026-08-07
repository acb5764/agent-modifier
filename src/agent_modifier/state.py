from __future__ import annotations

import json
import os
from pathlib import Path


class StateStore:
    """Tracks the last-processed message id per source, persisted as JSON.

    Writes are atomic (write to a temp file, then rename) so a crash
    mid-write can't corrupt the state file and cause reprocessing or gaps.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, int] = self._load()

    def _load(self) -> dict[str, int]:
        if not self._path.exists():
            return {}
        with self._path.open("r") as f:
            return json.load(f)

    def get_last_seen(self, key: str) -> int | None:
        return self._data.get(key)

    def set_last_seen(self, key: str, value: int) -> None:
        self._data[key] = value
        self._write()

    def _write(self) -> None:
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp_path.open("w") as f:
            json.dump(self._data, f)
        os.replace(tmp_path, self._path)
