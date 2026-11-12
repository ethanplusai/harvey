"""Where Sellbird's database is, and how to open it.

A database is named by a locator: a SQLite file path (the default, and all a
self-hosted install ever uses), ``:memory:``, or ``<prefix>:<name>`` for a
storage backend registered by an extension, such as a server keeping each
workspace in a shared database.

A backend is registered under its prefix, either in code with
``register_backend`` or through the ``sellbird.storage`` entry point group (the
entry point's name is the prefix, its value a zero-argument callable, such as
a class, returning the backend). Entry points load on first use, so a plain install
pays nothing.

A backend hands back a connection shaped like aiosqlite's (``execute``,
``executemany``, ``executescript``, ``commit``, ``close``, ``row_factory``),
so the stores keep one code path. A backend that manages its own schema says
so: ``StateManager.init_db`` then asks it to check the schema instead of
running the SQLite migrations.

A connection is opened per use and closed after, unless the process turned
on ``share_connections()``: the dashboard and the heartbeat do, and then keep
one SQLite connection per database, lent to one user at a time (see the
comment above ``share_connections`` for why).
"""

from __future__ import annotations

import asyncio
import atexit
import sqlite3
import weakref
from contextlib import asynccontextmanager
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, AsyncIterator, Protocol, runtime_checkable

BUSY_TIMEOUT_SECONDS = 30.0
ENTRY_POINT_GROUP = "sellbird.storage"


class IntegrityError(sqlite3.IntegrityError):
    """A constraint failed in a backend other than SQLite. Subclasses sqlite3's,
    so the stores' ``except sqlite3.IntegrityError`` handlers keep working."""


@runtime_checkable
class StorageBackend(Protocol):
    async def connect(self, name: str, *, timeout: float) -> Any:
        """An open, aiosqlite-shaped connection to the database called ``name``."""

    async def check_schema(self, name: str) -> None:
        """Raise if the schema is missing or behind. Never migrates."""

    def exists(self, name: str) -> bool:
        """Whether the database is there."""


_BACKENDS: dict[str, StorageBackend] = {}
_ENTRY_POINTS_LOADED = False


def register_backend(prefix: str, backend: StorageBackend) -> None:
    """Route locators ``<prefix>:<name>`` to ``backend``. Re-registering replaces."""
    if not prefix or not prefix.isidentifier():
        raise ValueError("a storage prefix is a short name like 'pg'")
    if not isinstance(backend, StorageBackend):
        raise TypeError("a storage backend needs connect, check_schema and exists")
    _BACKENDS[prefix] = backend


def unregister_backend(prefix: str) -> None:
    _BACKENDS.pop(prefix, None)


def _load_entry_points() -> None:
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name in _BACKENDS:
            continue
        register_backend(ep.name, ep.load()())


def split(locator) -> tuple[StorageBackend | None, str]:
    """The backend a locator names (None for SQLite) and the name it gives it."""
    text = str(locator)
    prefix, sep, rest = text.partition(":")
    if not sep or text == ":memory:" or not prefix.isidentifier() or len(prefix) == 1:
        # A path, :memory:, or a Windows drive letter (C:\\...).
        return None, text
    _load_entry_points()
    backend = _BACKENDS.get(prefix)
    if backend is None:
        raise LookupError(f"no storage backend is registered for '{prefix}:'")
    return backend, rest


def manages_schema(locator) -> bool:
    """True when a backend, not the SQLite migrations, owns this database's schema."""
    return split(locator)[0] is not None


# ── One connection per process (sharing) ──
#
# By default every ``connect()`` opens a fresh SQLite connection, on a fresh
# aiosqlite thread, and closes it after: simple, and right for a one-shot
# command. A long-lived process is different. The dashboard answers dozens
# of requests a minute, each opening and closing its own connection, and
# under that churn SQLite's file-lock mutexes have been seen to hang for good
# (every request then waits forever and the browser shows them all pending).
# With sharing on, a process keeps one connection per database and event
# loop and hands it out one user at a time: an asyncio lock serialises use,
# re-entrant for the task that already holds it, and each use ends with any
# open transaction rolled back and the row factory cleared, so a caller sees
# the connection as it would a fresh one. Backends are never shared.

