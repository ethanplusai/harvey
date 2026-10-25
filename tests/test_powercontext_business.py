"""Customer business regressions through real SQLite and SDK Topic supervision.

Only Profile/Topic generation decisions are deterministic doubles. These tests
verify host integration, durable publication and returned evidence, not model
quality or the recall rate of a real language model.
"""

import json
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

pytest.importorskip("powercontext.builtin.runtime")

from powercontext.builtin.artifacts.topic_memory import TopicMemoryContent
from powercontext.builtin.artifacts.topic_memory.generation import (
    TopicMemoryGlobalOutput,
    TopicMemoryProbe,
    TopicMemoryProbeOutput,
    TopicMemoryProposal,
    TopicMemoryReconcileOutput,
)
from powercontext.builtin.inference import GenerationResult, character_token_estimator
from powercontext.builtin.runtime.topic_memory_processing import TopicMemoryStageSet

from harvey.config import PowerContextConfig
from harvey.integrations import powercontext_brain
from harvey.integrations.powercontext import PowerContextProvider
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.state import StateManager


PROFILE_SOURCE = "我是数据库技术负责人。请始终使用简洁中文沟通，并列出明确的待办事项。"
PROFILE = "客户是数据库技术负责人；稳定沟通偏好是简洁中文和明确的待办事项。"
POSTGRES = "PostgreSQL POC：只用 20 GB 合成数据；全程限定欧盟境内；禁止索取生产数据。"
POSTGRES_UPDATED = "PostgreSQL POC：数据规模现改为 50 GB 合成数据；仍全程限定欧盟境内；仍禁止索取生产数据。"
CLICKHOUSE = "ClickHouse POC 是独立项目：使用 200 GB 脱敏数据；处理地区是美国。"
OTHER_POSTGRES = "PostgreSQL POC：另一客户仅准备 75 GB 合成数据；处理地区是加拿大。"
SHORT_QUESTION = "下一步怎么安排？需要提前准备什么数据？"


class BusinessDecisions:
    """A controlled multi-topic model; all persistence and search remain real."""

    def __init__(self):
        self.topic_windows = 0
        self.profile_windows = 0
        self.profile_requests = []

    @staticmethod
    def source_text(item):
        # Captured Sources expose a JSON envelope containing content and metadata.
        try:
            decoded = json.loads(item.content)
        except (ValueError, TypeError):
            return item.content
        return decoded.get("content", item.content) if isinstance(decoded, dict) else item.content

    @classmethod
    def project_evidence(cls, evidence, project):
        # Fixture decisions retain requirements, not greetings or scheduling chatter.
        return tuple(item for item in evidence if project in cls.source_text(item) and "GB" in cls.source_text(item))

    async def generate(self, value):
        stage = type(value).__name__
        if stage == "TopicMemoryProbeInput":
            self.topic_windows += 1
            return GenerationResult(output=TopicMemoryProbeOutput(probes=tuple(
                TopicMemoryProbe(query=project, evidence_ids=tuple(item.evidence_id for item in evidence))
                for project in ("PostgreSQL", "ClickHouse")
                if (evidence := self.project_evidence(value.evidence, project))
            )))
        if stage == "TopicMemoryGlobalInput":
            proposals = []
            for project in ("PostgreSQL", "ClickHouse"):
                evidence = self.project_evidence(value.evidence, project)
                if not evidence:
                    continue
                historical = next((item for item in value.historical if item.title == f"{project} POC"), None)
                proposals.append(TopicMemoryProposal(
                    candidate_id=historical.candidate_id if historical else None,
                    content=TopicMemoryContent(
                        title=f"{project} POC", summary=self.source_text(evidence[-1]),
                        detail=self.source_text(evidence[-1]),
                    ),
                    evidence_ids=tuple(item.evidence_id for item in evidence),
                ))
            return GenerationResult(output=TopicMemoryGlobalOutput(proposals=tuple(proposals)))
        if stage == "TopicMemoryReconcileInput":
            return GenerationResult(output=TopicMemoryReconcileOutput(proposals=value.proposals))
        raise AssertionError(f"Unexpected fixture generation stage: {stage}")

    async def generate_profile(self, value):
        self.profile_windows += 1
        self.profile_requests.append(value)
        if value.previous_content:
            return None
        if any(PROFILE_SOURCE in source for source in value.sources):
            return PROFILE
        return None

    def stages(self):
        return TopicMemoryStageSet(
            probe=self, global_evolver=self, planner=self, evolver=self,
            temporary=self, reconciler=self, reducer=self,
            estimator=character_token_estimator(), input_tokens_limit=26000, max_requests=1,
        )


@pytest.fixture
def models(monkeypatch):
    decisions = BusinessDecisions()

    class ProfileGenerator:
        async def generate(self, value):
            return await decisions.generate_profile(value)

    def reject_atomic(_brain):
        pytest.fail("Default customer memory unexpectedly enabled Atomic extraction")

    monkeypatch.setattr(powercontext_brain, "build_topic_stages", lambda *_args, **_kwargs: decisions.stages())
    monkeypatch.setattr(powercontext_brain, "build_profile_generator", lambda _brain: ProfileGenerator())
    monkeypatch.setattr(powercontext_brain, "build_memory_pipeline", reject_atomic)
    return decisions


@pytest_asyncio.fixture
async def customer(tmp_path):
    state = StateManager(str(tmp_path / "harvey.db"))
    await state.init_db()
    contact = Prospect(first_name="Chen", email="chen-business@example.test")
    contact.id = await state.add_prospect(contact)
    return state, contact


