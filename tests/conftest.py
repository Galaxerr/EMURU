"""Execution requirements shared by MCP integration tests."""

import socket

import pytest


@pytest.fixture(scope="session")
def mcp_environment():
    # Socket creation can succeed while sandbox policy denies asyncio wakeup writes.
    try:
        sender, receiver = socket.socketpair()
        with sender, receiver:
            sender.send(b"\0")
    except OSError as error:
        pytest.fail(
            "MCP tests require local socketpair writes for asyncio thread wakeups. "
            "Rerun with approved local socket IPC permissions: " + str(error),
            pytrace=False,
        )
