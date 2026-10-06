"""Durable FIFO orchestration with no Hermes, PTB, or native scheduling knowledge."""

import asyncio
import logging
from dataclasses import dataclass

from emuru.hermes.profile import HealthError
from emuru.telegram import queue as queue_module

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnResult:
    success: bool
    error_code: str | None = None
    interrupted: bool = False


class TelegramWorker:
    """Adapters own execution; this module owns admission and durable outcomes.

    Execution supplies identity(), wait_ready(), route(payload),
    execute(payload, session_key), and notice(update_id, status=None).
    The optional health callback owns process reporting, never queue recovery.
    """

    def __init__(self, queue, execution, *, health=lambda code: None):
        self.queue, self.execution, self._health = queue, execution, health
        self._wake, self._running = asyncio.Event(), asyncio.Event()
        self._task = None
        self._bot_id = None
        self._stopping = False
        self.retired = self.intake_failed = False
        self.health_error = None

    def ensure_accepting(self):
        if self.intake_failed:
            raise RuntimeError("Telegram intake stopped after a durable queue failure")
        if self.retired:
            raise RuntimeError("An interrupted native turn requires a gateway restart")

    def start(self):
        self.ensure_accepting()
        bot_id, _ = self.execution.identity()
        if self._bot_id is not None and self._bot_id != bot_id:
            raise HealthError("consumer_bot_scope_changed")
        self._bot_id = bot_id
        if self._task is not None and self._task.done():
            raise HealthError(self.health_error or "consumer_exited")
        if self._task is None:
            self._stopping = False
            self._running.clear()
            self._task = asyncio.create_task(self.run(), name="emuru-telegram-worker")
            self._task.add_done_callback(self._worker_done)
        self._wake.set()

    def stage(self, bot_id, updates, username=""):
        """Commit intake before acknowledgement; wake only after the whole batch commits."""
        self.ensure_accepting()
        try:
            if self._bot_id is not None and self._bot_id != bot_id:
                raise HealthError("consumer_bot_scope_changed")
            result = self.queue.stage(bot_id, updates, username)
        except Exception:
            self.intake_failed = True
            raise
        self._wake.set()
        return result

    def _worker_done(self, task):
        error = None if task.cancelled() else task.exception()
        if not self._stopping:
            self.health_error = "consumer_failed" if error else "consumer_exited"
            self.intake_failed = True
            self._health(self.health_error)

    async def require_consumer(self):
        await asyncio.sleep(0)
        if (
            self._task is None
            or self._task.done()
            or not self._running.is_set()
            or self._bot_id != self.execution.identity()[0]
            or self.intake_failed
        ):
            raise HealthError(self.health_error or "consumer_not_running")
        self._health(None)

    async def local_status(self):
        bot, username = self.execution.identity()
        while row := self.queue.claim(bot, username, local_status=True):
            try:
                health = self.health_error or (
                    "consumer_running"
                    if self._task and not self._task.done() and self._running.is_set()
                    else "consumer_not_running"
                )
                await self.execution.notice(
                    row["update_id"],
                    queue_module.diagnostics(self.queue.path, process_health=[health]),
                )
            except asyncio.CancelledError:
                self.queue.mark_interrupted(row["sequence"])
                raise
            except Exception:  # noqa: BLE001 -- never expose private SDK exception text
                self.queue.finish(row["sequence"], False, "status_reply_failed")
            else:
                self.queue.finish(row["sequence"], True)

    async def once(self):
        if self.retired:
            raise RuntimeError("An interrupted native turn requires a gateway restart")
        await self.local_status()
        # Readiness is opaque: the adapter waits before a durable claim is made.
        await self.execution.wait_ready()
        bot, username = self.execution.identity()
        row = self.queue.claim_report(bot)
        if row:
            try:
                await self.execution.notice(row["update_id"])
            except Exception:  # noqa: BLE001 -- one attempt; no credential-bearing errors
                logger.warning("EMURU failure notice could not be delivered")
            return True
        row = self.queue.claim(bot, username, session_key=self.execution.route)
        if row is None:
            return False
        try:
            outcome = await self.execution.execute(
                queue_module.as_update(row), row["session_key"]
            )
            if not isinstance(outcome, TurnResult):
                raise TypeError("Execution adapter must return TurnResult")
        except asyncio.CancelledError:
            self.retired = True
            self.queue.mark_interrupted(row["sequence"])
            raise
        except Exception as error:  # noqa: BLE001 -- terminal without replay
            logger.warning("EMURU Telegram turn failed; it will not be replayed")
            self.queue.finish(
                row["sequence"], False, queue_module.safe_error_code(error)
            )
        else:
            if outcome.interrupted:
                self.retired = True
                self.queue.mark_interrupted(row["sequence"])
            else:
                self.queue.finish(row["sequence"], outcome.success, outcome.error_code)
        return True

    async def run(self):
        self._running.set()
        while True:
            self._wake.clear()
            while await self.once():
                pass
            await self._wake.wait()

    async def stop(self):
        self._stopping = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._running.clear()
