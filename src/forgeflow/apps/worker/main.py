"""Orchestrator worker.

- Relays committed outbox events to Kafka.
- Consumes workflow events: runs requirement analysis; starts execution once a
  workflow is routed to development.
- Consumes task events: advances the task graph (scheduler tick) whenever a task
  changes state.
- Reaper: re-dispatches lost dispatches, fails tasks whose worker stopped
  heart-beating, and wakes up due retries.
- Telemetry: Prometheus metrics from relayed events (port METRICS_PORT), gauges,
  and knowledge indexing (telemetry.py).

Delivery is at-least-once: offsets are committed after handling; duplicates are
absorbed by Redis de-duplication plus idempotent state checks.

Run: python -m forgeflow.apps.worker.main
"""

from __future__ import annotations

import asyncio
import logging
import signal

from aiokafka import AIOKafkaConsumer

from forgeflow.apps.container import Container, build_container, load_environment
from forgeflow.apps.worker.autonomy_loop import supervise
from forgeflow.apps.worker.telemetry import gauges, index_knowledge
from forgeflow.core.config import get_settings
from forgeflow.core.logging import bind_context, clear_context, configure_logging, log_event
from forgeflow.observability import metrics
from forgeflow.platform.events.coordination import Coordinator
from forgeflow.platform.events.kafka import OutboxRelay, deserialize, ensure_topics
from forgeflow.schemas.events import Event, EventType, Topics

logger = logging.getLogger("forgeflow.worker")
CONSUMER_GROUP = "orchestrator-worker"

# Task events after which the scheduler must re-evaluate the graph.
TICK_ON = frozenset(
    {
        EventType.TASK_CREATED,
        EventType.TASK_COMPLETED,
        EventType.TASK_FAILED,
        EventType.TASK_CANCELLED,
        EventType.TASK_RETRYING,
        # The workflow shows WAITING_FOR_APPROVAL while a capability waits for a human.
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_RESOLVED,
    }
)


async def handle_event(container: Container, coordinator: Coordinator, event: Event) -> None:
    relevant = event.event_type in (EventType.ANALYSIS_REQUESTED, EventType.WORKFLOW_ROUTED) or (
        event.event_type in TICK_ON
    )
    if not relevant:
        return
    if await coordinator.already_processed(CONSUMER_GROUP, event.event_id):
        log_event(logger, "duplicate event skipped", event_id=event.event_id)
        return
    bind_context(workflow_id=event.workflow_id)
    try:
        async with coordinator.workflow_lock(event.workflow_id):
            if event.event_type == EventType.ANALYSIS_REQUESTED:
                await container.service.process_analysis(
                    event.workflow_id, int(event.payload["requirement_version"])
                )
            elif event.event_type == EventType.WORKFLOW_ROUTED:
                await container.execution.start_execution(event.workflow_id)
            else:
                await container.execution.tick(event.workflow_id)
        await coordinator.mark_processed(CONSUMER_GROUP, event.event_id)
    finally:
        clear_context()


async def consume(container: Container, stop: asyncio.Event) -> None:
    assert container.redis is not None
    coordinator = Coordinator(container.redis)
    consumer = AIOKafkaConsumer(
        Topics.WORKFLOW,
        Topics.TASK,
        bootstrap_servers=container.settings.kafka_bootstrap_servers,
        group_id=CONSUMER_GROUP,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        # Requirement analysis can take minutes; stay in the group meanwhile.
        max_poll_interval_ms=1_800_000,
    )
    await consumer.start()
    try:
        while not stop.is_set():
            batches = await consumer.getmany(timeout_ms=1000)
            for _, messages in batches.items():
                for message in messages:
                    try:
                        await handle_event(container, coordinator, deserialize(message.value))
                    except Exception:
                        # Failures are recorded on the workflow/task itself; a crash here
                        # is unexpected and must not stall the partition.
                        logger.exception("event handling failed")
            if batches:
                await consumer.commit()
    finally:
        await consumer.stop()


async def reap(container: Container, stop: asyncio.Event) -> None:
    assert container.redis is not None
    coordinator = Coordinator(container.redis)
    interval = container.settings.reaper_interval_seconds
    while not stop.is_set():
        try:
            for workflow_id in await container.execution.reap():
                async with coordinator.workflow_lock(workflow_id):
                    await container.execution.tick(workflow_id)
        except Exception:
            logger.exception("reaper iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


def install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loop
            signal.signal(sig, lambda *_: stop.set())


async def main() -> None:
    load_environment()
    settings = get_settings()
    configure_logging("orchestrator-worker", settings.log_level)
    await ensure_topics(settings.kafka_bootstrap_servers)
    container = await build_container(settings, "orchestrator-worker")
    metrics.serve(settings.metrics_port)
    relay = OutboxRelay(
        container.store,
        settings.kafka_bootstrap_servers,
        settings.outbox_poll_interval_seconds,
        observer=metrics.observe_event,
    )
    await relay.start()
    stop = asyncio.Event()
    install_signal_handlers(stop)
    log_event(logger, "worker started", fake_llm=settings.fake_llm)
    try:
        await asyncio.gather(
            relay.run(stop),
            consume(container, stop),
            reap(container, stop),
            gauges(container, stop),
            index_knowledge(container, stop),
            supervise(container, stop),
        )
    finally:
        await relay.stop()
        await container.close()


if __name__ == "__main__":
    asyncio.run(main())
