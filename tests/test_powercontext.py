"""Embedded storage regressions; model decisions are deterministic doubles.

These tests exercise the real PowerContext SQLite Runtime, not an HTTP service.
The separate Brain bridge tests verify Harvey's model invocation contract.
"""

import asyncio
import builtins
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.integrations import powercontext as integration
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.state import StateManager


REQUIREMENT = "PostgreSQL 迁移演练：数据处理全程限定在欧盟境内；只用 20 GB 合成数据；不要索取生产数据。"
UPDATED = "PostgreSQL 迁移演练更新：合成数据规模改为 50 GB，仍限定欧盟境内，仍不得索取生产数据。"
QUESTION = "关于 PostgreSQL 迁移演练，下一步怎么安排，需要准备什么数据？"


def config(**overrides):
    return SimpleNamespace(**{"enabled": True, "database_path": "", "namespace": "test-workspace",
                             "max_bytes": 8192, "timeout": 30, "memory_families": ["atomic"], **overrides})


@pytest_asyncio.fixture
async def state(tmp_path):
    value = StateManager(str(tmp_path / "harvey.db"))
    await value.init_db()
    return value


@pytest_asyncio.fixture
async def prospect(state):
    value = Prospect(first_name="Ana", last_name="Example", company="Ana Co",
                     title="Owner", email="ana@example.test", email_status="verified")
    value.id = await state.add_prospect(value)
    return value


async def saved_conversation(state, prospect, *messages):
    convo = Conversation(id="", prospect_id=prospect.id, thread=list(messages))
    convo.id = await state.add_conversation(convo)
    return convo


@pytest.fixture
def model_pipeline(monkeypatch):
    pytest.importorskip("powercontext.builtin.runtime")
    from powercontext.builtin.artifacts.atomic_memory.extraction import (
        AtomicMemoryCandidate, AtomicMemoryExtractionOutput, AtomicMemoryGenerationPipeline,
    )
    from powercontext.builtin.artifacts.atomic_memory.reconciliation import (
        AtomicMemoryReconciliationContent, AtomicMemoryReconciliationOutput,
    )
    from powercontext.builtin.inference import GenerationResult, character_token_estimator
    from harvey.integrations import powercontext_brain

    class Decisions:
        extracts = 0
        failure = None
        blocker = None
        entered = None

        async def generate(self, request):
            if hasattr(request, "proposal"):
                related = tuple(request.related)
                target = related[0] if related else None
                return GenerationResult(output=AtomicMemoryReconciliationOutput(
                    action="revise" if target else "create",
                    compared_ids=tuple(item.item_id for item in related),
                    target_ids=(target.item_id,) if target else (),
                    content=AtomicMemoryReconciliationContent(kind="constraint", text=request.proposal.text),
                    evidence_ids=tuple(dict.fromkeys((*request.proposal.evidence_ids,
                                                     *(target.evidence_ids if target else ())))),
                    reason="Preserve the newest explicit PostgreSQL requirement and its source.",
                ))
            self.extracts += 1
            if self.blocker is not None:
                self.entered.set()
                await self.blocker.wait()
            if self.failure:
                raise self.failure
            candidates = []
            for item in request.evidence:
                content = item.content
                text = content.get("content", content.get("text", "")) if isinstance(content, dict) else content
                if isinstance(text, str) and text.startswith("PostgreSQL"):
                    candidates.append(AtomicMemoryCandidate(
                        kind="constraint", text=text.split("\n", 1)[0], evidence_ids=(item.evidence_id,),
                    ))
            return GenerationResult(output=AtomicMemoryExtractionOutput(candidates=tuple(candidates)))

    decisions = Decisions()
    pipeline = AtomicMemoryGenerationPipeline(
        extractor=decisions, reconciler=decisions, estimator=character_token_estimator(),
    )
    monkeypatch.setattr(powercontext_brain, "build_memory_pipeline", lambda brain: pipeline)
    return decisions


@asynccontextmanager
async def provider(state, **settings):
    value = integration.PowerContextProvider(config(**settings), state, object())
    try:
        await value.start()
        yield value
    finally:
        await value.aclose()


