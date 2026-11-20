"""Project restrictions through busy native Telegram admission and durable replies."""

# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.

import asyncio

import pytest
from telegram_helpers import update as raw
from test_project_controls import (  # noqa: F401 -- shared enrollment fixtures
    controls,
    enrollment,
)
from test_supervisor import ROOT, durable
from test_telegram_runtime import boundary  # noqa: F401 -- installed bridge boundary

from emuru.projects import supervisor


@pytest.fixture
def supervised(boundary, controls):
    home, grant, card, queue, _deliver = controls
    boundary.queue = boundary.execution.queue = boundary.bridge.queue = queue
    boundary.execution.mutation_target = (home, home)
    return boundary, home, grant, card


def test_hold_commits_while_conversation_waits_without_model_dispatch(supervised):
    b, home, grant, card = supervised

    async def check():
        b.queue.stage(7, [raw(1, "wait")])
        await b.adapter.connect()
        try:
            await asyncio.wait_for(b.started.wait(), 1)
            token = supervisor.status(home, ROOT, grant["project"], card)["token"]
            text = f"/project hold {grant['proposal']} {card.id} {token}"
            b.bot.batch = [b.Update(raw(2, text))]
            await b.bot.get_updates()
            await b.app.process_update(b.bot.batch[0])
            assert (
                supervisor.status(home, ROOT, grant["project"], card)["control"]
                == "held"
            )
            assert b.effects == ["wait"]
            assert not b.release.is_set()
            assert b.queue.status() == {"started": 1, "completed": 1}
        finally:
            b.release.set()
            await b.bridge.stop()

    asyncio.run(check())


def test_failed_delivery_reopens_committed_outcome_without_control_replay(supervised):
    b, home, grant, card = supervised

    async def check():
        token = supervisor.status(home, ROOT, grant["project"], card)["token"]
        text = f"/project hold {grant['proposal']} {card.id} {token}"
        b.queue.stage(7, [raw(1, text)])
        b.bot.fail_send = True
        await b.bridge.local_status()
        held = supervisor.status(home, ROOT, grant["project"], card)
        assert held["control"] == "held"
        assert b.queue.status() == {"failed": 1}
        b.bot.fail_send = False
        before = durable(home)
        await b.execution.notice(1)
        assert "held; admitted stage" in b.network[-1][1]["text"]
        assert durable(home) == before
        assert supervisor.status(home, ROOT, grant["project"], card) == held
        assert b.effects == []

    asyncio.run(check())
