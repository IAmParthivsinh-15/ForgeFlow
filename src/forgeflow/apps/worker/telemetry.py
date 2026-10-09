"""Orchestrator-worker background loops for observability and knowledge (spec 36-37, 67).

- `gauges`: Kafka consumer lag for every ForgeFlow consumer group, Redis latency,
  worktree count and search-index sizes, refreshed periodically.
- `index_knowledge`: its own consumer group on the workflow and task topics. Indexes
  the repository when execution starts, each finished task's history, and the fixes
  that resolved failures once a workflow completes. Documents have deterministic ids,
  so redelivered events are harmless.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiokafka import AIOKafkaConsumer
from aiokafka.admin import AIOKafkaAdminClient

from forgeflow.apps.container import Container
from forgeflow.core.logging import bind_context, clear_context
from forgeflow.extensibility.projects import project_id_for
from forgeflow.knowledge.index import KINDS
from forgeflow.observability import metrics
from forgeflow.platform.events.kafka import deserialize
from forgeflow.schemas.events import Event, EventType, Topics

logger = logging.getLogger("forgeflow.telemetry")
INDEXER_GROUP = "knowledge-indexer"
CONSUMER_GROUPS = ("orchestrator-worker", "agent-worker", INDEXER_GROUP)
GAUGE_INTERVAL_SECONDS = 15


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def consumer_lag(
    admin: AIOKafkaAdminClient, probe: AIOKafkaConsumer
) -> dict[tuple[str, str], int]:
    """(group, topic) -> messages not yet consumed."""
    lag: dict[tuple[str, str], int] = {}
    for group in CONSUMER_GROUPS:
        offsets = await admin.list_consumer_group_offsets(group)
        if not offsets:
            continue
        ends = await probe.end_offsets(list(offsets))
        for tp, meta in offsets.items():
            committed = meta.offset if meta.offset >= 0 else 0
            key = (group, tp.topic)
            lag[key] = lag.get(key, 0) + max(0, ends.get(tp, committed) - committed)
    return lag


async def gauges(container: Container, stop: asyncio.Event) -> None:
    settings = container.settings
    admin = AIOKafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap_servers)
    probe = AIOKafkaConsumer(bootstrap_servers=settings.kafka_bootstrap_servers)
    await admin.start()
    await probe.start()
    try:
        while not stop.is_set():
            try:
                for (group, topic), value in (await consumer_lag(admin, probe)).items():
                    metrics.KAFKA_LAG.labels(group, topic).set(value)
                if container.redis is not None:
                    started = time.perf_counter()
                    await container.redis.ping()
                    metrics.REDIS_LATENCY.observe(time.perf_counter() - started)
                metrics.WORKTREES.set(
                    await asyncio.to_thread(metrics.count_worktrees, settings.workspaces_root)
                )
                knowledge = container.knowledge
                if knowledge is not None and knowledge.index is not None and knowledge.available():
                    for kind in KINDS:
                        metrics.KNOWLEDGE_DOCS.labels(kind).set(await knowledge.index.count(kind))
            except Exception:
                logger.debug("gauge refresh failed", exc_info=True)
            await _sleep(stop, GAUGE_INTERVAL_SECONDS)
    finally:
        await probe.stop()
        await admin.close()


async def handle_knowledge_event(container: Container, event: Event) -> None:
    knowledge = container.knowledge
    if knowledge is None or not knowledge.enabled:
        return
    if event.event_type not in (
        EventType.WORKFLOW_EXECUTION_STARTED,
        EventType.TASK_COMPLETED,
        EventType.TASK_FAILED,
        EventType.WORKFLOW_COMPLETED,
    ):
        return
    wf = await container.store.get_workflow(event.workflow_id)
    if not wf.repository_path or wf.execution is None:
        return
    repository_id = wf.project_id or project_id_for(wf.repository_path)
    if event.event_type == EventType.WORKFLOW_EXECUTION_STARTED:
        await knowledge.index_repository(
            repository_id,
            container.worktrees.repository(wf.repository_path),
            wf.execution.base_commit,
            wf.execution.base_ref,
        )
    elif event.event_type == EventType.WORKFLOW_COMPLETED:
        await knowledge.mark_resolved(
            wf.workflow_id, await container.store.list_tasks(wf.workflow_id)
        )
    elif event.task_id:
        task = await container.store.get_task(event.task_id)
        runs = [
            r
            for r in await container.store.list_agent_runs(wf.workflow_id)
            if r.task_id == task.task_id
        ]
        await knowledge.index_task(task, repository_id, wf.execution.target_commit, runs)


async def index_knowledge(container: Container, stop: asyncio.Event) -> None:
    if container.knowledge is None or not container.knowledge.enabled:
        return
    consumer = AIOKafkaConsumer(
        Topics.WORKFLOW,
        Topics.TASK,
        bootstrap_servers=container.settings.kafka_bootstrap_servers,
        group_id=INDEXER_GROUP,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_interval_ms=600_000,
    )
    await consumer.start()
    try:
        while not stop.is_set():
            batches = await consumer.getmany(timeout_ms=1000)
            for _, messages in batches.items():
                for message in messages:
                    event = deserialize(message.value)
                    bind_context(workflow_id=event.workflow_id)
                    try:
                        await handle_knowledge_event(container, event)
                    except Exception:
                        # Indexing is best-effort; it must never stall the partition.
                        logger.exception("knowledge indexing failed")
                    finally:
                        clear_context()
            if batches:
                await consumer.commit()
    finally:
        await consumer.stop()
