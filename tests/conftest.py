"""Shared test setup.

Brain's Claude path checks the installed CLI's ``--help`` before sending a
prompt (sellbird/claude_runtime.py). Unit tests stub the subprocess itself, so
they also get a stand-in CLI here: found as plain ``claude``, with a help
text listing every isolation flag. Tests of the check itself override these,
and ``@pytest.mark.real_claude_cli`` tests use the real installed CLI. This
lives in tests/ only; the shipped package has no way to skip the check.
"""

import os

# An exported SELLBIRD_HOME (manual checks) must not move the home that
# sellbird.paths resolves at import, so drop it before any sellbird import.
# Tests that need it pass env= to resolve_home or use monkeypatch.
os.environ.pop("SELLBIRD_HOME", None)

import pytest  # noqa: E402

from sellbird import claude_runtime  # noqa: E402

FAKE_HELP = "\n".join(f"  {flag} <value>   test" for flag in claude_runtime.REQUIRED_FLAGS)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_claude_cli: runs the installed Claude CLI (no stand-in)")


@pytest.fixture(autouse=True)
def _private_dashboard_token(tmp_path_factory):
    """Never create or read the checkout's real data/dashboard-token."""
    import sellbird.dashboard as dash

    dash.token_store.configure(tmp_path_factory.mktemp("token") / "dashboard-token")
    yield
    dash.token_store.configure(dash.TOKEN_FILE)


@pytest.fixture(autouse=True)
def _stand_in_claude_cli(request, monkeypatch):
    claude_runtime.clear_cache()
    if request.node.get_closest_marker("real_claude_cli"):
        yield
        claude_runtime.clear_cache()
        return
    monkeypatch.setattr(claude_runtime, "find_claude", lambda env=None: "claude")
    monkeypatch.setattr(claude_runtime, "_probe_help", lambda exe: FAKE_HELP)
    yield
    claude_runtime.clear_cache()


import sellbird.humanize  # noqa: E402


@pytest.fixture(autouse=True)
def _no_installed_humanizer(monkeypatch, tmp_path):
    """Tests never pick up the operator's real ~/.claude Humanizer skill."""
    monkeypatch.setattr(sellbird.humanize, "SKILL_PATH", tmp_path / "no-humanizer.md")


# Every CLI command seeds the home folder first (sellbird.home). Tests that
# call cli.main() would create the checkout's data/, so seeding is a no-op
# here; tests of seeding itself call the real function.
@pytest.fixture(autouse=True)
def _no_home_seeding(monkeypatch):
    import sellbird.home

    monkeypatch.setattr(sellbird.home, "seed_home", lambda home, source: [])
