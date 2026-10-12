"""Real signals and durable receipts with a synthetic execution adapter; no network."""

import asyncio
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from emuru.telegram.native import NativeTelegram
from emuru.telegram.queue import TelegramQueue, diagnostics, load_telegram_settings
from emuru.telegram.worker import TelegramWorker, TurnResult

ROOT = Path(__file__).resolve().parents[1]


def queue_at(directory):
    return TelegramQueue(
        directory / "queue.sqlite3",
        load_telegram_settings(ROOT / "infra/hermes/telegram-settings.json"),
        42,
        clock=lambda: 100000,
    )


async def child(directory, mode):
    from telegram_helpers import DeterministicTelegram, update

    queue = queue_at(directory)
    queue.stage(7, [update(1, "tool"), update(2, "pending")])
    events, done = [], asyncio.Event()
    polling_stopped = asyncio.Event()

    async def execute(payload):
        queue.mark_tools_started(7, payload["update_id"])
        events.append("tool_started")
        print("TURN_STARTED", flush=True)
        if mode == "complete":
            await polling_stopped.wait()
            events.append("final_response")
            return TurnResult(True)
        await asyncio.Event().wait()

    worker = TelegramWorker(queue, DeterministicTelegram(execute))
    execution = object.__new__(NativeTelegram)
    execution.worker, execution.queue, execution.turn_timeout = worker, queue, 0.3

    async def stop_polling():
        assert worker._stopping
        events.append("polling_stopped")
        polling_stopped.set()

    execution._app = NS(updater=NS(running=True, stop=stop_polling))

    class Runner:
        async def start(self):
            return True

        async def stop(self, **kwargs):
            assert worker._task is None
            events.append("native_stopped")
            done.set()

    run = NS(
        GatewayRunner=Runner,
        start_gateway=None,
        _exit_after_graceful_shutdown=lambda code: None,
    )
    execution.install_launch_guards(
        run, NS(_attach_to_host_gateway_or_guard=None), NS()
    )
    runner = Runner()
    asyncio.get_running_loop().add_signal_handler(
        signal.SIGTERM, lambda: asyncio.create_task(runner.stop())
    )
    worker.start()
    await done.wait()
    queue.close()
    print(json.dumps(events), flush=True)


def check():
    for mode in ("complete", "timeout", "kill"):
        with tempfile.TemporaryDirectory(prefix="emuru-shutdown-") as name:
            directory = Path(name)
            process = subprocess.Popen(
                [sys.executable, __file__, "--child", name, mode],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert select.select([process.stdout], [], [], 10)[0], (
                    "Worker startup timed out"
                )
                assert process.stdout.readline().strip() == "TURN_STARTED"
                process.send_signal(
                    signal.SIGKILL if mode == "kill" else signal.SIGTERM
                )
                stdout, stderr = process.communicate(timeout=5)
                assert process.returncode == (
                    -signal.SIGKILL if mode == "kill" else 0
                ), stderr
                report = diagnostics(directory / "queue.sqlite3")
                state = {
                    "complete": "completed",
                    "timeout": "interrupted",
                    "kill": "started",
                }[mode]
                assert report["queue"] == {state: 1, "queued": 1}, report
                if mode != "kill":
                    events = json.loads(stdout.splitlines()[-1])
                    expected = ["tool_started", "polling_stopped"]
                    if mode == "complete":
                        expected.append("final_response")
                    assert events == expected + ["native_stopped"], events
                queue = queue_at(directory)
                try:
                    queue.recover()
                    assert queue.status() == {
                        "completed" if mode == "complete" else "interrupted": 1,
                        "queued": 1,
                    }
                    if mode != "complete":
                        assert (
                            "Some actions may already have completed"
                            in queue.terminal_notice(7, 1)
                        )
                    assert queue.claim(7)["update_id"] == 2
                    assert queue.claim(7) is None
                finally:
                    queue.close()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    print("Synthetic SIGTERM completion, deadline and SIGKILL no-replay recovery: PASS")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        os.umask(0o077)
        asyncio.run(child(Path(sys.argv[2]), sys.argv[3]))
    else:
        check()
