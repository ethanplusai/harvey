"""SDK-supervised Topic processing with the embedding application's Brain.

Only the launcher is host-owned. PowerContext still selects durable work,
assigns fencing tokens, publishes revisions and acknowledges processing.
"""

from __future__ import annotations

import asyncio

from powercontext.builtin.artifacts.topic_memory import TOPIC_MEMORY_SOURCE_WINDOW_BINDING
from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.sqlite import SQLiteConfig, SQLiteProfile, SQLiteTopicMemoryFTSIndex
from powercontext.builtin.persistence.topic_memory import TopicMemoryRepository
from powercontext.builtin.persistence.topic_memory_index import CompositeTopicMemoryIndex
from powercontext.builtin.runtime.artifact_processing import ArtifactProcessingBinding
from powercontext.builtin.runtime.processing_contracts import (
    ArtifactProcessingBlockedError,
    ArtifactProcessingWorkAssignment,
    ArtifactProcessingWorkerCompletion,
)
from powercontext.builtin.runtime.topic_memory_processing import (
    TopicMemoryAtomicPublisher,
    TopicMemoryProcessor,
    TopicMemoryWindowSelector,
)
from powercontext.builtin.runtime.topic_memory_scope import TopicMemoryScopeProcessor, topic_memory_processing_block
from powercontext.builtin.sources import BUILTIN_SOURCE_REGISTRY


class TopicProcessingUnavailable(RuntimeError):
    """Topic generation did not durably finish; saved Sources remain retryable."""


class _TaskHandle:
    def __init__(self, task: asyncio.Task[ArtifactProcessingWorkerCompletion]):
        self.task = task

    async def wait(self) -> ArtifactProcessingWorkerCompletion:
        # The supervisor cancels this wait at its deadline, then calls terminate.
        # Keep ownership until that explicit cancellation/cleanup boundary.
        return await asyncio.shield(self.task)

    async def terminate(self) -> None:
        if not self.task.done() and not self.task.cancelling():
            self.task.cancel()
        try:
            await asyncio.shield(self.task)
        except asyncio.CancelledError:
            if not self.task.done():
                # Its finally blocks still own resources. A supervisor timeout
                # must retain the handle instead of pretending it has exited.
                raise
        except Exception:
            # wait() already reports failure; observing termination still succeeds.
            pass


class TopicProcessingAdapter:
    """Register a cancellable local worker and await committed Source progress.

    This is cooperative task isolation, not an OS process sandbox. The same
    Brain owns model calls, authentication and subprocess cleanup. Every task
    receives an assignment from the SDK supervisor and owns its database engine.
    """

    def __init__(self, database_config: SQLiteConfig, brain, *, timeout: float = 300):
        if not database_config.is_persistent:
            raise ValueError("Topic processing requires persistent SQLite storage")
        if timeout <= 0:
            raise ValueError("Topic processing timeout must be positive")
        self.database_config = database_config
        self.brain = brain
        self.timeout = timeout
        self.binding = ArtifactProcessingBinding(
            binding_name=TOPIC_MEMORY_SOURCE_WINDOW_BINDING,
            artifact_family="topic-memory",
            launcher=self,
            max_workers=1,
            worker_timeout_seconds=timeout,
            automatic_processing_interval=None,
            work_block=topic_memory_processing_block,
        )

    async def start(self, assignment: ArtifactProcessingWorkAssignment) -> _TaskHandle:
        # No await between creation and ownership handoff: cancellation cannot
        # leave a partially started task without a supervisor-owned handle.
        return _TaskHandle(asyncio.create_task(
            self._process(assignment), name=f"harvey-topic-worker-{assignment.worker_id}",
        ))

    async def _process(self, assignment: ArtifactProcessingWorkAssignment) -> ArtifactProcessingWorkerCompletion:
        from harvey.integrations.powercontext_brain import build_topic_stages

        try:
            stages = build_topic_stages(self.brain, context_window_tokens=32768, timeout=self.timeout)
            # The parent Runtime creates the schema before starting supervision.
            # Do not open another Runtime or initialize unrelated family indexes.
            async with SQLiteProfile.open(self.database_config, tables=()) as profile:
                database = profile.database
                sources = SourceRepository(BUILTIN_SOURCE_REGISTRY)
                topics = TopicMemoryRepository(index=CompositeTopicMemoryIndex(SQLiteTopicMemoryFTSIndex()))
                # The parent already initialized and validated the Topic index.
                # Repeating repository initialization here scans all Topic heads;
                # normal SDK search/publication still enforce retrieval shape.
                publisher = TopicMemoryAtomicPublisher(database, sources, topics)
                processor = TopicMemoryProcessor(
                    database=database, sources=sources, topics=topics, stages=stages,
                    publisher=publisher, embedding_model=None,
                )
                selector = TopicMemoryWindowSelector(
                    database, sources, stages.estimator,
                    context_window_tokens=32768, probe_fixed_prompt=stages.fixed_prompts.get("probe"),
                )
                return await TopicMemoryScopeProcessor(
                    database, processor, selector,
                ).process(assignment)
        except ArtifactProcessingBlockedError:
            # The SDK owns the durable, sanitized block classification.
            raise
        except Exception:
            # Database/provider exception values may include customer evidence.
            raise TopicProcessingUnavailable("Topic processing failed; saved Sources remain pending.") from None

    async def high_watermark(self, scope_id: str) -> int:
        """Snapshot the committed Source journal after capture, before one flush."""
        async with SQLiteProfile.open_readonly(self.database_config) as profile:
            async with profile.database.transaction() as connection:
                return await SourceRepository(BUILTIN_SOURCE_REGISTRY).journal_position(connection, scope_id)

    async def wait_until_processed(self, scope_id: str, target_sequence: int) -> None:
        """Wait for a fixed Source frontier; accepted flush alone is insufficient.

        A window with no useful Topic still advances its cursor and completes.
        Polling does not create new flush requests or alter processing generations.
        """
        if type(target_sequence) is not int or target_sequence < 0:
            raise ValueError("Topic processing target must be a nonnegative Source sequence")
        if target_sequence == 0:
            return
        async with asyncio.timeout(self.timeout), SQLiteProfile.open_readonly(self.database_config) as profile:
            cursors = SourceCursorRepository()
            while True:
                async with profile.database.transaction() as connection:
                    cursor = await cursors.load(connection, scope_id, TOPIC_MEMORY_SOURCE_WINDOW_BINDING)
                    if cursor is not None and cursor.cursor.sequence >= target_sequence:
                        return
                    block = await topic_memory_processing_block(connection, scope_id, TOPIC_MEMORY_SOURCE_WINDOW_BINDING)
                    if block is not None:
                        raise TopicProcessingUnavailable("Topic processing is blocked; saved Sources remain pending.")
                await asyncio.sleep(0.1)
