"""Shared one-shot Scout/coder exclusion lock."""

import fcntl
import os
from contextlib import contextmanager

from emuru.vault import mutations


@contextmanager
def dispatcher_lock(home):
    with mutations.directory(home) as parent:
        mutations.private_directory(parent)
    folder = home / "emuru-projects"
    with mutations.directory(folder) as parent:
        mutations.private_directory(parent)
        fd = os.open(
            "scout.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=parent,
        )
        try:
            mutations.regular(fd, private=True)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)
