"""Autonomy loops in the orchestrator worker (additional.md sections 2.1, 8).

- supervise: every AUTONOMY_TICK_SECONDS, advance every non-terminal run by one step.
  State is durable (MongoDB), so a restarted worker simply continues; a per-run Redis
  lock keeps scaled replicas from stepping the same run concurrently.
- sweep: the backup trigger, every `source.sweep_interval_seconds` of the contract.
No terminal session is involved in routine execution.
"""

from __future__ import annotations

import asyncio
import logging
import time

from forgeflow.apps.container import Container
from forgeflow.autonomy.intake import sweep
from forgeflow.core.errors import ForgeFlowError
from forgeflow.core.logging import clear_context, log_event
from forgeflow.observability import metrics
from forgeflow.platform.events.coordination import Coordinator
from forgeflow.schemas.autonomy import RunStatus

logger = logging.getLogger("forgeflow.autonomy")


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def supervise(container: Container, stop: asyncio.Event) -> None:
    autonomy = container.autonomy
    if autonomy is None or not container.settings.autonomy_enabled:
        return
    coordinator = Coordinator(container.redis) if container.redis is not None else None
    last_sweep = 0.0
    while not stop.is_set():
        try:
            active = await autonomy.runs.active()
            for status in RunStatus:
                metrics.L4_ACTIVE.labels(str(status)).set(
                    sum(1 for r in active if r.status == status)
                )
            for run in active:
                if coordinator is not None:
                    async with coordinator.workflow_lock(f"run:{run.run_id}"):
                        await autonomy.commander.step(run)
                else:
                    await autonomy.commander.step(run)
                clear_context()
            contract = autonomy.commander.contract()
            due = time.monotonic() - last_sweep >= contract.source.sweep_interval_seconds
            if contract.source.repository and due:
                last_sweep = time.monotonic()
                client = await autonomy.commander.github(contract)
                if client is not None:
                    result = await sweep(autonomy.runs, contract, client)
                    log_event(logger, "sweep completed", **result)
        except ForgeFlowError as exc:
            log_event(logger, "autonomy loop error", logging.WARNING, error=str(exc)[:300])
        except Exception:
            logger.exception("autonomy loop iteration failed")
        await _wait(stop, container.settings.autonomy_tick_seconds)
