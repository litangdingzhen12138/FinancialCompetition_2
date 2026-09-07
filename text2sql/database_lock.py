"""Process-local coordination for DuckDB readers and administrative writes."""

from __future__ import annotations

from pathlib import Path
from threading import Lock, RLock


_LOCKS: dict[str, RLock] = {}
_LOCKS_GUARD = Lock()


def database_lock(path: Path) -> RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _LOCKS[key] = lock
        return lock
