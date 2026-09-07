from __future__ import annotations

import logging
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import typedstream

from ..models import Command
from ..state import StateStore
from .base import Source

logger = logging.getLogger(__name__)

DEFAULT_CHAT_DB_PATH = Path("~/Library/Messages/chat.db").expanduser()

# message.date is nanoseconds since the "Apple epoch" (2001-01-01 00:00:00
# UTC), not the Unix epoch -- this is the offset between the two.
APPLE_EPOCH_OFFSET_SECONDS = 978307200

# is_from_me = 0 so we only ever see incoming messages, never our own replies.
# chat.guid is the conversation the message belongs to -- for a 1:1 it looks
# like "iMessage;-;+15551234567", for a group like "iMessage;+;chatGUID...".
# It's exactly the string the Messages app expects for `send ... to chat id`,
# so it doubles as the reply target: replying always lands back in whichever
# thread (direct or group) the command came from, not a fresh DM to the sender.
# GROUP BY collapses the rare case where a message maps to more than one chat
# row, picking one deterministically rather than duplicating the command.
QUERY = """
SELECT message.ROWID, message.date, message.text, message.attributedBody, handle.id AS sender,
       chat.guid AS chat_guid
FROM message
JOIN handle ON message.handle_id = handle.ROWID
JOIN chat_message_join ON chat_message_join.message_id = message.ROWID
JOIN chat ON chat.ROWID = chat_message_join.chat_id
WHERE message.ROWID > ? AND message.is_from_me = 0
GROUP BY message.ROWID
ORDER BY message.ROWID
"""

# When someone sends several photos as one action, iMessage/chat.db usually
# splits them into separate message rows -- only one of which carries the
# typed caption, the rest have no text at all. A caption-less row is
# otherwise indistinguishable from someone just sending a random photo with
# no request attached, so it's only ever picked up as "belongs to the
# triggered message next to it" if it lands within this many nanoseconds of
# one, from the same sender and chat. Wide enough to cover a multi-photo
# burst (all such rows normally share the same handful of seconds), narrow
# enough not to glue an unrelated, later photo onto an old command.
BURST_WINDOW_NS = 10_000_000_000

ATTACHMENTS_QUERY = """
SELECT attachment.filename
FROM message_attachment_join
JOIN attachment ON attachment.ROWID = message_attachment_join.attachment_id
WHERE message_attachment_join.message_id = ?
"""


