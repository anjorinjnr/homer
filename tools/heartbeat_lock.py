"""Cross-process advisory locking for HEARTBEAT.md read-modify-write.

Mirrors nanobot.utils.heartbeat_lock; kept as a small standalone copy here
so Homer's tools don't pull in nanobot for local development. The lock
file path (.heartbeat.lock in the workspace dir) MUST stay in sync with
the nanobot copy or the heartbeat service and tasks_update.py won't
serialize against each other.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOCK_FILENAME = ".heartbeat.lock"


@contextmanager
def heartbeat_lock(workspace: Path | str) -> Iterator[None]:
    """Acquire an exclusive flock for the workspace's HEARTBEAT.md.

    Not reentrant: nesting in the same process deadlocks (flock is
    per open file description, and each ``with`` opens a fresh fd).
    """
    lock_path = Path(workspace) / LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding=encoding)
    os.replace(tmp, path)
