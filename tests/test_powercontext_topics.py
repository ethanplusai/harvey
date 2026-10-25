"""Real SDK supervision and SQLite publication with deterministic Topic stages."""

import asyncio
import importlib
from contextlib import asynccontextmanager

import pytest

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
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, RuntimeConfig, open_builtin_runtime
from powercontext.builtin.runtime.topic_memory_processing import TopicMemoryStageSet
from powercontext.builtin.scope import ScopeDraft


class Decisions:
    def __init__(self):
        self.probe_calls = 0
        self.entered = asyncio.Event()
        self.release = None
        self.cleaned = asyncio.Event()
        self.failure = None

    async def generate(self, value):
        if type(value).__name__ == "TopicMemoryProbeInput":
            self.probe_calls += 1
            self.entered.set()
            try:
                if self.release is not None:
                    await self.release.wait()
                if self.failure:
                    raise self.failure
            finally:
                self.cleaned.set()
            relevant = tuple(item.evidence_id for item in value.evidence if "PostgreSQL" in item.content)
            return GenerationResult(output=TopicMemoryProbeOutput(probes=(
                TopicMemoryProbe(query="PostgreSQL", evidence_ids=relevant),
            ) if relevant else ()))
        if type(value).__name__ == "TopicMemoryGlobalInput":
            evidence = tuple(item for item in value.evidence if "PostgreSQL" in item.content)
            return GenerationResult(output=TopicMemoryGlobalOutput(proposals=(TopicMemoryProposal(
                candidate_id=value.historical[0].candidate_id if value.historical else None,
                content=TopicMemoryContent(
                    title="PostgreSQL POC", summary=evidence[-1].content,
                    detail=evidence[-1].content,
                ),
                evidence_ids=tuple(item.evidence_id for item in evidence),
            ),)))
        if type(value).__name__ == "TopicMemoryReconcileInput":
            return GenerationResult(output=TopicMemoryReconcileOutput(proposals=value.proposals))
        raise AssertionError(f"Unexpected stage {type(value).__name__}")

    def stages(self):
        return TopicMemoryStageSet(
            probe=self, global_evolver=self, planner=self, evolver=self,
            temporary=self, reconciler=self, reducer=self,
            estimator=character_token_estimator(), input_tokens_limit=26000,
            max_requests=1,
        )


@pytest.fixture
def decisions(monkeypatch):
    from harvey.integrations import powercontext_brain

    value = Decisions()
    monkeypatch.setattr(powercontext_brain, "build_topic_stages", lambda *_args, **_kw: value.stages(), raising=False)
    return value


@asynccontextmanager
async def runtime(tmp_path, brain=None):
    module = importlib.import_module("harvey.integrations.powercontext_topics")
    database = SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'topics.db'}")
    adapter = module.TopicProcessingAdapter(database, brain or object(), timeout=10)
    async with open_builtin_runtime(BuiltinConfig(
        database=database,
        runtime=RuntimeConfig(
            artifact_processing_families=("topic-memory",),
            schedule_seconds=None, experience_schedule_seconds=None,
        ),
    ), artifact_processing_bindings=(adapter.binding,)) as value:
        yield value, adapter


async def scope_for(value, key="contact"):
    return (await value.scopes.create(ScopeDraft(title=key, summary="Customer test scope", idempotency_key=key))).scope_id


async def capture_and_process(value, adapter, scope, event, content):
    receipt = await value.sources.for_scope(scope).capture(CaptureSource(source_id=event, content=content, metadata={}))
    target = await adapter.high_watermark(scope)
    assert target == receipt.sequence
    await value.topic_memory.for_scope(scope).flush()
    await adapter.wait_until_processed(scope, target)


@pytest.mark.asyncio
async def test_real_supervisor_publishes_revises_and_reopens_topics(tmp_path, decisions):
    async with runtime(tmp_path) as (value, adapter):
        scope = await scope_for(value)
        await capture_and_process(value, adapter, scope, "first", "PostgreSQL POC: 20 GB synthetic data in EU.")
        first = (await value.topic_memory.for_scope(scope).browse(limit=10))[0]
        await capture_and_process(value, adapter, scope, "update", "PostgreSQL POC: now 50 GB synthetic data in EU.")
        latest = (await value.topic_memory.for_scope(scope).browse(limit=10))[0]
        assert latest.artifact_ref.artifact_id == first.artifact_ref.artifact_id
        assert latest.artifact_ref.revision == first.artifact_ref.revision + 1
        assert "50 GB" in latest.summary
        calls = decisions.probe_calls
    async with runtime(tmp_path) as (value, adapter):
        assert await scope_for(value) == scope
        await adapter.wait_until_processed(scope, 2)
        assert (await value.topic_memory.for_scope(scope).browse(limit=10))[0] == latest
        assert decisions.probe_calls == calls


