"""Redis-backed short-lived coordination: locks and event de-duplication (spec section 34)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

DEDUP_TTL_SECONDS = 7 * 24 * 3600


class Coordinator:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    @asynccontextmanager
    async def lock(self, name: str, ttl: float = 1800) -> AsyncIterator[None]:
        lock = self.redis.lock(
            f"lock:{name}",
            timeout=ttl,
            blocking_timeout=ttl,
            raise_on_release_error=False,
        )
        if not await lock.acquire():
            raise TimeoutError(f"could not acquire lock {name}")
        try:
            yield
        finally:
            await lock.release()

    def workflow_lock(self, workflow_id: str, ttl: float = 1800):
        return self.lock(f"workflow:{workflow_id}", ttl)

    async def already_processed(self, consumer: str, event_id: str) -> bool:
        return bool(await self.redis.exists(f"dedup:{consumer}:{event_id}"))

    async def mark_processed(self, consumer: str, event_id: str) -> None:
        await self.redis.set(f"dedup:{consumer}:{event_id}", "1", ex=DEDUP_TTL_SECONDS)
