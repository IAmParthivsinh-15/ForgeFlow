"""Orchestrator worker.

- Relays committed outbox events to Kafka.
- Consumes `forgeflow.workflow.events` and runs requirement analysis when
  `requirement.analysis_requested` arrives.

Delivery is at-least-once: offsets are committed after handling; duplicates are
absorbed by Redis de-duplication plus the service's own staleness check.

Run: python -m forgeflow.apps.worker.main
"""

from __future__ import annotations

import asyncio
import logging
import signal

from aiokafka import AIOKafkaConsumer

from forgeflow.apps.container import Container, build_container, load_environment
from forgeflow.core.config import get_settings
from forgeflow.core.logging import bind_context, clear_context, configure_logging, log_event
from forgeflow.platform.events.coordination import Coordinator
from forgeflow.platform.events.kafka import OutboxRelay, deserialize, ensure_topics
from forgeflow.schemas.events import Event, EventType, Topics

logger = logging.getLogger("forgeflow.worker")
CONSUMER_GROUP = "orchestrator-worker"


async def handle_event(container: Container, coordinator: Coordinator, event: Event) -> None:
    if event.event_type != EventType.ANALYSIS_REQUESTED:
        return
    if await coordinator.already_processed(CONSUMER_GROUP, event.event_id):
        log_event(logger, "duplicate event skipped", event_id=event.event_id)
        return
    bind_context(workflow_id=event.workflow_id)
    try:
        async with coordinator.workflow_lock(event.workflow_id):
            await container.service.process_analysis(
                event.workflow_id, int(event.payload["requirement_version"])
            )
        await coordinator.mark_processed(CONSUMER_GROUP, event.event_id)
    finally:
        clear_context()


async def consume(container: Container, stop: asyncio.Event) -> None:
    assert container.redis is not None
    coordinator = Coordinator(container.redis)
    consumer = AIOKafkaConsumer(
        Topics.WORKFLOW,
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
                        # The service records failures on the workflow itself; a crash here
                        # is unexpected and must not stall the partition.
                        logger.exception("event handling failed")
            if batches:
                await consumer.commit()
    finally:
        await consumer.stop()


async def main() -> None:
    load_environment()
    settings = get_settings()
    configure_logging("orchestrator-worker", settings.log_level)
    await ensure_topics(settings.kafka_bootstrap_servers)
    container = await build_container(settings)
    relay = OutboxRelay(
        container.store, settings.kafka_bootstrap_servers, settings.outbox_poll_interval_seconds
    )
    await relay.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loop
            signal.signal(sig, lambda *_: stop.set())

    log_event(logger, "worker started", fake_llm=settings.fake_llm)
    try:
        await asyncio.gather(relay.run(stop), consume(container, stop))
    finally:
        await relay.stop()
        await container.close()


if __name__ == "__main__":
    asyncio.run(main())
