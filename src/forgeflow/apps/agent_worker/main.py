"""Agent worker: executes dispatched tasks (spec sections 24, 47, 93).

Consumes `task.dispatched` from `forgeflow.task.events` and runs up to
AGENT_WORKER_CONCURRENCY tasks at once. Scale out by running more replicas
(`docker compose up --scale agent-worker=3`).

MongoDB, not Kafka, decides ownership: a task is executed only if this worker wins
the DISPATCHED -> RUNNING claim, so duplicate or replayed dispatch events are
harmless. Offsets are committed as soon as a task is handed to a slot; if the
worker dies mid-task, the orchestrator's reaper notices the missing heartbeat and
retries the task.

Run: python -m forgeflow.apps.agent_worker.main
"""

from __future__ import annotations

import asyncio
import logging
import socket

from aiokafka import AIOKafkaConsumer

from forgeflow.apps.container import build_container, load_environment
from forgeflow.apps.worker.main import install_signal_handlers
from forgeflow.core.config import get_settings
from forgeflow.core.ids import new_id
from forgeflow.core.logging import configure_logging, log_event
from forgeflow.platform.events.kafka import deserialize, ensure_topics
from forgeflow.platform.execution.executor import TaskExecutor
from forgeflow.schemas.events import EventType, Topics

logger = logging.getLogger("forgeflow.agent_worker")
CONSUMER_GROUP = "agent-worker"


class Slots:
    """Bounded concurrent execution, de-duplicated per task attempt."""

    def __init__(self, executor: TaskExecutor, size: int) -> None:
        self.executor = executor
        self.semaphore = asyncio.Semaphore(size)
        self.running: dict[tuple[str, int], asyncio.Task] = {}

    async def submit(self, task_id: str, attempt: int) -> None:
        key = (task_id, attempt)
        if key in self.running:
            return
        await self.semaphore.acquire()  # back-pressure: stop consuming when full
        self.running[key] = asyncio.create_task(self._run(key))

    async def _run(self, key: tuple[str, int]) -> None:
        try:
            await self.executor.execute(*key)
        except Exception:
            logger.exception("task execution crashed")
        finally:
            self.semaphore.release()
            self.running.pop(key, None)

    async def drain(self) -> None:
        if self.running:
            await asyncio.gather(*self.running.values(), return_exceptions=True)


async def main() -> None:
    load_environment()
    settings = get_settings()
    configure_logging("agent-worker", settings.log_level)
    await ensure_topics(settings.kafka_bootstrap_servers)
    container = await build_container(settings)
    worker_id = f"{socket.gethostname()}-{new_id('w')}"
    slots = Slots(container.executor(worker_id), settings.agent_worker_concurrency)
    consumer = AIOKafkaConsumer(
        Topics.TASK,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=CONSUMER_GROUP,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_interval_ms=1_800_000,
    )
    await consumer.start()
    stop = asyncio.Event()
    install_signal_handlers(stop)
    log_event(
        logger,
        "agent worker started",
        worker_id=worker_id,
        concurrency=settings.agent_worker_concurrency,
        fake_llm=settings.fake_llm,
    )
    try:
        while not stop.is_set():
            batches = await consumer.getmany(timeout_ms=1000)
            for _, messages in batches.items():
                for message in messages:
                    event = deserialize(message.value)
                    if event.event_type == EventType.TASK_DISPATCHED and event.task_id:
                        await slots.submit(event.task_id, int(event.payload.get("attempt", 0)))
            if batches:
                await consumer.commit()
    finally:
        await consumer.stop()
        await slots.drain()
        await container.close()


if __name__ == "__main__":
    asyncio.run(main())
