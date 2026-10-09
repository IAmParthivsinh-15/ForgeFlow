"""Prometheus metrics (spec section 67).

Most metrics are derived from the event stream at one point - the outbox relay in
the orchestrator worker - so they are counted once no matter how many agent workers
run: every state change already produces an event (spec section 2.4). Process-local
measurements (API latency, Redis latency, Kafka consumer lag, worktree count,
deployment checks) are recorded where they happen.

Names follow the spec, with Prometheus unit suffixes where the spec names a latency
(`forgeflow_api_latency_seconds`, `forgeflow_redis_latency_seconds`).

Scrape targets: platform-api:8000/metrics and orchestrator-worker:9101/metrics.
"""

from __future__ import annotations

import logging
from pathlib import Path

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from forgeflow.schemas.events import Event, EventType

logger = logging.getLogger(__name__)

_DURATION = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600)
_FAST = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}

WORKFLOWS = Counter(
    "forgeflow_workflows_total", "Workflows created and finished, by status", ["status"]
)
WORKFLOW_DURATION = Histogram(
    "forgeflow_workflow_duration_seconds",
    "Time from creation to a terminal status",
    ["status"],
    buckets=(*_DURATION, 7200, 14400),
)
WORKFLOW_TRANSITIONS = Counter(
    "forgeflow_workflow_transitions_total", "Workflow status changes", ["status"]
)
TASKS = Counter(
    "forgeflow_tasks_total", "Task attempts finished, by kind and status", ["kind", "status"]
)
TASK_DURATION = Histogram(
    "forgeflow_task_duration_seconds", "Task attempt run time", ["kind"], buckets=_DURATION
)
TASK_RETRIES = Counter("forgeflow_task_retries_total", "Tasks scheduled for retry", ["kind"])
AGENT_RUNS = Counter("forgeflow_agent_runs_total", "Agent runs", ["agent", "status"])
AGENT_DURATION = Histogram(
    "forgeflow_agent_duration_seconds", "Agent run time", ["agent"], buckets=_DURATION
)
TOOL_CALLS = Counter("forgeflow_tool_calls_total", "Native tool calls by agents", ["agent", "tool"])
CAPABILITY_CALLS = Counter(
    "forgeflow_capability_calls_total",
    "MCP tool and connector calls through the capability gateway",
    ["type", "result", "approval"],
)
TEST_RUNS = Counter("forgeflow_test_runs_total", "Repository checks executed", ["kind"])
TEST_FAILURES = Counter("forgeflow_test_failures_total", "Repository checks that failed", ["kind"])
CI_RUNS = Counter("forgeflow_ci_runs_total", "CI builds", ["provider"])
CI_FAILURES = Counter(
    "forgeflow_ci_failures_total", "CI builds not SUCCESS", ["provider", "result"]
)
CI_DURATION = Histogram(
    "forgeflow_ci_duration_seconds", "CI build duration", ["provider"], buckets=_DURATION
)
DEPLOYMENTS = Counter(
    "forgeflow_deployments_total", "Deployment verifications", ["application", "status"]
)
DEPLOYMENT_FAILURES = Counter(
    "forgeflow_deployment_failures_total", "Deployments found unhealthy", ["application"]
)
REPAIRS = Counter("forgeflow_repair_rounds_total", "Repair rounds requested")
APPROVALS = Counter("forgeflow_approvals_total", "Approval requests and decisions", ["status"])
EVENTS = Counter("forgeflow_events_published_total", "Events relayed to Kafka", ["topic"])
KAFKA_LAG = Gauge("forgeflow_kafka_consumer_lag", "Messages behind the log end", ["group", "topic"])
REDIS_LATENCY = Histogram("forgeflow_redis_latency_seconds", "Redis PING round trip", buckets=_FAST)
API_LATENCY = Histogram(
    "forgeflow_api_latency_seconds",
    "Platform API request latency",
    ["method", "route", "status"],
    buckets=_FAST,
)
WORKTREES = Gauge("forgeflow_worktree_count", "Task worktrees on disk")
KNOWLEDGE_DOCS = Gauge("forgeflow_knowledge_documents", "Documents in the search index", ["kind"])


def observe_event(event: Event) -> None:
    """Update metrics from one published event. Never raises."""
    try:
        _observe(event)
    except Exception:  # metrics must never break event relaying
        logger.debug("metric update failed", exc_info=True)


