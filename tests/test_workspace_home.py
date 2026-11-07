"""Home: the per-workspace paths and mode, and the current-home context."""

import asyncio

import pytest

from sellbird.workspace_home import (
    Home,
    NotOnHosted,
    current_home,
    default_home,
    require_self_hosted,
    use_home,
)


def test_for_root_derives_every_path(tmp_path):
    h = Home.for_root(tmp_path)
    assert h.root == tmp_path
    assert h.data_dir == tmp_path / "data"
    assert h.db_path == tmp_path / "data" / "sellbird.db"
    assert h.env_file == tmp_path / ".env"
    assert h.prompts_dir == tmp_path / "prompts"
    assert h.skills_dir == tmp_path / "skills"
    assert h.m365_token_file == tmp_path / "data" / "m365_token.json"
    assert h.gmail_token_file == tmp_path / "data" / "gmail_token.json"
    assert h.dashboard_token_file == tmp_path / "data" / "dashboard-token"
    assert h.agent_token_file == tmp_path / "data" / "agent-token"
    assert h.config_file == tmp_path / "sellbird.yaml"
    assert h.mode == "self_hosted" and not h.hosted
    assert h.secret_backend is None


def test_for_root_hosted_and_backend(tmp_path):
    backend = object()
    h = Home.for_root(tmp_path, mode="hosted", secret_backend=backend)
    assert h.hosted and h.secret_backend is backend


def test_default_home_follows_patched_dashboard_db_path(monkeypatch, tmp_path):
    import sellbird.dashboard as dash

    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "x" / "sellbird.db")
    assert current_home().db_path == tmp_path / "x" / "sellbird.db"
    assert current_home().data_dir == tmp_path / "x"


def test_default_home_reads_legacy_globals():
    import sellbird.dashboard as dash
    import sellbird.integrations.microsoft as ms
    from sellbird.paths import PROJECT_ROOT

    h = default_home()
    assert h.root == PROJECT_ROOT
    assert h.config_file is None and not h.hosted
    assert h.dashboard_token_file == dash.TOKEN_FILE
    assert h.agent_token_file == h.data_dir / "agent-token"
    assert h.m365_token_file == ms.TOKEN_FILE
    assert h.env_file == PROJECT_ROOT / ".env"
    assert h.prompts_dir == PROJECT_ROOT / "prompts"
    assert h.skills_dir == PROJECT_ROOT / "skills"


def test_use_home_sets_and_resets(tmp_path):
    a = Home.for_root(tmp_path / "a")
    with use_home(a):
        assert current_home() is a
    assert current_home() != a


def test_use_home_resets_after_exception(tmp_path):
    a = Home.for_root(tmp_path / "a")
    with pytest.raises(RuntimeError):
        with use_home(a):
            raise RuntimeError("boom")
    assert current_home() != a


@pytest.mark.asyncio
async def test_concurrent_tasks_keep_their_own_home(tmp_path):
    homes = [Home.for_root(tmp_path / n) for n in ("a", "b")]

    async def worker(h):
        seen = []
        with use_home(h):
            for _ in range(10):
                seen.append(current_home())
                await asyncio.sleep(0)
        return seen

    results = await asyncio.gather(*(worker(h) for h in homes))
    for h, seen in zip(homes, results):
        assert len(seen) == 10
        assert all(s is h for s in seen)


@pytest.mark.asyncio
async def test_to_thread_inherits_home(tmp_path):
    a = Home.for_root(tmp_path / "a")
    with use_home(a):
        seen = await asyncio.to_thread(current_home)
    assert seen is a


def test_require_self_hosted_raises_in_hosted(tmp_path):
    with use_home(Home.for_root(tmp_path, mode="hosted")):
        with pytest.raises(NotOnHosted) as e:
            require_self_hosted("local_training")
    assert e.value.payload["error"] == "not_on_hosted"
    message = e.value.payload["message"]
    assert message.startswith("This runs only on your own computer (training through")
    assert "on the server instead" in message


def test_require_self_hosted_passes_when_self_hosted(tmp_path):
    with use_home(Home.for_root(tmp_path)):
        require_self_hosted("local_training")


def test_an_unknown_local_only_feature_is_a_bug(tmp_path):
    with pytest.raises(ValueError, match="unknown local-only feature"):
        require_self_hosted("training")


def test_every_local_only_feature_says_why():
    from sellbird.workspace_home import HOSTED_REFUSED, LOCAL_ONLY

    assert HOSTED_REFUSED == tuple(LOCAL_ONLY)
    assert all(reason.strip() for reason in LOCAL_ONLY.values())


def test_brain_reads_prompts_and_skills_from_the_current_home(tmp_path):
    from sellbird.brain import Brain

    (tmp_path / "prompts").mkdir()
    (tmp_path / "skills").mkdir()
    (tmp_path / "prompts" / "writer.md").write_text("hi {{name}}", encoding="utf-8")
    (tmp_path / "skills" / "tone.md").write_text("be brief", encoding="utf-8")
    brain = Brain.__new__(Brain)  # load_prompt/load_skill need no config
    with use_home(Home.for_root(tmp_path)):
        assert brain.load_prompt("writer", name="Dana") == "hi Dana"
        assert brain.load_skill("tone") == "be brief"