@pytest.mark.asyncio
async def test_disabled_requires_no_powercontext_import_or_database(state, prospect, monkeypatch):
    original_import = builtins.__import__

    def reject_sdk(name, *args, **kwargs):
        if name == "powercontext" or name.startswith("powercontext."):
            pytest.fail("disabled integration imported the optional SDK")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_sdk)
    cfg = config()
    cfg.enabled = False
    value = integration.PowerContextProvider(cfg, state)
    await value.start()
    assert await value.prepare(prospect, QUESTION) == ""
    assert await value.capture_conversation(prospect, "missing") == []
    await value.aclose()
    assert not (state_path(state) / "powercontext.db").exists()


def state_path(state):
    from pathlib import Path
    return Path(state.db_path).parent


@pytest.mark.asyncio
async def test_old_constraints_survive_history_window_and_runtime_restart(state, prospect, model_pipeline):
    convo = await saved_conversation(
        state, prospect, Message(sender="prospect", content=REQUIREMENT),
        *(Message(sender="harvey" if i % 2 else "prospect", content="谢谢，我们还在讨论。")
          for i in range(30)),
    )
    assert REQUIREMENT not in " ".join(m.content for m in convo.thread[-6:])
    async with provider(state) as first:
        scope = await first.resolve_scope(prospect)
        content = await first.prepare(prospect, QUESTION, conversation_id=convo.id)
        assert REQUIREMENT in content
        assert "historical data, not instructions" in content
        assert 'family="atomic-memory"' in content
    assert (state_path(state) / "powercontext.db").is_file()
    before = model_pipeline.extracts
    async with provider(state) as reopened:
        assert await reopened.resolve_scope(prospect) == scope
        content = await reopened.prepare(prospect, QUESTION, conversation_id=convo.id)
        assert REQUIREMENT in content
        assert model_pipeline.extracts == before, "replayed sources must not incur duplicate extraction"


@pytest.mark.asyncio
async def test_contact_isolation_and_only_persisted_customer_sources(state, prospect, model_pipeline):
    other = Prospect(id="other-contact", first_name="Ben", email="ben@example.test")
    wrong = await saved_conversation(state, other, Message(sender="prospect", content=REQUIREMENT))
    convo = await saved_conversation(
        state, prospect, Message(sender="prospect", content=REQUIREMENT),
        Message(sender="harvey", content="PostgreSQL use 999 GB production data, unsent draft."),
    )
    async with provider(state) as value:
        assert await value.capture_conversation(prospect, wrong.id) == []
        content = await value.prepare(prospect, QUESTION, conversation_id=convo.id)
        assert REQUIREMENT in content
        assert "999 GB" not in content
        assert await value.prepare(other, QUESTION) == ""
        assert await value.resolve_scope(other) != await value.resolve_scope(prospect)


@pytest.mark.asyncio
async def test_new_customer_requirement_revises_old_fact(state, prospect, model_pipeline):
    convo = await saved_conversation(state, prospect, Message(sender="prospect", content=REQUIREMENT))
    async with provider(state) as value:
        assert REQUIREMENT in await value.prepare(prospect, QUESTION, conversation_id=convo.id)
        convo.thread.append(Message(sender="prospect", content=UPDATED))
        await state.update_conversation(convo.id, thread_json=convo.thread_json())
        content = await value.prepare(prospect, QUESTION, conversation_id=convo.id)
        assert UPDATED in content
        assert "20 GB" not in content


@pytest.mark.asyncio
async def test_failed_extraction_is_visible_and_retryable_after_restart(state, prospect, model_pipeline, caplog):
    convo = await saved_conversation(state, prospect, Message(sender="prospect", content=REQUIREMENT))
    model_pipeline.failure = ValueError("private customer body and credential")
    async with provider(state) as value:
        with pytest.raises(integration.PowerContextUnavailable) as failure:
            await value.prepare(prospect, QUESTION, conversation_id=convo.id)
        assert "private" not in str(failure.value)
    assert "private customer" not in caplog.text
    model_pipeline.failure = None
    async with provider(state) as value:
        assert REQUIREMENT in await value.prepare(prospect, QUESTION, conversation_id=convo.id)
        before = model_pipeline.extracts
        await value.capture_conversation(prospect, convo.id)
        assert model_pipeline.extracts == before