_SHARING = False
# Per event loop (weakly: a loop that is gone takes its entry with it), then
# per database path. A connection belongs to the loop it was opened on.
_SHARED: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _SharedConnection]]" = (
    weakref.WeakKeyDictionary())


class _SharedConnection:
    def __init__(self, db) -> None:
        self.db = db
        self.lock = asyncio.Lock()
        self.owner: asyncio.Task | None = None
        self.depth = 0


def share_connections(enabled: bool = True) -> None:
    """Keep one SQLite connection per database for this process (the
    dashboard and the heartbeat), instead of one per use. Call
    ``close_shared()`` before the event loop ends."""
    global _SHARING
    _SHARING = enabled


def sharing_connections() -> bool:
    return _SHARING


async def close_shared() -> None:
    """Close the shared connections opened on the running loop."""
    per_loop = _SHARED.pop(asyncio.get_running_loop(), {})
    for shared in list(per_loop.values()):
        try:
            await shared.db.close()
        except Exception:  # pragma: no cover - closing is best effort
            pass
    per_loop.clear()


def _stop_connections(per_loop: dict) -> None:
    """Free connections whose loop ended without close_shared(): stop their
    threads (aiosqlite's are not daemons, and would keep the process alive)
    and close the files. Runs when the loop is collected, and at exit."""
    for shared in list(per_loop.values()):
        try:
            shared.db.stop()
        except Exception:  # pragma: no cover - best effort
            pass
    per_loop.clear()


def _stop_all_shared() -> None:
    for per_loop in list(_SHARED.values()):
        _stop_connections(per_loop)


atexit.register(_stop_all_shared)


async def _shared(name: str, timeout: float) -> "_SharedConnection":
    loop = asyncio.get_running_loop()
    key = str(Path(name).resolve()) if name != ":memory:" else name
    per_loop = _SHARED.get(loop)
    if per_loop is None:
        per_loop = _SHARED[loop] = {}
        weakref.finalize(loop, _stop_connections, per_loop)
    shared = per_loop.get(key)
    if shared is None:
        shared = _SharedConnection(await _open_daemon(name, timeout))
        per_loop[key] = shared
    return shared


async def _open_daemon(name: str, timeout: float):
    """An aiosqlite connection whose worker thread is a daemon: one left
    open when its loop ends (a crash, a test) must not keep the process from
    exiting, since Python joins ordinary threads before any atexit hook."""
    import aiosqlite

    def connector():
        return sqlite3.connect(name, timeout=timeout, check_same_thread=False)

    conn = aiosqlite.Connection(connector, 64)
    thread = getattr(conn, "_thread", None)   # aiosqlite's worker, before it starts
    if thread is not None and not thread.is_alive():
        thread.daemon = True
    return await conn


@asynccontextmanager
async def _use_shared(shared: "_SharedConnection") -> AsyncIterator[Any]:
    task = asyncio.current_task()
    if shared.owner is task:
        # The task holding the connection asked for it again (a method
        # calling another): same connection, no second lock.
        shared.depth += 1
        try:
            yield shared.db
        finally:
            shared.depth -= 1
        return
    async with shared.lock:
        shared.owner, shared.depth = task, 1
        try:
            yield shared.db
        finally:
            shared.owner, shared.depth = None, 0
            db = shared.db
            try:
                if db.in_transaction:
                    await db.rollback()
            except Exception:  # pragma: no cover - the connection is gone
                pass
            db.row_factory = None


@asynccontextmanager
async def connect(locator, *, timeout: float = BUSY_TIMEOUT_SECONDS) -> AsyncIterator[Any]:
    """Open the database a locator names, and close it after; or, with
    ``share_connections()`` on, lend this process's one connection to it."""
    backend, name = split(locator)
    if backend is None:
        if _SHARING:
            async with _use_shared(await _shared(name, timeout)) as db:
                yield db
            return
        import aiosqlite

        db = await aiosqlite.connect(name, timeout=timeout)
    else:
        db = await backend.connect(name, timeout=timeout)
    try:
        yield db
    finally:
        await db.close()


async def check_schema(locator) -> None:
    backend, name = split(locator)
    if backend is not None:
        await backend.check_schema(name)


def exists(locator) -> bool:
    """Whether the database is there: the file for SQLite, the backend's answer otherwise."""
    backend, name = split(locator)
    return Path(name).exists() if backend is None else backend.exists(name)
