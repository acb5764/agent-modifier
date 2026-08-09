from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import Config, load_config
from .dispatcher import Dispatcher
from .models import DispatchResult
from .sources.base import Source
from .sources.imessage import IMessageSource
from .state import StateStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"
STATE_PATH = REPO_ROOT / "state" / "state.json"
ATTACHMENTS_DIR = REPO_ROOT / "state" / "attachments"
DISPATCH_LOG_PATH = REPO_ROOT / "logs" / "dispatch.log"
RUNNER_LOG_PATH = REPO_ROOT / "logs" / "runner.log"

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    RUNNER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(RUNNER_LOG_PATH), logging.StreamHandler()],
    )


def _build_reply(result: DispatchResult) -> str:
    if not result.success:
        return result.message
    if result.pr_url:
        return f"{result.message}\n\nJust needs a quick approve: {result.pr_url}"
    return result.message


def _build_sources(config: Config, state: StateStore) -> list[Source]:
    return [
        IMessageSource(
            trigger=config.trigger,
            allowlist=config.allowlist_imessage,
            state=state,
        )
    ]


def run_forever() -> None:
    _configure_logging()
    config = load_config(CONFIG_PATH)
    state = StateStore(STATE_PATH)
    dispatcher = Dispatcher(
        config.target_repo,
        config.claude,
        log_path=DISPATCH_LOG_PATH,
        attachments_dir=ATTACHMENTS_DIR,
        state=state,
    )
    sources = _build_sources(config, state)

    logger.info(
        "agent-modifier starting: trigger=%r target_repo=%s poll_interval=%ss",
        config.trigger,
        config.target_repo.path,
        config.poll_interval_seconds,
    )

    while True:
        for source in sources:
            try:
                commands = source.poll()
            except Exception:
                logger.exception("poll failed for source %s", source.name)
                continue

            for command in commands:
                logger.info(
                    "dispatching command from %s (%s): %r",
                    command.sender_id,
                    command.source,
                    command.instruction,
                )
                result = dispatcher.dispatch(command)
                logger.info("dispatch result for %s: %s", command.raw_message_id, result)
                try:
                    source.reply(command, _build_reply(result))
                except Exception:
                    logger.exception("failed to send reply for %s", command.raw_message_id)

        time.sleep(config.poll_interval_seconds)


if __name__ == "__main__":
    run_forever()