def _observe(event: Event) -> None:
    p = event.payload
    t = event.event_type
    EVENTS.labels(event.topic).inc()
    if t == EventType.WORKFLOW_CREATED:
        WORKFLOWS.labels("CREATED").inc()
    elif t == EventType.WORKFLOW_STATUS_CHANGED:
        current = str(p.get("current", ""))
        WORKFLOW_TRANSITIONS.labels(current).inc()
        if current in _TERMINAL:
            WORKFLOWS.labels(current).inc()
            if isinstance(p.get("age_seconds"), (int, float)):
                WORKFLOW_DURATION.labels(current).observe(float(p["age_seconds"]))
    elif t in (EventType.TASK_COMPLETED, EventType.TASK_FAILED):
        kind = str(p.get("kind", "unknown"))
        TASKS.labels(kind, "completed" if t == EventType.TASK_COMPLETED else "failed").inc()
        if isinstance(p.get("duration_ms"), int):
            TASK_DURATION.labels(kind).observe(p["duration_ms"] / 1000)
    elif t == EventType.TASK_RETRYING:
        TASK_RETRIES.labels(str(p.get("kind", "unknown"))).inc()
    elif t in (EventType.AGENT_RUN_COMPLETED, EventType.AGENT_RUN_FAILED):
        agent = str(p.get("agent_type", "unknown"))
        AGENT_RUNS.labels(
            agent, "completed" if t == EventType.AGENT_RUN_COMPLETED else "failed"
        ).inc()
        if isinstance(p.get("duration_ms"), int):
            AGENT_DURATION.labels(agent).observe(p["duration_ms"] / 1000)
        for tool in p.get("tools") or []:
            if tool:
                TOOL_CALLS.labels(agent, str(tool)).inc()
    elif t == EventType.CHECK_COMPLETED:
        kind = str(p.get("kind", "unknown"))
        TEST_RUNS.labels(kind).inc()
        if not p.get("passed"):
            TEST_FAILURES.labels(kind).inc()
    elif t == EventType.CI_BUILD_COMPLETED:
        provider = str(p.get("provider", "unknown"))
        CI_RUNS.labels(provider).inc()
        if p.get("result") != "SUCCESS":
            CI_FAILURES.labels(provider, str(p.get("result"))).inc()
        if isinstance(p.get("build_duration_ms"), int):
            CI_DURATION.labels(provider).observe(p["build_duration_ms"] / 1000)
    elif t == EventType.CAPABILITY_USED:
        CAPABILITY_CALLS.labels(
            str(p.get("type")), str(p.get("result")), str(p.get("approval"))
        ).inc()
    elif t == EventType.REPAIR_REQUESTED:
        REPAIRS.inc()
    elif t in (EventType.APPROVAL_REQUESTED, EventType.APPROVAL_RESOLVED):
        APPROVALS.labels(str(p.get("status", "pending"))).inc()


def count_worktrees(workspaces_root: Path) -> int:
    """Worktrees are workspaces/<workflow>/<task>; count the task folders."""
    if not workspaces_root.is_dir():
        return 0
    return sum(
        1
        for wf in workspaces_root.iterdir()
        if wf.is_dir() and wf.name.startswith("wf_")
        for task in wf.iterdir()
        if task.is_dir()
    )


def serve(port: int) -> None:
    if port:
        start_http_server(port)


# --- L4 autonomy (additional.md section 9) ----------------------------------------------
L4_TRIGGERS = Counter(
    "forgeflow_l4_triggers_total", "Work-item triggers by path and outcome", ["trigger", "result"]
)
L4_PLANS = Counter("forgeflow_l4_plans_total", "Commander plans by validation result", ["result"])
L4_RUNS = Counter(
    "forgeflow_l4_runs_total",
    "Runs reaching a terminal decision",
    ["decision", "human_intervention"],
)
L4_DECISIONS = Counter(
    "forgeflow_l4_decisions_total", "Decisions taken by the commander", ["decision"]
)
L4_ESCALATIONS = Counter(
    "forgeflow_l4_escalations_total", "Escalations by named condition", ["condition"]
)
L4_INTERVENTIONS = Counter(
    "forgeflow_l4_human_interventions_total", "Human interventions by reason", ["reason"]
)
L4_REVIEW = Counter(
    "forgeflow_l4_independent_reviews_total", "Independent review outcomes", ["result"]
)
L4_CLOSURE_MISMATCH = Counter(
    "forgeflow_l4_closure_mismatches_total", "Closure checks that failed", ["check"]
)
L4_BREAKER = Counter(
    "forgeflow_l4_circuit_breaker_trips_total", "Circuit breaker trips", ["breaker"]
)
L4_STOPS = Counter("forgeflow_l4_emergency_stops_total", "Emergency stops")
L4_STOP_LATENCY = Histogram(
    "forgeflow_l4_stop_latency_seconds", "Stop request to STOPPED", buckets=_FAST
)
L4_RESUMES = Counter("forgeflow_l4_resumes_total", "Resume attempts", ["result"])
L4_WORKERS = Histogram(
    "forgeflow_l4_parallel_workers",
    "Max parallel workers observed per run",
    buckets=(1, 2, 3, 4, 6, 8),
)
L4_TOKENS = Counter("forgeflow_l4_tokens_total", "Tokens used by autonomous runs")
L4_COST = Counter("forgeflow_l4_cost_usd_total", "Estimated cost of autonomous runs (USD)")
L4_DURATION = Histogram(
    "forgeflow_l4_run_duration_seconds", "Run duration to a terminal decision", buckets=_DURATION
)
L4_ACTIVE = Gauge("forgeflow_l4_active_runs", "Runs not yet terminal", ["status"])
L4_ALERTS = Counter("forgeflow_l4_alerts_total", "Alerts raised", ["kind"])
