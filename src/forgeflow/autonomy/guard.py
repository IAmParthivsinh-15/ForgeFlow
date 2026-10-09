"""System-enforced guardrails (additional.md sections 3-4). None of this is in a prompt.

- start guard   no saved, validated plan -> execution does not start
- spawn guard   before every dispatch: run state, worker cap, budgets
- gateway guard stopped/paused runs and observe mode cannot change external systems
- counters and circuit breakers, evaluated by the commander on every step
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from forgeflow.autonomy.runs import RunStore
from forgeflow.schemas.autonomy import NO_SPAWN, Counters, Run, RunStatus
from forgeflow.schemas.task import TaskStatus

BLOCKED_STATES = frozenset({RunStatus.STOPPING, RunStatus.STOPPED, RunStatus.PAUSED_BY_GUARDRAIL})


def _exceeded(run: Run) -> str | None:
    c, b = run.counters, run.limits.budgets
    if c.tokens > b.max_tokens:
        return f"token budget exhausted ({c.tokens} > {b.max_tokens})"
    if c.cost_usd > b.max_cost_usd:
        return f"cost budget exhausted ({c.cost_usd:.2f} > {b.max_cost_usd:.2f} USD)"
    if c.runtime_seconds > b.max_runtime_seconds:
        return f"runtime exceeded ({c.runtime_seconds:.0f}s > {b.max_runtime_seconds}s)"
    if c.retries > b.max_retries:
        return f"retry budget exhausted ({c.retries} > {b.max_retries})"
    return None


def tripped_breaker(run: Run) -> tuple[str, str] | None:
    """(breaker, detail) for the first threshold reached, else None."""
    c, cb = run.counters, run.circuit_breakers
    if (detail := _exceeded(run)) is not None:
        return "budget", detail
    if c.failed_tasks >= cb.max_failed_tasks:
        return (
            "worker_errors",
            f"{c.failed_tasks} failed task attempts (limit {cb.max_failed_tasks})",
        )
    if c.provider_failures >= cb.max_provider_failures:
        return "provider_outage", f"{c.provider_failures} model provider failures"
    if c.capability_errors >= cb.max_capability_errors:
        return "tool_errors", f"{c.capability_errors} tool/connector/MCP errors"
    return None


class Guardrails:
    def __init__(self, runs: RunStore) -> None:
        self.runs = runs

    async def _run_for(self, workflow_id: str | None) -> Run | None:
        return await self.runs.by_workflow(workflow_id) if workflow_id else None

    async def start_guard(self, wf: Any) -> str | None:
        if not getattr(wf, "autonomous", False):
            return None
        run = await self._run_for(wf.workflow_id)
        if run is None:
            return "autonomous workflow without a run"
        if run.plan is None or run.plan.status != "VALIDATED_AND_SAVED":
            return "no validated, saved plan (task_plan.json)"
        if run.status in NO_SPAWN:
            return f"run is {run.status}"
        return None

    async def spawn_guard(self, wf: Any, active: int) -> tuple[int, str | None]:
        """How many more tasks may start now (0 blocks dispatch), and why."""
        if not getattr(wf, "autonomous", False):
            return 1_000_000, None
        run = await self._run_for(wf.workflow_id)
        if run is None or run.plan is None or run.plan.status != "VALIDATED_AND_SAVED":
            return 0, "no validated, saved plan"
        if run.status in NO_SPAWN:
            return 0, f"run is {run.status}"
        if (detail := _exceeded(run)) is not None:
            return 0, detail
        cap = min(run.limits.max_workers, run.plan.worker_count_max)
        return max(0, cap - active), f"worker cap {cap}"

    async def gateway_guard(self, inv: Any, capability: Any) -> str | None:
        run = await self._run_for(getattr(inv, "workflow_id", None))
        if run is None:
            return None
        if run.status in BLOCKED_STATES:
            return f"run {run.trace_id} is {run.status}; external actions are blocked"
        external_write = "WRITE" in capability.permissions and "NETWORK" in capability.permissions
        if run.mode == "observe" and external_write:
            return "observe mode: the decision is recorded but external changes are not made"
        if capability.capability_id == "github.issue.comment":
            if run.counters.comments_posted >= run.action_profile.max_comments_per_run:
                return "comment rate limit for this run reached"
        return None


def measure(
    run: Run, tasks: list[Any], agent_runs: list[Any], audit: list[dict[str, Any]]
) -> Counters:
    """Recompute counters from durable records (never from model statements)."""
    counters = run.counters.model_copy()
    tokens = 0
    cost = 0.0
    provider_failures = 0
    unpriced: set[str] = set()
    prices = run.limits.model_prices_per_1k_tokens
    for agent_run in agent_runs:
        for attempt in agent_run.attempts:
            used = attempt.input_tokens + attempt.output_tokens
            tokens += used
            ref = f"{attempt.provider}/{attempt.model}"
            if attempt.status == "failed":
                provider_failures += 1
            if used and ref not in prices:
                unpriced.add(ref)
            cost += prices.get(ref, 0.0) * used / 1000
    counters.tokens = tokens
    counters.cost_usd = round(cost, 6)
    counters.provider_failures = provider_failures
    counters.unpriced_models = sorted(unpriced)
    counters.failed_tasks = sum(
        1
        for t in tasks
        if t.status == TaskStatus.FAILED and not (t.error or "").startswith("emergency stop")
    )
    counters.retries = sum(max(0, t.attempt - 1) for t in tasks) + sum(
        1 for t in tasks if t.kind == "repair"
    )
    counters.capability_errors = sum(1 for a in audit if a.get("result") == "error")
    counters.workers_spawned = sum(t.attempt for t in tasks)
    counters.runtime_seconds = (datetime.now(UTC) - run.created_at).total_seconds()
    counters.max_parallel_observed = max(counters.max_parallel_observed, _max_overlap(tasks))
    return counters


def _max_overlap(tasks: list[Any]) -> int:
    """Largest number of tasks whose [started, completed] windows overlapped."""
    points = []
    for t in tasks:
        if t.started_at and t.kind != "publish":
            points.append((t.started_at, 1))
            points.append((t.completed_at or t.updated_at, -1))
    best = current = 0
    for _, delta in sorted(points, key=lambda p: (p[0], p[1])):
        current += delta
        best = max(best, current)
    return best
