"""sellbird.storage: SQLite by default, other backends by locator prefix."""

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from sellbird import storage
from sellbird.state import StateManager


class _Recorder:
    """A backend over SQLite files in one folder, recording what it was asked."""

    def __init__(self, folder: Path, *, schema_ok: bool = True):
        self.folder = folder
        self.schema_ok = schema_ok
        self.opened: list[str] = []
        self.checked: list[str] = []

    async def connect(self, name: str, *, timeout: float):
        self.opened.append(name)
        return await aiosqlite.connect(self.folder / f"{name}.db", timeout=timeout)

    async def check_schema(self, name: str) -> None:
        self.checked.append(name)
        if not self.schema_ok:
            raise RuntimeError("schema is behind")

    def exists(self, name: str) -> bool:
        return (self.folder / f"{name}.db").exists()


@pytest.fixture
def backend(tmp_path):
    b = _Recorder(tmp_path)
    storage.register_backend("rec", b)
    yield b
    storage.unregister_backend("rec")


@pytest.mark.parametrize("locator", ["data/sellbird.db", "/abs/sellbird.db", ":memory:",
                                     r"C:\Users\x\sellbird.db", "C:/x/sellbird.db"])
def test_paths_are_sqlite(locator):
    assert storage.split(locator) == (None, locator)
    assert not storage.manages_schema(locator)


def test_unknown_prefix_is_an_error_not_a_file():
    with pytest.raises(LookupError, match="'nope:'"):
        storage.split("nope:workspace")


def test_register_rejects_bad_prefixes_and_objects(tmp_path):
    with pytest.raises(ValueError):
        storage.register_backend("", _Recorder(tmp_path))
    with pytest.raises(ValueError):
        storage.register_backend("a b", _Recorder(tmp_path))
    with pytest.raises(TypeError):
        storage.register_backend("x", object())


@pytest.mark.asyncio
async def test_sqlite_connect_opens_and_closes(tmp_path):
    path = tmp_path / "x.db"
    async with storage.connect(path) as db:
        await db.execute("CREATE TABLE t (a)")
        await db.commit()
    assert storage.exists(path)
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT name FROM sqlite_master").fetchone() == ("t",)


@pytest.mark.asyncio
async def test_backend_locator_goes_to_the_backend(backend):
    assert storage.manages_schema("rec:acme")
    async with storage.connect("rec:acme") as db:
        await db.execute("CREATE TABLE t (a)")
        await db.commit()
    assert backend.opened == ["acme"] and storage.exists("rec:acme")


@pytest.mark.asyncio
async def test_state_on_a_backend_checks_the_schema_and_never_migrates(backend):
    state = StateManager(db_path="rec:acme")
    await state.init_db()
    assert backend.checked == ["acme"]
    assert backend.opened == []          # no SQLite migrations ran against it


@pytest.mark.asyncio
async def test_a_behind_schema_stops_init(backend):
    backend.schema_ok = False
    with pytest.raises(RuntimeError, match="behind"):
        await StateManager(db_path="rec:acme").init_db()


@pytest.mark.asyncio
async def test_state_on_sqlite_still_migrates(tmp_path):
    state = StateManager(db_path=str(tmp_path / "s.db"))
    await state.init_db()
    with sqlite3.connect(tmp_path / "s.db") as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] > 0


def test_a_bare_state_manager_uses_the_current_homes_database(tmp_path):
    from sellbird.workspace_home import Home, use_home

    with use_home(Home.for_root(tmp_path)):
        assert StateManager().db_path == str(tmp_path / "data" / "sellbird.db")


def test_entry_point_backends_load_on_first_use(monkeypatch, tmp_path):
    made = []

    class Factory:
        def __init__(self):
            made.append(self)

        async def connect(self, name, *, timeout):  # pragma: no cover - not opened
            raise AssertionError

        async def check_schema(self, name):  # pragma: no cover
            pass

        def exists(self, name):
            return True

    class EP:
        name = "epb"

        def load(self):
            return Factory

    monkeypatch.setattr(storage, "_ENTRY_POINTS_LOADED", False)
    monkeypatch.setattr(storage, "entry_points", lambda group: [EP()] if group ==
                        storage.ENTRY_POINT_GROUP else [])
    monkeypatch.setattr(storage, "_BACKENDS", {})
    assert storage.exists("epb:anything") and len(made) == 1
    assert storage.exists("epb:again") and len(made) == 1


def test_integrity_error_is_sqlites():
    assert issubclass(storage.IntegrityError, sqlite3.IntegrityError)


# ── Shared connections (one per process, for the dashboard and heartbeat) ──


@pytest_asyncio.fixture
async def shared(tmp_path):
    storage.share_connections(True)
    try:
        yield str(tmp_path / "s.db")
    finally:
        await storage.close_shared()
        storage.share_connections(False)


@pytest.mark.asyncio
async def test_sharing_hands_out_one_connection(shared):
    async with storage.connect(shared) as a:
        await a.execute("CREATE TABLE t (x)")
        await a.commit()
    async with storage.connect(shared) as b:
        assert a is b                     # the same connection, not a new one
    storage.share_connections(False)
    async with storage.connect(shared) as c:
        assert c is not a                 # sharing off: a fresh one again
    storage.share_connections(True)


@pytest.mark.asyncio
async def test_sharing_serialises_users_and_cleans_up_after_each(shared):
    import aiosqlite
    async with storage.connect(shared) as db:
        await db.execute("CREATE TABLE t (x)")
        await db.commit()

    order = []

    async def user(name, sleep):
        async with storage.connect(shared) as db:
            order.append(name + ">")
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            await db.execute("INSERT INTO t VALUES (?)", (name,))
            await asyncio.sleep(sleep)
            order.append("<" + name)
            # No commit: the shared connection must not leak this into the next user.

    await asyncio.gather(user("a", 0.02), user("b", 0))
    assert order in (["a>", "<a", "b>", "<b"], ["b>", "<b", "a>", "<a"])
    async with storage.connect(shared) as db:
        assert db.row_factory is None and not db.in_transaction
        async with db.execute("SELECT COUNT(*) FROM t") as cur:
            assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_sharing_is_reentrant_for_the_holding_task(shared):
    async with storage.connect(shared) as outer:
        async with storage.connect(shared) as inner:   # a method calling another
            assert inner is outer
        assert outer.row_factory is None


@pytest.mark.asyncio
async def test_close_shared_closes_and_a_new_use_reopens(shared):
    async with storage.connect(shared) as a:
        pass
    await storage.close_shared()
    async with storage.connect(shared) as b:
        assert b is not a
        async with b.execute("SELECT 1") as cur:
            assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_state_manager_works_on_a_shared_connection(shared):
    from sellbird.state import StateManager
    sm = StateManager(shared)
    await sm.init_db()
    await sm.init_db()                     # idempotent on the same connection
    assert await sm.get_signal_codes() == []
    async with storage.connect(shared) as db:
        async with db.execute("PRAGMA journal_mode") as cur:
            assert (await cur.fetchone())[0] == "wal"


def test_shared_connections_belong_to_their_loop(tmp_path):
    """Each event loop gets its own connection: a test suite, or anything
    else running loops back to back, must never get one bound to a dead loop."""
    path = str(tmp_path / "loops.db")
    storage.share_connections(True)
    try:
        async def use():
            async with storage.connect(path) as db:
                async with db.execute("SELECT 1") as cur:
                    assert (await cur.fetchone())[0] == 1
                return db
        first = asyncio.run(use())
        second = asyncio.run(use())
        assert first is not second
    finally:
        storage.share_connections(False)
