"""ForgeFlow operator CLI (additional.md sections 5, 8). Talks to the Platform API.

    forgeflow status                          mode, contract, queue, alerts
    forgeflow runs [--status S] [--watch]     queued / active / finished runs
    forgeflow run show <trace_id>             run, decision, counters, closure
    forgeflow run plan <trace_id>             the saved task_plan.json
    forgeflow run events <trace_id>           append-only evidence log
    forgeflow run stop <trace_id> --reason R  EMERGENCY STOP (idempotent)
    forgeflow run resume <trace_id>           resume after a stop / guardrail pause
    forgeflow run rerun <trace_id>            explicit new run for the same issue
    forgeflow intake <issue_number>           trigger a run manually (same path as webhook)
    forgeflow sweep                           run the backup sweep now
    forgeflow triage                          escalations, approvals, open alerts
    forgeflow monitor [--watch]               health, guardrails, metrics endpoints
    forgeflow review [--last 5]               last-N-run review

FORGEFLOW_API_URL selects the API (default http://localhost:8000). Routine autonomous
execution never needs this CLI or an open terminal; it is for operators.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

API = os.environ.get("FORGEFLOW_API_URL", "http://localhost:8000").rstrip("/")


def _call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    try:
        response = httpx.request(method, f"{API}{path}", json=body, timeout=30)
    except httpx.HTTPError as exc:
        sys.exit(f"cannot reach the ForgeFlow API at {API}: {type(exc).__name__}")
    if response.status_code >= 400:
        detail = response.json().get("detail") if response.content else response.reason_phrase
        sys.exit(f"error {response.status_code}: {detail}")
    return response.json() if response.content else None


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _row(run: dict[str, Any]) -> str:
    item = run["source_item"]
    c = run["counters"]
    return (
        f"{run['trace_id']:<24} {run['status']:<20} {(run.get('decision') or '-'):<16} "
        f"{item['repository']}#{item['number']:<6} workers={c['max_parallel_observed']} "
        f"tokens={c['tokens']} retries={c['retries']}  {item['title'][:50]}"
    )


def _watch(fn: Any, watch: bool, interval: float = 3) -> None:
    while True:
        if watch:
            print("\033[2J\033[H", end="")
        fn()
        if not watch:
            return
        time.sleep(interval)


def cmd_status(_: argparse.Namespace) -> None:
    _print(_call("GET", "/api/v1/autonomy/status"))


def cmd_runs(args: argparse.Namespace) -> None:
    def show() -> None:
        query = f"?status={args.status}" if args.status else ""
        runs = _call("GET", f"/api/v1/autonomy/runs{query}")
        print(f"{'TRACE':<24} {'STATUS':<20} {'DECISION':<16} SOURCE")
        for run in runs:
            print(_row(run))
        if not runs:
            print("(no runs)")

    _watch(show, args.watch)


def cmd_run(args: argparse.Namespace) -> None:
    trace = args.trace_id
    if args.action == "show":
        _print(_call("GET", f"/api/v1/autonomy/runs/{trace}"))
    elif args.action == "plan":
        _print(_call("GET", f"/api/v1/autonomy/runs/{trace}/plan"))
    elif args.action == "events":
        for e in _call("GET", f"/api/v1/autonomy/runs/{trace}/events"):
            print(
                f"{e['seq']:>4} {e['timestamp'][:19]} {e['actor']:<18} {e['type']:<26} "
                f"{json.dumps(e['data'], default=str)[:120]}"
            )
    elif args.action == "stop":
        if not args.reason:
            sys.exit("--reason is required for an emergency stop")
        run = _call("POST", f"/api/v1/autonomy/runs/{trace}/stop", {"reason": args.reason})
        stop = run.get("stop") or {}
        print(
            f"{trace}: {run['status']} (requested {stop.get('requested_at')}, "
            f"stopped {stop.get('stopped_at')}; "
            f"halted tasks: {len(stop.get('cancelled_tasks', []))})"
        )
    elif args.action == "resume":
        run = _call("POST", f"/api/v1/autonomy/runs/{trace}/resume")
        print(f"{trace}: {run['status']} (resumes: {run['resumes']})")
    elif args.action == "rerun":
        run = _call("POST", f"/api/v1/autonomy/runs/{trace}/rerun")
        print(
            f"new run {run['trace_id']} for "
            f"{run['source_item']['repository']}#{run['source_item']['number']}"
        )


def cmd_intake(args: argparse.Namespace) -> None:
    run = _call("POST", "/api/v1/autonomy/intake", {"number": args.number})
    print(f"{run['trace_id']}: {run['status']}")


def cmd_sweep(_: argparse.Namespace) -> None:
    _print(_call("POST", "/api/v1/autonomy/sweep"))


def cmd_triage(args: argparse.Namespace) -> None:
    def show() -> None:
        print("ESCALATED RUNS")
        for run in _call(
            "GET", "/api/v1/autonomy/runs?status=ESCALATED,PAUSED_BY_GUARDRAIL,STOPPED&limit=20"
        ):
            e = run.get("escalation") or {}
            print(
                f"  {run['trace_id']} [{e.get('condition', run['status'])}] "
                f"{str(e.get('summary', ''))[:90]}"
            )
            if e.get("decision_needed"):
                print(f"      decision needed: {e['decision_needed']}")
        print("PENDING APPROVALS")
        for a in _call("GET", "/api/v1/approvals?status=pending"):
            print(f"  {a['approval_id']} {a['risk']:<7} {a['summary'][:100]}")
        print("OPEN ALERTS")
        for a in _call("GET", "/api/v1/autonomy/alerts"):
            print(f"  {a['raised_at'][:19]} {a['severity']:<8} {a['kind']:<18} {a['summary'][:90]}")
        print('\nEmergency stop: forgeflow run stop <trace_id> --reason "..."')

    _watch(show, args.watch)


def cmd_monitor(args: argparse.Namespace) -> None:
    def show() -> None:
        try:
            health = httpx.get(f"{API}/health", timeout=10).json()
        except httpx.HTTPError as exc:
            health = {"error": type(exc).__name__}
        status = _call("GET", "/api/v1/autonomy/status")
        print(f"health: {health.get('status')} {health.get('checks')}")
        print(
            f"mode: {status['mode']}  contract: {status['contract_version']}  source ready: "
            f"{status['source_ready']}  open alerts: {status['open_alerts']}"
        )
        print(f"active runs: {status['active'] or 'none'}")
        g = status["guardrails"]
        print(f"guardrails: workers<={g['max_workers']} budgets={g['budgets']}")
        print(f"breakers: {g['circuit_breakers']}")
        print(f"metrics: {API}/metrics  (Grafana: http://localhost:3000)")

    _watch(show, args.watch, 5)


def cmd_review(args: argparse.Namespace) -> None:
    for row in _call("GET", f"/api/v1/autonomy/review?last={args.last}"):
        flag = "autonomous" if row["autonomous_without_intervention"] else "needed a human"
        print(f"{row['trace_id']} {row['status']:<16} {row['source']:<16} {flag}")
        for i in row["interventions"]:
            print(f"    - {i['kind']}: {i['classification']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="forgeflow", description="ForgeFlow operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p = sub.add_parser("runs")
    p.add_argument("--status")
    p.add_argument("--watch", action="store_true")
    p.set_defaults(func=cmd_runs)
    p = sub.add_parser("run")
    p.add_argument("action", choices=["show", "plan", "events", "stop", "resume", "rerun"])
    p.add_argument("trace_id")
    p.add_argument("--reason")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("intake")
    p.add_argument("number", type=int)
    p.set_defaults(func=cmd_intake)
    sub.add_parser("sweep").set_defaults(func=cmd_sweep)
    p = sub.add_parser("triage")
    p.add_argument("--watch", action="store_true")
    p.set_defaults(func=cmd_triage)
    p = sub.add_parser("monitor")
    p.add_argument("--watch", action="store_true")
    p.set_defaults(func=cmd_monitor)
    p = sub.add_parser("review")
    p.add_argument("--last", type=int, default=5)
    p.set_defaults(func=cmd_review)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
