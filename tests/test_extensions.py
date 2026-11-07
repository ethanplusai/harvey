"""sellbird.extensions: what an installed extension can add, and how it's kept apart."""

import asyncio
import sys

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from sellbird import extensions


@pytest.fixture
def registry(monkeypatch):
    """A fresh, already-loaded registry for this test; the process's comes back after."""
    monkeypatch.setattr(extensions, "_REGISTRY", extensions.Registry())
    monkeypatch.setattr(extensions, "_LOADED", True)
    return extensions._REGISTRY


async def _ok(ctx):
    pass


# ── registering ──

@pytest.mark.parametrize("name", ["", "Has Caps", "with space", "-leading", "x" * 65, 3])
def test_names_are_short_lowercase_words(registry, name):
    with pytest.raises(extensions.ExtensionError):
        registry.add_tick_step(name, _ok)
    with pytest.raises(extensions.ExtensionError):
        registry.add_cli_command(name, lambda p: None)


def test_a_tick_step_is_async_and_registered_once(registry):
    with pytest.raises(extensions.ExtensionError, match="async"):
        registry.add_tick_step("ext.sync", lambda ctx: None)
    registry.add_tick_step("ext.sync", _ok)
    with pytest.raises(extensions.ExtensionError, match="already"):
        registry.add_tick_step("ext.sync", _ok)


def test_a_command_is_registered_once(registry):
    registry.add_cli_command("ext", lambda p: None)
    with pytest.raises(extensions.ExtensionError, match="already"):
        registry.add_cli_command("ext", lambda p: None)


# ── loading ──

class _EP:
    def __init__(self, name, install):
        self.name, self._install = name, install

    def load(self):
        return self._install


def _with_entry_points(monkeypatch, eps):
    monkeypatch.setattr(extensions, "_REGISTRY", extensions.Registry())
    monkeypatch.setattr(extensions, "_LOADED", False)
    monkeypatch.setattr(extensions, "entry_points",
                        lambda group: eps if group == extensions.ENTRY_POINT_GROUP else [])


def test_extensions_load_once_in_name_order(monkeypatch):
    order = []
    _with_entry_points(monkeypatch, [_EP("b", lambda r: order.append("b")),
                                     _EP("a", lambda r: order.append("a"))])
    extensions.registry()
    extensions.registry()
    assert order == ["a", "b"]


def test_a_failing_extension_stops_with_its_name(monkeypatch):
    def broken(registry):
        raise RuntimeError("no database")
    _with_entry_points(monkeypatch, [_EP("acme", broken)])
    with pytest.raises(extensions.ExtensionError, match="'acme' failed to install: no database"):
        extensions.registry()


def test_no_extensions_means_an_empty_registry(monkeypatch):
    _with_entry_points(monkeypatch, [])
    reg = extensions.registry()
    assert reg.routers == [] and reg.tick_steps == {} and reg.cli_commands == {}


# ── tick steps ──

@pytest.mark.asyncio
async def test_tick_steps_run_in_order_and_never_stop_each_other(registry):
    ran = []

    async def first(ctx):
        ran.append(("first", ctx.config, ctx.state))

    async def broken(ctx):
        raise ValueError("boom")

    async def slow(ctx):
        await asyncio.sleep(5)

    async def last(ctx):
        ran.append(("last", ctx.config, ctx.state))

    for name, step in [("a.first", first), ("a.broken", broken), ("a.slow", slow), ("a.last", last)]:
        registry.add_tick_step(name, step)
    results = await extensions.run_tick_steps("CFG", "STATE", timeout=0.05)
    assert results == {"a.first": "ok", "a.broken": "failed", "a.slow": "timed out", "a.last": "ok"}
    assert ran == [("first", "CFG", "STATE"), ("last", "CFG", "STATE")]


@pytest.mark.asyncio
async def test_the_heartbeat_runs_tick_steps_each_cycle(registry, monkeypatch, tmp_path):
    from sellbird.state import StateManager
    from tests.test_prospecting_stall import (
        _draft_and_open_conversation, _instantly_with_a_draft_and_an_open_conversation)

    state = StateManager(str(tmp_path / "s.db"))
    await state.init_db()
    await _draft_and_open_conversation(state)
    seen = []

    async def step(ctx):
        seen.append(ctx.state is state)

    registry.add_tick_step("ext.after-cycle", step)
    calls, stop = [], asyncio.Event()
    main_mod = _instantly_with_a_draft_and_an_open_conversation(monkeypatch, state, calls, stop, True)
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)
    assert calls == ["replies", "send"] and seen == [True]


# ── commands ──

def test_an_extension_command_runs_from_the_cli(registry, monkeypatch, capsys):
    import sellbird.cli as cli

    def configure(parser):
        parser.add_argument("--who", default="world")
        return lambda args: print(f"hello {args.who}")

    registry.add_cli_command("ext-hello", configure, help="say hello")
    monkeypatch.setattr(sys, "argv", ["sellbird", "ext-hello", "--who", "there"])
    cli.main()
    assert "hello there" in capsys.readouterr().out


def test_an_extension_cannot_take_a_built_in_command(registry, monkeypatch):
    import sellbird.cli as cli

    registry.add_cli_command("status", lambda p: (lambda args: None))
    monkeypatch.setattr(sys, "argv", ["sellbird", "status"])
    with pytest.raises(extensions.ExtensionError, match="clashes"):
        cli.main()


# ── the dashboard ──

def test_build_app_puts_its_routers_first_and_keeps_the_built_in_routes():
    import sellbird.dashboard as dash

    router = APIRouter()

    @router.get("/api/rename-notice")
    async def mine():
        return {"mine": True}

    @router.get("/api/ext/ping")
    async def ping():
        return {"pong": True}

    class Open:  # stands in for a server's own sign-in
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            await self.app(scope, receive, send)

    from starlette.middleware import Middleware
    built = dash.build_app(guard=Middleware(Open), routers=[router])
    client = TestClient(built)
    assert client.get("/api/ext/ping").json() == {"pong": True}
    assert client.get("/api/rename-notice").json() == {"mine": True}
    assert client.get("/api/setup-status").status_code == 200   # a built-in route, no token


def test_a_built_app_runs_its_requests_in_its_home(tmp_path):
    import sellbird.dashboard as dash
    from sellbird.workspace_home import Home, current_home

    router = APIRouter()

    @router.get("/api/ext/home")
    def which():  # a sync handler: runs in the threadpool
        return {"root": str(current_home().root)}

    class Open:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            await self.app(scope, receive, send)

    from starlette.middleware import Middleware
    built = dash.build_app(guard=Middleware(Open), routers=[router])
    built.state.home = Home.for_root(tmp_path)
    assert TestClient(built).get("/api/ext/home").json() == {"root": str(tmp_path)}


def test_a_local_only_feature_is_a_409_on_a_hosted_home(tmp_path):
    import sellbird.dashboard as dash
    from sellbird.workspace_home import Home, require_self_hosted

    router = APIRouter()

    @router.post("/api/ext/train")
    async def train():
        require_self_hosted("desktop")

    class Open:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            await self.app(scope, receive, send)

    from starlette.middleware import Middleware
    built = dash.build_app(guard=Middleware(Open), routers=[router])
    built.state.home = Home.for_root(tmp_path, mode="hosted")
    response = TestClient(built).post("/api/ext/train")
    assert response.status_code == 409 and response.json()["error"] == "not_on_hosted"
