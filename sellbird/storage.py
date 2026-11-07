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
"""

from __future__ import annotations

import sqlite3
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


@asynccontextmanager
async def connect(locator, *, timeout: float = BUSY_TIMEOUT_SECONDS) -> AsyncIterator[Any]:
    """Open the database a locator names, and close it after."""
    backend, name = split(locator)
    if backend is None:
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
