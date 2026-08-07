from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Command


class Source(ABC):
    name: str

    @abstractmethod
    def poll(self) -> list[Command]:
        """Return newly observed Commands since the last poll."""

    @abstractmethod
    def reply(self, command: Command, text: str) -> None:
        """Send a status message back to wherever the command came from."""