async def saved_conversation(state, contact, *contents):
    conversation = Conversation(id="", prospect_id=contact.id, thread=[
        Message(sender="prospect", content=content) for content in contents
    ])
    conversation.id = await state.add_conversation(conversation)
    return conversation


@asynccontextmanager
async def provider(state):
    value = PowerContextProvider(PowerContextConfig(enabled=True, timeout=20), state, object())
    try:
        await value.start()
        yield value
    finally:
        await value.aclose()


async def published_topics(value, contact):
    scope = await value.resolve_scope(contact)
    # Inspect published identities through the SDK's public browse operation.
    return {item.title: item for item in await value._runtime.topic_memory.for_scope(scope).browse(limit=10)}


def assert_project_records(topics, postgres=POSTGRES):
    assert set(topics) == {"PostgreSQL POC", "ClickHouse POC"}
    assert topics["PostgreSQL POC"].summary == postgres
    assert topics["ClickHouse POC"].summary == CLICKHOUSE


def test_customer_memory_defaults_to_profile_and_topic_only():
    assert set(PowerContextConfig().memory_families) == {"profile", "topic"}


@pytest.mark.asyncio
async def test_two_projects_keep_distinct_requirements_and_return_grounded_evidence(customer, models):
    state, contact = customer
    postgres = await saved_conversation(state, contact, PROFILE_SOURCE, POSTGRES)
    clickhouse = await saved_conversation(state, contact, CLICKHOUSE)
    async with provider(state) as value:
        await value.capture_conversation(contact, postgres.id)
        await value.capture_conversation(contact, clickhouse.id)
        assert_project_records(await published_topics(value, contact))
        scope = await value.resolve_scope(contact)
        assert not (await value._runtime.atomic_memory.for_scope(scope).list()).items
        pg_context = await value.prepare(contact, "PostgreSQL")
        ch_context = await value.prepare(contact, "ClickHouse")
        assert POSTGRES in pg_context
        assert CLICKHOUSE in ch_context
        assert PROFILE in pg_context and PROFILE in ch_context
        # An assembly can contain several clearly attributed Topics. If another
        # project is returned, its complete distinct requirements must stay intact.
        if "200 GB" in pg_context:
            assert CLICKHOUSE in pg_context
        if "20 GB" in ch_context:
            assert POSTGRES in ch_context
        assert models.topic_windows > 0 and models.profile_windows > 0


@pytest.mark.asyncio
async def test_project_update_revises_same_topic_without_replacing_profile_or_other_project(customer, models):
    state, contact = customer
    postgres = await saved_conversation(state, contact, PROFILE_SOURCE, POSTGRES)
    clickhouse = await saved_conversation(state, contact, CLICKHOUSE)
    async with provider(state) as value:
        await value.capture_conversation(contact, postgres.id)
        await value.capture_conversation(contact, clickhouse.id)
        before = await published_topics(value, contact)
        postgres.thread.append(Message(sender="prospect", content=POSTGRES_UPDATED))
        await state.update_conversation(postgres.id, thread_json=postgres.thread_json())
        context = await value.prepare(contact, "PostgreSQL", conversation_id=postgres.id)
        after = await published_topics(value, contact)
        assert_project_records(after, postgres=POSTGRES_UPDATED)
        assert after["PostgreSQL POC"].artifact_ref.artifact_id == before["PostgreSQL POC"].artifact_ref.artifact_id
        assert after["PostgreSQL POC"].artifact_ref.revision > before["PostgreSQL POC"].artifact_ref.revision
        assert after["ClickHouse POC"].artifact_ref == before["ClickHouse POC"].artifact_ref
        assert POSTGRES_UPDATED in context and "20 GB" not in context
        assert PROFILE in context
        assert PROFILE in await value.prepare(contact, "ClickHouse")
        assert models.profile_requests[-1].previous_content.strip() == PROFILE


@pytest.mark.asyncio
async def test_contacts_do_not_share_project_constraints(customer, models):
    state, contact = customer
    other = Prospect(first_name="Li", email="li-business@example.test")
    other.id = await state.add_prospect(other)
    first_convo = await saved_conversation(state, contact, PROFILE_SOURCE, POSTGRES)
    other_convo = await saved_conversation(state, other, OTHER_POSTGRES)
    async with provider(state) as value:
        first_context = await value.prepare(contact, "PostgreSQL", conversation_id=first_convo.id)
        other_context = await value.prepare(other, "PostgreSQL", conversation_id=other_convo.id)
        assert POSTGRES in first_context and "75 GB" not in first_context
        assert OTHER_POSTGRES in other_context and "20 GB" not in other_context
        assert PROFILE not in other_context
        first = (await published_topics(value, contact))["PostgreSQL POC"]
        second = (await published_topics(value, other))["PostgreSQL POC"]
        assert first.artifact_ref.artifact_id != second.artifact_ref.artifact_id


@pytest.mark.asyncio
async def test_single_project_short_followup_survives_restart_without_reprocessing_sources(customer, models):
    state, contact = customer
    conversation = await saved_conversation(state, contact, PROFILE_SOURCE, POSTGRES,
                                            *("谢谢，我们还在内部讨论。" for _ in range(12)))
    async with provider(state) as value:
        await value.capture_conversation(contact, conversation.id)
        original_topic = (await published_topics(value, contact))["PostgreSQL POC"]
        calls = (models.topic_windows, models.profile_windows)
    async with provider(state) as reopened:
        context = await reopened.prepare(contact, SHORT_QUESTION, conversation_id=conversation.id)
        assert (await published_topics(reopened, contact))["PostgreSQL POC"].artifact_ref == original_topic.artifact_ref
        assert (models.topic_windows, models.profile_windows) == calls
        assert PROFILE in context
        assert POSTGRES in context
