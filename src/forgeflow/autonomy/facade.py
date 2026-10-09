"""Wire the autonomy layer into a container and install its guards (additional.md 3-5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forgeflow.autonomy.alerts import Alerts
from forgeflow.autonomy.commander import Commander
from forgeflow.autonomy.control import RunControl
from forgeflow.autonomy.guard import Guardrails
from forgeflow.autonomy.runs import RunStore


@dataclass
class Autonomy:
    runs: RunStore
    guard: Guardrails
    alerts: Alerts
    commander: Commander
    control: RunControl


def build_autonomy(container: Any) -> Autonomy | None:
    ext = container.extensibility
    if ext is None:
        return None
    runs = RunStore(ext.store)
    guard = Guardrails(runs)
    alerts = Alerts(ext.store)
    commander = Commander(container, runs, alerts, container.settings)
    control = RunControl(container, runs, alerts, commander)
    # Enforcement outside the LLM: execution start, every dispatch, every external call.
    container.execution.start_guard = guard.start_guard
    container.execution.spawn_guard = guard.spawn_guard
    ext.gateway.run_guard = guard.gateway_guard
    return Autonomy(runs=runs, guard=guard, alerts=alerts, commander=commander, control=control)
