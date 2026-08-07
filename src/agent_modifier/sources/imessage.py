from __future__ import annotations

import logging
import sqlite3
import subprocess
from pathlib import Path

import typedstream

from ..models import Command
from ..state import StateStore
from .base import Source

logger = logging.getLogger(__name__)

DEFAULT_CHAT_DB_PATH = Path("~/Library/Messages/chat.db").expanduser()

# is_from_me = 0 so we only ever see incoming messages, never our own replies.
QUERY = """
SELECT message.ROWID, message.text, message.attributedBody, handle.id AS sender
FROM message
JOIN handle ON message.handle_id = handle.ROWID
WHERE message.ROWID > ? AND message.is_from_me = 0
ORDER BY message.ROWID
"""


def _extract_text(text: str | None, attributed_body: bytes | None) -> str | None:
    # On modern macOS, message.text is frequently NULL and the real text is
    # archived inside attributedBody as a legacy NSArchiver "typedstream"
    # blob (NSAttributedString), not plain text or a keyed plist.
    if text:
        return text
    if not attributed_body:
        return None
    try:
        obj = typedstream.unarchive_from_data(attributed_body)
        value = obj.contents[0].value
        return getattr(value, "value", None)
    except Exception:
        logger.exception("failed to parse attributedBody blob")
        return None


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class IMessageSource(Source):
    name = "imessage"

    def __init__(
        self,
        trigger: str,
        allowlist: list[str],
        state: StateStore,
        db_path: Path = DEFAULT_CHAT_DB_PATH,
    ):
        self._trigger = trigger.strip().lower()
        self._allowlist = set(allowlist)
        self._state = state
        self._db_path = db_path
        if not self._allowlist:
            logger.warning(
                "no allowlisted iMessage senders configured -- no commands will be actioned"
            )

    def poll(self) -> list[Command]:
        last_seen = self._state.get_last_seen(self.name) or 0
        max_rowid = last_seen
        commands: list[Command] = []

        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        try:
            for rowid, text, attributed_body, sender in conn.execute(QUERY, (last_seen,)):
                max_rowid = max(max_rowid, rowid)

                if sender not in self._allowlist:
                    continue

                message_text = _extract_text(text, attributed_body)
                if not message_text:
                    continue

                stripped = message_text.strip()
                if not stripped.lower().startswith(self._trigger):
                    continue

                instruction = stripped[len(self._trigger):].strip()
                if not instruction:
                    continue

                commands.append(
                    Command(
                        source=self.name,
                        sender_id=sender,
                        instruction=instruction,
                        raw_message_id=str(rowid),
                    )
                )
        finally:
            conn.close()

        if max_rowid > last_seen:
            self._state.set_last_seen(self.name, max_rowid)

        return commands

    def reply(self, command: Command, text: str) -> None:
        script = (
            'tell application "Messages"\n'
            "set targetService to first service whose service type = iMessage\n"
            f'set targetBuddy to buddy "{command.sender_id}" of targetService\n'
            f'send "{_escape_applescript(text)}" to targetBuddy\n'
            "end tell"
        )
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True)
