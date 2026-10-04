"""Kafka event bus and the outbox relay (spec sections 31-33, 136)."""

from __future__ import annotations

import asyncio
import json
import logging

from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from forgeflow.core.logging import log_event
from forgeflow.platform.state.store import WorkflowStore
from forgeflow.schemas.events import Event, Topics

logger = logging.getLogger(__name__)

ALL_TOPICS = (Topics.WORKFLOW, Topics.AGENT)


def serialize(event: Event) -> bytes:
    return event.model_dump_json().encode("utf-8")


def deserialize(raw: bytes) -> Event:
    return Event.model_validate(json.loads(raw))


async def ensure_topics(bootstrap_servers: str, partitions: int = 3) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap_servers)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        missing = [t for t in ALL_TOPICS if t not in existing]
        if missing:
            await admin.create_topics(
                [NewTopic(name=t, num_partitions=partitions, replication_factor=1) for t in missing]
            )
            log_event(logger, "kafka topics created", topics=missing)
    finally:
        await admin.close()


async def kafka_ping(bootstrap_servers: str) -> None:
    """Connectivity check; callers bound it with their own timeout."""
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap_servers, request_timeout_ms=3000)
    try:
        await admin.start()
        await admin.list_topics()
    finally:
        await admin.close()


class OutboxRelay:
    """Publishes committed outbox events to Kafka, keyed by workflow_id for ordering."""

    def __init__(self, store: WorkflowStore, bootstrap_servers: str, interval: float = 0.5) -> None:
        self.store = store
        self.interval = interval
        self.producer = AIOKafkaProducer(
            bootstrap_servers=bootstrap_servers, acks="all", enable_idempotence=True
        )

    async def start(self) -> None:
        await self.producer.start()

    async def stop(self) -> None:
        await self.producer.stop()

    async def relay_once(self) -> int:
        events = await self.store.fetch_unpublished_events()
        published: list[str] = []
        for event in events:
            await self.producer.send_and_wait(
                event.topic, value=serialize(event), key=event.workflow_id.encode("utf-8")
            )
            published.append(event.event_id)
        await self.store.mark_events_published(published)
        return len(published)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                count = await self.relay_once()
            except Exception:
                logger.exception("outbox relay iteration failed")
                count = 0
            if count == 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval)
                except TimeoutError:
                    pass