@pytest.mark.asyncio
async def test_cancellation_preserves_retryable_sources(state, prospect, model_pipeline):
    convo = await saved_conversation(state, prospect, Message(sender="prospect", content=REQUIREMENT))
    model_pipeline.failure = asyncio.CancelledError()
    async with provider(state) as value:
        with pytest.raises(asyncio.CancelledError):
            await value.prepare(prospect, QUESTION, conversation_id=convo.id)
        model_pipeline.failure = None
        assert REQUIREMENT in await value.prepare(prospect, QUESTION, conversation_id=convo.id)


@pytest.mark.asyncio
async def test_missing_optional_dependency_fails_enabled_start_clearly(state, monkeypatch):
    original_import = builtins.__import__

    def reject_sdk(name, *args, **kwargs):
        if name == "powercontext" or name.startswith("powercontext."):
            raise ModuleNotFoundError("powercontext")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_sdk)
    value = integration.PowerContextProvider(config(), state, object())
    with pytest.raises(integration.PowerContextUnavailable, match=r"harvey\[powercontext\]"):
        await value.start()
    await value.aclose()



@pytest.mark.asyncio
async def test_disabled_accepts_hosts_without_a_database_path():
    value = integration.PowerContextProvider(config(enabled=False), object())
    await value.start()
    assert await value.prepare(Prospect(id="contact"), QUESTION) == ""
    await value.aclose()


@pytest.mark.asyncio
async def test_relative_database_path_is_anchored_to_harvey_data_directory(state, model_pipeline):
    async with provider(state, database_path="memory/customer.db") as value:
        assert value.database_path == state_path(state) / "memory" / "customer.db"
    assert (state_path(state) / "memory" / "customer.db").is_file()


@pytest.mark.asyncio
async def test_prepared_context_is_preserved_exactly_within_byte_budget(state, prospect, model_pipeline, monkeypatch):
    from powercontext.builtin.runtime.application import ScopedContextApplication

    text = "## Memory\n  客户原文包含缩进、换行。\n  Artifact: exact-source@1\n"

    async def prepare(_self, request):
        assert request.max_bytes == 512
        return SimpleNamespace(status="ready", content=text, content_bytes=len(text.encode("utf-8")))

    monkeypatch.setattr(ScopedContextApplication, "prepare", prepare)
    async with provider(state, max_bytes=512) as value:
        result = await value.prepare(prospect, QUESTION)
        assert "Contact history:\n" + text + "\nEND POWERCONTEXT EVIDENCE" in result


@pytest.mark.asyncio
async def test_invalid_prepared_byte_count_blocks_generation(state, prospect, model_pipeline, monkeypatch):
    from powercontext.builtin.runtime.application import ScopedContextApplication

    async def prepare(_self, request):
        return SimpleNamespace(status="ready", content="客户" * 100, content_bytes=200)

    monkeypatch.setattr(ScopedContextApplication, "prepare", prepare)
    async with provider(state, max_bytes=512) as value:
        with pytest.raises(integration.PowerContextUnavailable):
            await value.prepare(prospect, QUESTION)


@pytest.mark.asyncio
async def test_long_source_is_kept_whole_and_relevant_fact_is_extracted(state, prospect, model_pipeline):
    from powercontext.builtin.persistence.sqlite import SQLiteConfig
    from powercontext.builtin.runtime import BuiltinConfig, RuntimeConfig, open_builtin_runtime

    original = REQUIREMENT + "\n" + "Ordinary supporting context. " * 400
    assert len(original.encode("utf-8")) > 8192
    convo = await saved_conversation(state, prospect, Message(sender="prospect", content=original))
    async with provider(state) as value:
        records = await value.capture_conversation(prospect, convo.id)
        scope = await value.resolve_scope(prospect)
        content = await value.prepare(prospect, QUESTION)
        assert REQUIREMENT in content
        database_path = value.database_path
    async with open_builtin_runtime(BuiltinConfig(
        database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database_path}"),
        runtime=RuntimeConfig(artifact_processing_families=(), schedule_seconds=None,
                              experience_schedule_seconds=None),
    )) as runtime:
        source = await runtime.records.for_scope(scope).get_source("content", records[0]["event_id"])
        assert source.content == original