def _fetch_attachments(conn: sqlite3.Connection, message_rowid: int) -> tuple[Path, ...]:
    paths = []
    for (filename,) in conn.execute(ATTACHMENTS_QUERY, (message_rowid,)):
        if not filename:
            continue
        # filename is stored with a literal "~" for the home directory.
        path = Path(filename).expanduser()
        if path.exists():
            paths.append(path)
        else:
            logger.warning("attachment on message %s not found on disk: %s", message_rowid, path)
    return tuple(paths)


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

    def rowid_before(self, cutoff: datetime) -> int:
        """Return the ROWID to use as `last_seen` so polling resumes at `cutoff`.

        `cutoff` is interpreted as local time (naive datetimes are treated
        as this machine's local timezone, matching how a person would read
        a date like "2026-08-14"). Used for one-time cursor bootstrapping
        only -- normal operation never calls this, since the persisted
        cursor is always the source of truth once one exists.
        """
        target_ns = int((cutoff.timestamp() - APPLE_EPOCH_OFFSET_SECONDS) * 1_000_000_000)
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        try:
            first_at_or_after = conn.execute(
                "SELECT MIN(ROWID) FROM message WHERE date >= ?", (target_ns,)
            ).fetchone()[0]
            if first_at_or_after is not None:
                return first_at_or_after - 1
            # Nothing on record yet at/after cutoff (it's still in the
            # future) -- skip everything that exists so far; anything new
            # will naturally have a timestamp at/after cutoff by the time
            # it arrives.
            max_rowid = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0]
            return max_rowid or 0
        finally:
            conn.close()

    def poll(self) -> list[Command]:
        last_seen = self._state.get_last_seen(self.name) or 0
        max_rowid = last_seen

        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(QUERY, (last_seen,)).fetchall()
            for rowid, *_rest in rows:
                max_rowid = max(max_rowid, rowid)

            # Caption-less rows (no text, so they can't match the trigger on
            # their own) from an allowlisted sender, keyed by (sender,
            # chat_guid) so a triggered message only ever picks up orphans
            # from its own conversation.
            orphans_by_thread: dict[tuple[str, str], list[tuple[int, int]]] = {}
            for rowid, date, text, attributed_body, sender, chat_guid in rows:
                if sender not in self._allowlist:
                    continue
                if _extract_text(text, attributed_body):
                    continue
                if not _fetch_attachments(conn, rowid):
                    continue
                orphans_by_thread.setdefault((sender, chat_guid), []).append((rowid, date))

            claimed_orphan_rowids: set[int] = set()
            commands: list[Command] = []
            consumed_rowids: dict[str, int] = {}
            for rowid, date, text, attributed_body, sender, chat_guid in rows:
                if sender not in self._allowlist:
                    continue

                message_text = _extract_text(text, attributed_body)
                if not message_text:
                    continue

                stripped = message_text.strip()
                if not stripped.lower().startswith(self._trigger):
                    continue

                instruction = stripped[len(self._trigger):].strip()
                attachment_paths = list(_fetch_attachments(conn, rowid))
                min_rowid = rowid

                for orphan_rowid, orphan_date in orphans_by_thread.get((sender, chat_guid), []):
                    if orphan_rowid in claimed_orphan_rowids:
                        continue
                    if abs(orphan_date - date) <= BURST_WINDOW_NS:
                        attachment_paths.extend(_fetch_attachments(conn, orphan_rowid))
                        claimed_orphan_rowids.add(orphan_rowid)
                        min_rowid = min(min_rowid, orphan_rowid)

                if not instruction and not attachment_paths:
                    continue

                command_id = str(rowid)
                consumed_rowids[command_id] = min_rowid
                commands.append(
                    Command(
                        source=self.name,
                        sender_id=sender,
                        instruction=instruction,
                        raw_message_id=command_id,
                        chat_id=chat_guid,
                        attachment_paths=tuple(attachment_paths),
                    )
                )

            # An orphan only merges into a triggered command if both land in
            # this same poll -- but dispatch can take anywhere from seconds
            # to (per CLAUDE_TIMEOUT_SECONDS) minutes, and the cursor moves
            # past a triggered message the moment it's dispatched. So a
            # photo attached moments later, as its own separate send, easily
            # lands in a *later* poll with no triggered sibling left to
            # claim it -- there's nothing to widen a time window against.
            # Rather than let that go the same way the pre-fix silent drop
            # did, an unclaimed orphan becomes its own command with an empty
            # instruction: the dispatcher already has a fallback prompt for
            # exactly that ("use the attached file(s) as context"), plus a
            # per-sender history recap, so it still has enough to go on.
            for (sender, chat_guid), orphans in orphans_by_thread.items():
                for orphan_rowid, _orphan_date in orphans:
                    if orphan_rowid in claimed_orphan_rowids:
                        continue
                    command_id = str(orphan_rowid)
                    consumed_rowids[command_id] = orphan_rowid
                    commands.append(
                        Command(
                            source=self.name,
                            sender_id=sender,
                            instruction="",
                            raw_message_id=command_id,
                            chat_id=chat_guid,
                            attachment_paths=_fetch_attachments(conn, orphan_rowid),
                        )
                    )
        finally:
            conn.close()

        commands.sort(key=lambda c: int(c.raw_message_id))

        # Rows that never became a Command are gone for good -- safe to
        # skip forever. A row that DID become a Command (including any
        # caption-less attachment rows folded into it above, or standing
        # alone as their own command) is only safe to skip once ack() has
        # been called for it, so the cursor stops right before the earliest
        # rowid still tied to a pending command instead of racing ahead of
        # it. Everything from there up to max_rowid just gets re-scanned
        # (and re-filtered, or re-returned, re-merged) on the next poll --
        # cheap, and the only way to guarantee a crash between poll() and
        # dispatch() can't drop a message or a merged attachment on the
        # floor.
        safe_rowid = min(consumed_rowids.values()) - 1 if consumed_rowids else max_rowid
        if safe_rowid > last_seen:
            self._state.set_last_seen(self.name, safe_rowid)

        return commands

    def ack(self, command: Command) -> None:
        rowid = int(command.raw_message_id)
        current = self._state.get_last_seen(self.name) or 0
        if rowid > current:
            self._state.set_last_seen(self.name, rowid)

    def reply(self, command: Command, text: str) -> None:
        script = (
            'tell application "Messages"\n'
            f'send "{_escape_applescript(text)}" to chat id "{_escape_applescript(command.chat_id)}"\n'
            "end tell"
        )
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True)
