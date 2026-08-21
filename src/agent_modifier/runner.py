from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import Config, load_config
from .dispatcher import Dispatcher
from .models import Command, DispatchResult
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


def _build_reply(result: DispatchResult, agent_name: str) -> str:
    if not result.success:
        body = result.message
    elif result.pr_url:
        body = f"{result.message}\n\nJust needs a quick approve: {result.pr_url}"
    else:
        body = result.message
    return f"{body}\n\n— {agent_name}"


def _build_sources(config: Config, state: StateStore) -> list[Source]:
    return [
        IMessageSource(
            trigger=config.trigger,
            allowlist=config.allowlist_imessage,
            state=state,
        )
    ]


def _apply_cursor_bootstrap(config: Config, state: StateStore, sources: list[Source]) -> None:
    if config.bootstrap_cursor_after is None:
        return
    for source in sources:
        if state.is_bootstrap_applied(source.name) or not isinstance(source, IMessageSource):
            continue
        rowid = source.rowid_before(config.bootstrap_cursor_after)
        logger.warning(
            "one-time cursor bootstrap: seeding %s cursor to rowid=%s (skip everything before %s, "
            "per config bootstrap_cursor_after) -- this replaces whatever cursor was already stored",
            source.name,
            rowid,
            config.bootstrap_cursor_after,
        )
        state.set_last_seen(source.name, rowid)
        state.mark_bootstrap_applied(source.name)


def _recover_pending_dispatch(state: StateStore, sources: list[Source], agent_name: str) -> None:
    """Handle a command that was still in flight when the process last died.

    The cursor never advances past a command until ack() runs right after
    dispatch() returns, so a plain restart is always safe -- the worst case
    is dispatch() gets called again for a command it never got to. But if
    dispatch() itself was interrupted mid-run, real side effects (a git
    commit, a database write) may already have happened, and there's no
    way to know from here. Auto-retrying would risk doing it twice, so
    instead: skip it (advance the cursor past it, same as any other command
    once it's resolved) and tell a human to go check, rather than silently
    redoing possibly-completed work.
    """
    for source in sources:
        pending = state.get_pending_dispatch(source.name)
        if not pending:
            continue
        logger.critical(
            "found an in-flight command from before the last restart on %s "
            "(rowid=%s sender=%s instruction=%r) -- it was NOT auto-retried since it may "
            "have already partially completed. Skipping it and alerting the sender.",
            source.name,
            pending["rowid"],
            pending["sender_id"],
            pending["instruction"],
        )
        command = Command(
            source=source.name,
            sender_id=pending["sender_id"],
            instruction=pending["instruction"],
            raw_message_id=str(pending["rowid"]),
            chat_id=pending["chat_id"],
        )
        source.ack(command)
        state.clear_pending_dispatch(source.name)
        try:
            source.reply(
                command,
                "I got interrupted partway through your last request and I'm not "
                "sure if it finished or not, so I'm not redoing it automatically -- "
                "please check before asking me to try again.\n\n"
                f"— {agent_name}",
            )
        except Exception:
            logger.exception("failed to send crash-recovery alert for %s", pending["rowid"])


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
        agent_name=config.agent_name,
    )
    sources = _build_sources(config, state)

    _apply_cursor_bootstrap(config, state, sources)
    _recover_pending_dispatch(state, sources, config.agent_name)

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
                # Recorded before dispatch() runs so a crash mid-dispatch is
                # detectable on the next startup (see
                # _recover_pending_dispatch) instead of silently retried.
                state.set_pending_dispatch(
                    source.name,
                    {
                        "rowid": int(command.raw_message_id),
                        "sender_id": command.sender_id,
                        "instruction": command.instruction,
                        "chat_id": command.chat_id,
                    },
                )
                result = dispatcher.dispatch(command)
                logger.info("dispatch result for %s: %s", command.raw_message_id, result)
                # Ack right after dispatch (success or failure), before the
                # reply attempt -- dispatch is what's expensive/stateful
                # (spends budget, may push commits or open a PR), so once
                # it's done this command must never be redelivered even if
                # sending the reply below fails.
                source.ack(command)
                state.clear_pending_dispatch(source.name)
                try:
                    source.reply(command, _build_reply(result, config.agent_name))
                except Exception:
                    logger.exception("failed to send reply for %s", command.raw_message_id)

        time.sleep(config.poll_interval_seconds)


if __name__ == "__main__":
    run_forever()
