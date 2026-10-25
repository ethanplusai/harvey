"""Real SQLite Profile integration; deterministic decisions do not measure model quality."""

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

pytest.importorskip("powercontext.builtin.runtime")

from harvey.config import PowerContextConfig
from harvey.integrations import powercontext_brain
from harvey.integrations.powercontext import PowerContextProvider, PowerContextUnavailable
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.state import StateManager


PROFILE = "# Contact\nChen is the database technical lead. Prefers concise Chinese action items."
CHANGED_PROFILE = "# Contact\nChen is the database technical lead. Prefers concise English action items."


@pytest_asyncio.fixture
async def customer(tmp_path):
    state = StateManager(str(tmp_path / "harvey.db"))
    await state.init_db()
    prospect = Prospect(first_name="Chen", email="chen@example.test")
    prospect.id = await state.add_prospect(prospect)
    conversation = Conversation(id="", prospect_id=prospect.id, thread=[
        Message(sender="prospect", content="I am the database technical lead. I prefer concise Chinese action items."),
        Message(sender="prospect", content="This PostgreSQL POC uses 20 GB synthetic data in the EU only."),
    ])
    conversation.id = await state.add_conversation(conversation)
    return state, prospect, conversation


@pytest.fixture
def profile_model(monkeypatch):
    class Decisions:
        requests = []
        fail = False

        async def generate(self, value):
            self.requests.append(value)
            if self.fail:
                raise RuntimeError("private provider detail")
            if any("English" in source for source in value.sources):
                return CHANGED_PROFILE
            if value.previous_content:
                return None
            return PROFILE

    model = Decisions()
    monkeypatch.setattr(powercontext_brain, "build_profile_generator", lambda brain: model, raising=False)
    return model


@asynccontextmanager
async def provider(state, **kwargs):
    value = PowerContextProvider(PowerContextConfig(
        enabled=True, memory_families=["profile"], **kwargs,
    ), state, object())
    try:
        await value.start()
        yield value
    finally:
        await value.aclose()


@pytest.mark.asyncio
async def test_profile_uses_sources_and_survives_restart_without_query_overlap(customer, profile_model):
    state, prospect, conversation = customer
    async with provider(state) as value:
        content = await value.prepare(prospect, "What's next?", conversation_id=conversation.id)
        assert all(line in content for line in PROFILE.splitlines())
        assert "20 GB" not in content
        scope = await value.resolve_scope(prospect)
        policy = await value._runtime.profiles.get_policy(scope)
        assert len(profile_model.requests) == 1
        assert any("20 GB" in source for source in profile_model.requests[0].sources)
    async with provider(state) as reopened:
        content = await reopened.prepare(prospect, "明天是否方便？", conversation_id=conversation.id)
        assert all(line in content for line in PROFILE.splitlines())
        assert len(profile_model.requests) == 1
        assert (await reopened._runtime.profiles.get_policy(scope)).version == policy.version


@pytest.mark.asyncio
async def test_profile_changes_with_new_evidence_and_is_contact_isolated(customer, profile_model):
    state, prospect, conversation = customer
    async with provider(state) as value:
        await value.capture_conversation(prospect, conversation.id)
        conversation.thread.append(Message(sender="prospect", content="Please use English action items from now on."))
        await state.update_conversation(conversation.id, thread_json=conversation.thread_json())
        result = await value.prepare(prospect, "Next steps?", conversation_id=conversation.id)
        assert all(line in result for line in CHANGED_PROFILE.splitlines()) and "Chinese" not in result
        assert profile_model.requests[-1].previous_content.rstrip() == PROFILE
        other = Prospect(id="different-contact", email="other@example.test")
        assert await value.prepare(other, "Next steps?") == ""


@pytest.mark.asyncio
async def test_profile_failure_remains_retryable_after_restart(customer, profile_model):
    state, prospect, conversation = customer
    profile_model.fail = True
    async with provider(state) as value:
        with pytest.raises(PowerContextUnavailable):
            await value.capture_conversation(prospect, conversation.id)
    profile_model.fail = False
    async with provider(state) as value:
        content = await value.prepare(prospect, "Next steps?")
        assert all(line in content for line in PROFILE.splitlines())


@pytest.mark.asyncio
async def test_existing_disabled_profile_policy_is_not_overwritten(customer, profile_model):
    state, prospect, conversation = customer
    async with provider(state) as value:
        scope = await value.resolve_scope(prospect)
        policy = await value._runtime.profiles.put_policy(
            scope, generation_enabled=False, activation_mode="automatic", expected_version=0,
        )
        with pytest.raises(PowerContextUnavailable):
            await value.prepare(prospect, "Next steps?", conversation_id=conversation.id)
        saved = await value._runtime.profiles.get_policy(scope)
        assert not saved.generation_enabled and saved.version == policy.version
        assert not profile_model.requests