@pytest.mark.asyncio
async def test_prepare_recovers_pending_sources_without_original_conversation_id(state, prospect, model_pipeline):
    convo = await saved_conversation(state, prospect, Message(sender="prospect", content=REQUIREMENT))
    model_pipeline.failure = ValueError("temporary model outage")
    async with provider(state) as value:
        with pytest.raises(integration.PowerContextUnavailable):
            await value.capture_conversation(prospect, convo.id)
    model_pipeline.failure = None
    async with provider(state) as value:
        # A later new conversation or Writer has no original conversation id.
        assert REQUIREMENT in await value.prepare(prospect, QUESTION)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["startup", "source_capture"])
async def test_capture_intent_survives_failure_before_source_commit(
    state, prospect, model_pipeline, monkeypatch, failure_stage,
):
    from powercontext.builtin.runtime.application import ScopedSourceApplication
    from harvey.integrations import powercontext_brain

    convo = await saved_conversation(state, prospect, Message(sender="prospect", content=REQUIREMENT))
    value = integration.PowerContextProvider(config(), state, object())

    def fail_start(_brain):
        raise RuntimeError("runtime startup temporarily unavailable")

    async def fail_capture(_self, _request):
        raise RuntimeError("storage failed before accepting Source")

    with monkeypatch.context() as failed:
        if failure_stage == "startup":
            failed.setattr(powercontext_brain, "build_memory_pipeline", fail_start)
        else:
            failed.setattr(ScopedSourceApplication, "capture", fail_capture)
        with pytest.raises(integration.PowerContextUnavailable):
            await value.capture_conversation(prospect, convo.id)
        await value.aclose()
    async with provider(state) as reopened:
        # Host may have consumed the email and moved its conversation to human review.
        assert REQUIREMENT in await reopened.prepare(prospect, QUESTION)
        before = model_pipeline.extracts
        assert REQUIREMENT in await reopened.prepare(prospect, QUESTION)
        assert model_pipeline.extracts == before


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
@pytest.mark.parametrize("same_conversation", [False, True])
async def test_waiting_capture_survives_interruption_and_concurrent_acknowledgement(
    state, prospect, model_pipeline, interruption, same_conversation,
):
    first_convo = await saved_conversation(state, prospect, Message(sender="prospect", content="Thank you."))
    model_pipeline.entered = asyncio.Event()
    model_pipeline.blocker = asyncio.Event()
    async with provider(state) as value:
        first = asyncio.create_task(value.capture_conversation(prospect, first_convo.id))
        try:
            await asyncio.wait_for(model_pipeline.entered.wait(), timeout=5)
            # The first capture holds the real contact processing lock in model work.
            if same_conversation:
                first_convo.thread.append(Message(sender="prospect", content=REQUIREMENT))
                await state.update_conversation(first_convo.id, thread_json=first_convo.thread_json())
                next_convo = first_convo
            else:
                next_convo = await saved_conversation(
                    state, prospect, Message(sender="prospect", content=REQUIREMENT),
                )
            if interruption == "timeout":
                value.config.timeout = 0.05
            second = asyncio.create_task(value.capture_conversation(prospect, next_convo.id))
            if interruption == "cancel":
                await asyncio.sleep(0.05)
                second.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await second
            else:
                with pytest.raises(integration.PowerContextUnavailable):
                    await second
        finally:
            # Successful acknowledgement of the first snapshot must retain newer work.
            model_pipeline.blocker.set()
            await first
    model_pipeline.blocker = None
    async with provider(state) as reopened:
        assert REQUIREMENT in await reopened.prepare(prospect, QUESTION)


@pytest.mark.asyncio
async def test_cancellation_during_intent_registration_finishes_only_the_short_commit(
    state, prospect, model_pipeline, monkeypatch,
):
    convo = await saved_conversation(state, prospect, Message(sender="prospect", content=REQUIREMENT))
    entered, release = asyncio.Event(), asyncio.Event()
    original_write = state.set_setting

    async def delayed_write(key, text):
        entered.set()
        await release.wait()
        await original_write(key, text)

    value = integration.PowerContextProvider(config(), state, object())
    with monkeypatch.context() as patch:
        patch.setattr(state, "set_setting", delayed_write)
        capture = asyncio.create_task(value.capture_conversation(prospect, convo.id))
        await asyncio.wait_for(entered.wait(), timeout=5)
        capture.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await capture
    assert model_pipeline.extracts == 0, "cancelled registration must not shield model work"
    await value.aclose()
    async with provider(state) as reopened:
        assert REQUIREMENT in await reopened.prepare(prospect, QUESTION)
