from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Command


class Source(ABC):
    name: str

    @abstractmethod
    def poll(self) -> list[Command]:
        """Return newly observed Commands since the last poll.

        A Command returned here is not yet considered processed -- if the
        process dies before ack() is called for it, the next poll() must
        return it again rather than silently skipping it.
        """

    @abstractmethod
    def ack(self, command: Command) -> None:
        """Mark a command as fully dispatched so it is never re-delivered.

        Must be called exactly once dispatch() has returned for a command
        (regardless of whether the dispatch or the reply succeeded) -- not
        before, since that's the point past which a retry would mean
        redoing (and possibly double-billing) already-completed work.
        """

    @abstractmethod
    def reply(self, command: Command, text: str) -> None:
        """Send a status message back to wherever the command came from."""