@pytest.mark.asyncio
async def test_no_topic_is_still_a_completed_source_window(tmp_path, decisions):
    async with runtime(tmp_path) as (value, adapter):
        scope = await scope_for(value)
        await capture_and_process(value, adapter, scope, "thanks", "Thank you.")
        assert await value.topic_memory.for_scope(scope).browse(limit=10) == ()


@pytest.mark.asyncio
async def test_accepted_flush_does_not_report_pending_generation_as_complete(tmp_path, decisions):
    decisions.release = asyncio.Event()
    async with runtime(tmp_path) as (value, adapter):
        scope = await scope_for(value)
        await value.sources.for_scope(scope).capture(CaptureSource(source_id="first", content="PostgreSQL POC: 20 GB.", metadata={}))
        assert (await value.topic_memory.for_scope(scope).flush()).status == "accepted"
        await asyncio.wait_for(decisions.entered.wait(), 3)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(adapter.wait_until_processed(scope, 1), 0.1)
        decisions.release.set()
        await adapter.wait_until_processed(scope, 1)


@pytest.mark.asyncio
async def test_shutdown_cancels_owned_model_task_and_restart_recovers_accepted_work(tmp_path, decisions):
    decisions.release = asyncio.Event()
    async with runtime(tmp_path) as (value, adapter):
        scope = await scope_for(value)
        await value.sources.for_scope(scope).capture(CaptureSource(source_id="first", content="PostgreSQL POC: 20 GB.", metadata={}))
        await value.topic_memory.for_scope(scope).flush()
        await asyncio.wait_for(decisions.entered.wait(), 3)
    assert decisions.cleaned.is_set(), "Runtime shutdown must await model cancellation cleanup"
    decisions.release = None
    async with runtime(tmp_path) as (value, adapter):
        # A persisted accepted request must recover without a second flush request.
        await adapter.wait_until_processed(scope, 1)
        assert "20 GB" in (await value.topic_memory.for_scope(scope).browse(limit=10))[0].summary


@pytest.mark.asyncio
async def test_distinct_contacts_do_not_share_topics(tmp_path, decisions):
    async with runtime(tmp_path) as (value, adapter):
        alice, bob = await scope_for(value, "alice"), await scope_for(value, "bob")
        await capture_and_process(value, adapter, alice, "same-id", "PostgreSQL POC: 20 GB for Alice.")
        await capture_and_process(value, adapter, bob, "same-id", "PostgreSQL POC: 50 GB for Bob.")
        alice_topic = (await value.topic_memory.for_scope(alice).browse(limit=10))[0]
        bob_topic = (await value.topic_memory.for_scope(bob).browse(limit=10))[0]
        assert "20 GB" in alice_topic.summary and "Bob" not in alice_topic.summary
        assert "50 GB" in bob_topic.summary and "Alice" not in bob_topic.summary


@pytest.mark.asyncio
async def test_failed_worker_keeps_sources_pending_and_sanitizes_error(tmp_path, decisions, caplog):
    decisions.failure = ValueError("private customer content and credential")
    async with runtime(tmp_path) as (value, adapter):
        scope = await scope_for(value)
        await value.sources.for_scope(scope).capture(CaptureSource(
            source_id="first", content="PostgreSQL POC: 20 GB.", metadata={},
        ))
        await value.topic_memory.for_scope(scope).flush()
        await asyncio.wait_for(decisions.entered.wait(), 3)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(adapter.wait_until_processed(scope, 1), 0.2)
        assert await value.topic_memory.for_scope(scope).browse(limit=10) == ()
    assert "private customer content" not in caplog.text
    decisions.failure = None
    async with runtime(tmp_path) as (value, adapter):
        await adapter.wait_until_processed(scope, 1)
        assert "20 GB" in (await value.topic_memory.for_scope(scope).browse(limit=10))[0].summary
