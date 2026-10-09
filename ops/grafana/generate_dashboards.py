"""Generate the ForgeFlow Grafana dashboards (spec section 67).

    python ops/grafana/generate_dashboards.py

Writes one JSON file per dashboard to ops/grafana/dashboards/ (docker compose) and
k8s/observability/dashboards/ (Kubernetes). Edit the panel lists below and re-run; do
not hand-edit the generated JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).with_name("dashboards")
K8S_OUT = Path(__file__).resolve().parents[2] / "k8s" / "observability" / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}


def panel(
    title: str,
    exprs: list[tuple[str, str]],
    kind: str = "timeseries",
    unit: str = "short",
    width: int = 12,
    description: str = "",
) -> dict:
    return {
        "type": kind,
        "title": title,
        "description": description,
        "datasource": DS,
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}}
        if kind == "timeseries"
        else {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value"},
        "targets": [
            {"refId": chr(65 + i), "datasource": DS, "expr": expr, "legendFormat": legend}
            for i, (expr, legend) in enumerate(exprs)
        ],
        "_width": width,
    }


def stat(title: str, expr: str, unit: str = "short", description: str = "") -> dict:
    return panel(title, [(expr, "")], "stat", unit, 6, description)


def dashboard(uid: str, title: str, panels: list[dict]) -> dict:
    x = y = 0
    row_height = 0
    laid = []
    for n, p in enumerate(panels, start=1):
        width = p.pop("_width")
        height = 4 if p["type"] == "stat" else 8
        if x + width > 24:
            x, y = 0, y + row_height
            row_height = 0
        p["id"] = n
        p["gridPos"] = {"x": x, "y": y, "w": width, "h": height}
        x += width
        row_height = max(row_height, height)
        laid.append(p)
    return {
        "uid": uid,
        "title": title,
        "tags": ["forgeflow"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "panels": laid,
    }


RATE = "[$__rate_interval]"
DASHBOARDS = {
    "platform": dashboard(
        "forgeflow-platform",
        "ForgeFlow / Platform",
        [
            stat("Workflows created (24h)", 'sum(increase(forgeflow_workflows_total{status="CREATED"}[24h]))'),
            stat("Completed (24h)", 'sum(increase(forgeflow_workflows_total{status="COMPLETED"}[24h]))'),
            stat("Failed (24h)", 'sum(increase(forgeflow_workflows_total{status="FAILED"}[24h]))'),
            stat(
                "Median workflow duration",
                'histogram_quantile(0.5, sum by (le) (rate(forgeflow_workflow_duration_seconds_bucket{status="COMPLETED"}[24h])))',
                "s",
            ),
            panel("Workflow status changes", [(f"sum by (status) (rate(forgeflow_workflow_transitions_total{RATE}))", "{{status}}")], unit="ops"),
            panel(
                "API latency p95 by route",
                [(f"histogram_quantile(0.95, sum by (le, route) (rate(forgeflow_api_latency_seconds_bucket{RATE})))", "{{route}}")],
                unit="s",
            ),
            panel("API requests by status", [(f"sum by (status) (rate(forgeflow_api_latency_seconds_count{RATE}))", "{{status}}")], unit="reqps"),
            panel("Approvals", [(f"sum by (status) (increase(forgeflow_approvals_total{RATE}))", "{{status}}")]),
        ],
    ),
    "agents": dashboard(
        "forgeflow-agents",
        "ForgeFlow / Agents",
        [
            panel("Agent runs by agent", [(f"sum by (agent) (rate(forgeflow_agent_runs_total{RATE}))", "{{agent}}")], unit="ops"),
            panel("Agent run failures", [(f'sum by (agent) (rate(forgeflow_agent_runs_total{{status="failed"}}{RATE}))', "{{agent}}")], unit="ops"),
            panel(
                "Agent duration p90",
                [(f"histogram_quantile(0.9, sum by (le, agent) (rate(forgeflow_agent_duration_seconds_bucket{RATE})))", "{{agent}}")],
                unit="s",
            ),
            panel("Native tool calls", [(f"topk(10, sum by (tool) (rate(forgeflow_tool_calls_total{RATE})))", "{{tool}}")], unit="ops"),
            panel(
                "MCP / connector calls",
                [(f"sum by (type, result, approval) (rate(forgeflow_capability_calls_total{RATE}))", "{{type}} {{result}} ({{approval}})")],
                unit="ops",
                width=24,
            ),
        ],
    ),
    "task-graph": dashboard(
        "forgeflow-task-graph",
        "ForgeFlow / Task Graph",
        [
            panel("Tasks finished by kind", [(f"sum by (kind) (rate(forgeflow_tasks_total{RATE}))", "{{kind}}")], unit="ops"),
            panel("Task failures by kind", [(f'sum by (kind) (rate(forgeflow_tasks_total{{status="failed"}}{RATE}))', "{{kind}}")], unit="ops"),
            panel(
                "Task duration p90 by kind",
                [(f"histogram_quantile(0.9, sum by (le, kind) (rate(forgeflow_task_duration_seconds_bucket{RATE})))", "{{kind}}")],
                unit="s",
            ),
            panel("Retries and repair rounds", [
                (f"sum by (kind) (increase(forgeflow_task_retries_total{RATE}))", "retry {{kind}}"),
                (f"sum(increase(forgeflow_repair_rounds_total{RATE}))", "repair rounds"),
            ]),
            panel("Worktrees on disk", [("forgeflow_worktree_count", "worktrees")], width=24),
        ],
    ),
    "ci-cd": dashboard(
        "forgeflow-ci-cd",
        "ForgeFlow / CI/CD",
        [
            stat("CI builds (24h)", "sum(increase(forgeflow_ci_runs_total[24h]))"),
            stat("CI failures (24h)", "sum(increase(forgeflow_ci_failures_total[24h]))"),
            stat("Deployment checks (24h)", "sum(increase(forgeflow_deployments_total[24h]))"),
            stat("Unhealthy deployments (24h)", "sum(increase(forgeflow_deployment_failures_total[24h]))"),
            panel("CI builds and failures", [
                (f"sum(rate(forgeflow_ci_runs_total{RATE}))", "builds"),
                (f"sum by (result) (rate(forgeflow_ci_failures_total{RATE}))", "{{result}}"),
            ], unit="ops"),
            panel(
                "CI build duration p90",
                [(f"histogram_quantile(0.9, sum by (le) (rate(forgeflow_ci_duration_seconds_bucket{RATE})))", "p90")],
                unit="s",
            ),
            panel("Deployment checks by application", [(f"sum by (application, status) (increase(forgeflow_deployments_total{RATE}))", "{{application}} {{status}}")], width=24),
        ],
    ),
    "infrastructure": dashboard(
        "forgeflow-infrastructure",
        "ForgeFlow / Infrastructure",
        [
            panel("Kafka consumer lag", [("sum by (group, topic) (forgeflow_kafka_consumer_lag)", "{{group}} {{topic}}")]),
            panel(
                "Redis latency p99",
                [(f"histogram_quantile(0.99, sum by (le) (rate(forgeflow_redis_latency_seconds_bucket{RATE})))", "p99")],
                unit="s",
            ),
            panel("Events relayed by topic", [(f"sum by (topic) (rate(forgeflow_events_published_total{RATE}))", "{{topic}}")], unit="ops"),
            panel("Search index documents", [("sum by (kind) (forgeflow_knowledge_documents)", "{{kind}}")]),
            panel("Scrape targets up", [("up", "{{job}} {{instance}}")], width=24),
        ],
    ),
    "failures": dashboard(
        "forgeflow-failures",
        "ForgeFlow / Failures",
        [
            panel("Test failures by check kind", [(f"sum by (kind) (rate(forgeflow_test_failures_total{RATE}))", "{{kind}}")], unit="ops"),
            panel("Test pass rate", [
                (f"1 - (sum(rate(forgeflow_test_failures_total{RATE})) / clamp_min(sum(rate(forgeflow_test_runs_total{RATE})), 1e-9))", "pass rate"),
            ], unit="percentunit"),
            panel("Failed tasks by kind", [(f'sum by (kind) (increase(forgeflow_tasks_total{{status="failed"}}{RATE}))', "{{kind}}")]),
            panel("Failed workflows", [(f'sum(increase(forgeflow_workflows_total{{status="FAILED"}}{RATE}))', "failed")]),
            panel("Denied or rejected capability calls", [
                (f'sum by (type, approval) (increase(forgeflow_capability_calls_total{{result="denied"}}{RATE}))', "{{type}} {{approval}}"),
            ], width=24),
        ],
    ),
    "autonomy": dashboard(
        "forgeflow-autonomy",
        "ForgeFlow / Autonomy (L4)",
        [
            stat("Runs resolved (24h)", 'sum(increase(forgeflow_l4_runs_total{decision="RESOLVE"}[24h]))'),
            stat(
                "Without human intervention (24h)",
                'sum(increase(forgeflow_l4_runs_total{human_intervention="no"}[24h]))',
            ),
            stat("Escalations (24h)", "sum(increase(forgeflow_l4_escalations_total[24h]))"),
            stat("Emergency stops (24h)", "sum(increase(forgeflow_l4_emergency_stops_total[24h]))"),
            panel("Triggers by path", [(f"sum by (trigger, result) (increase(forgeflow_l4_triggers_total{RATE}))", "{{trigger}} {{result}}")]),
            panel("Plans saved / rejected", [(f"sum by (result) (increase(forgeflow_l4_plans_total{RATE}))", "{{result}}")]),
            panel("Decisions", [(f"sum by (decision) (increase(forgeflow_l4_decisions_total{RATE}))", "{{decision}}")]),
            panel("Escalations by condition", [(f"sum by (condition) (increase(forgeflow_l4_escalations_total{RATE}))", "{{condition}}")]),
            panel("Independent review outcomes", [(f"sum by (result) (increase(forgeflow_l4_independent_reviews_total{RATE}))", "{{result}}")]),
            panel("Closure mismatches", [(f"sum by (check) (increase(forgeflow_l4_closure_mismatches_total{RATE}))", "{{check}}")]),
            panel("Circuit breaker trips", [(f"sum by (breaker) (increase(forgeflow_l4_circuit_breaker_trips_total{RATE}))", "{{breaker}}")]),
            panel("Human interventions", [(f"sum by (reason) (increase(forgeflow_l4_human_interventions_total{RATE}))", "{{reason}}")]),
            panel("Active runs by status", [("sum by (status) (forgeflow_l4_active_runs)", "{{status}}")]),
            panel("Parallel workers per run (p50)", [(f"histogram_quantile(0.5, sum by (le) (rate(forgeflow_l4_parallel_workers_bucket{RATE})))", "p50")]),
            panel("Stop latency p95", [(f"histogram_quantile(0.95, sum by (le) (rate(forgeflow_l4_stop_latency_seconds_bucket{RATE})))", "p95")], unit="s"),
            panel("Tokens and cost", [(f"sum(increase(forgeflow_l4_tokens_total{RATE}))", "tokens"), (f"sum(increase(forgeflow_l4_cost_usd_total{RATE}))", "USD")]),
            panel("Resumes", [(f"sum by (result) (increase(forgeflow_l4_resumes_total{RATE}))", "{{result}}")], width=24),
        ],
    ),
}


def main() -> None:
    for out in (OUT, K8S_OUT):
        out.mkdir(parents=True, exist_ok=True)
        for name, board in DASHBOARDS.items():
            (out / f"{name}.json").write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
            print(f"wrote {out / name}.json ({len(board['panels'])} panels)")


if __name__ == "__main__":
    main()
