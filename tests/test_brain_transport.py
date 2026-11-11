"""A server running Sellbird for others plugs its own model calls in through
``Brain(transport=...)``, and a home with no prompts folder still gets the
shipped prompts and skills."""

import asyncio
from pathlib import Path

import pytest

from sellbird.brain import Brain
from sellbird.workspace_home import Home, use_home


class Transport:
    def __init__(self, answer="ok", paused=""):
        self.answer, self.paused_reason, self.calls = answer, paused, []

    async def __call__(self, prompt, *, agent, task, timeout, expect_json):
        self.calls.append((prompt, agent, task, expect_json))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.asyncio
async def test_think_goes_through_the_transport():
    t = Transport('{"subject": "a b", "body": "hi"}')
    brain = Brain(state=None, transport=t)
    assert await brain.think_json("write", agent="writer", task="personal_email") == {
        "subject": "a b", "body": "hi"}
    prompt, agent, task, _ = t.calls[0]
    assert prompt.startswith("write") and (agent, task) == ("writer", "personal_email")


@pytest.mark.asyncio
async def test_a_failing_transport_answers_empty_not_raises():
    brain = Brain(state=None, transport=Transport(RuntimeError("api down")))
    assert await brain.think("x") == ""


@pytest.mark.asyncio
async def test_a_slow_transport_times_out():
    class Slow(Transport):
        async def __call__(self, prompt, **kw):
            await asyncio.sleep(5)
            return "late"
    assert await Brain(state=None, transport=Slow()).think("x", timeout=0.05) == ""


@pytest.mark.asyncio
async def test_a_paused_transport_pauses_the_brain():
    t = Transport(paused="daily AI budget reached")
    brain = Brain(state=None, transport=t)
    assert brain.paused_reason == "daily AI budget reached"
    assert await brain.think("x") == "" and t.calls == []


def test_a_home_without_prompts_gets_the_shipped_ones(tmp_path):
    nowhere = tmp_path / "nonexistent"
    home = Home.for_root(nowhere)
    with use_home(home):
        brain = Brain(state=None)
        assert "{{persona_company}}" in brain.load_prompt("writer")
        assert brain.load_skill("email_frameworks")
        assert brain.load_skill("product_knowledge_that_does_not_exist") == ""


def test_the_homes_own_prompt_wins(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "writer.md").write_text("mine {{x}}")
    with use_home(Home.for_root(Path(tmp_path))):
        assert Brain(state=None).load_prompt("writer", x="1") == "mine 1"
