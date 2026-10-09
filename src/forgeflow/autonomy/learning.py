"""Learning loop (additional.md section 7) and the last-five-run review (section 10).

Learning records what a finished run teaches and proposes candidates (a regression
test, a policy rule, a closure-check fix). Candidates are only proposals: accepting one
records the human decision; nothing here edits the contract, permissions, prompts or
budgets. Those change only through a governed, versioned contract update.
"""

from __future__ import annotations

from typing import Any

from forgeflow.core.errors import NotFoundError, ValidationFailed
from forgeflow.core.ids import new_id, utcnow
from forgeflow.extensibility.store import DocumentStore
from forgeflow.schemas.autonomy import LearningRecord, Run


def build_learning(
    run: Run,
    tasks: list[Any],
    audit: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
) -> LearningRecord:
    corrections: list[str] = []
    failed_tests: list[str] = []
    root_causes: list[str] = []
    for t in tasks:
        r = t.result
        if r is None:
            if t.error:
                root_causes.append(f"{t.key}: {t.error[:200]}")
            continue
        if r.review:
            corrections += [
                f"{f.severity} {f.file or '-'}: {f.message[:160]}" for f in r.review.findings
            ]
        failed_tests += [
            f"{c.command} (exit {c.exit_code})"
            for c in r.checks
            if not c.passed and c.kind == "test"
        ]
        if r.blocking:
            root_causes += [f"{t.kind} round {t.round}: {x[:200]}" for x in r.blocking_reasons]
    denied = [
        f"{a['agent']}: {a['action'][:120]} ({a.get('error') or 'denied'})"
        for a in audit
        if a.get("result") == "denied"
    ]
    interventions = [
        {
            "kind": "approval",
            "capability": a["capability_id"],
            "decision": a["status"],
            "by": a.get("decided_by"),
            "at": str(a.get("decided_at")),
        }
        for a in approvals
        if a.get("decided_by") and a["status"] in ("approved", "rejected")
    ]
    if run.stop:
        interventions.append(
            {"kind": "emergency_stop", "by": run.stop.actor, "reason": run.stop.reason}
        )
    if run.resumes:
        interventions.append({"kind": "resume", "count": run.resumes})
    mismatches = [f"{c.name}: {c.detail}" for c in run.closure if not c.passed]

    candidates: list[dict[str, Any]] = []
    for test in sorted(set(failed_tests))[:5]:
        candidates.append(
            {
                "type": "regression_test",
                "proposal": f"Keep a regression test for: {test}",
                "status": "proposed",
            }
        )
    for finding in corrections[:5]:
        candidates.append(
            {
                "type": "review_rule",
                "proposal": f"Add a review/lint rule catching: {finding}",
                "status": "proposed",
            }
        )
    for violation in sorted(set(denied))[:5]:
        candidates.append(
            {
                "type": "policy_rule",
                "proposal": f"Decide whether the profile should allow or keep denying: {violation}",
                "status": "proposed",
            }
        )
    for mismatch in mismatches:
        candidates.append(
            {
                "type": "closure_check",
                "proposal": f"Investigate closure mismatch: {mismatch}",
                "status": "proposed",
            }
        )
    return LearningRecord(
        learning_id=new_id("lrn"),
        trace_id=run.trace_id,
        decision=run.decision or "none",
        outcome=str(run.status),
        review_corrections=corrections,
        failed_tests=failed_tests,
        retries=run.counters.retries,
        root_causes=root_causes,
        human_interventions=interventions,
        blocked_policy_violations=denied,
        closure_mismatches=mismatches,
        candidates=candidates,
        created_at=utcnow(),
    )


async def decide_candidate(
    store: DocumentStore, learning_id: str, index: int, decision: str, actor: str, note: str = ""
) -> dict[str, Any]:
    """Record a human decision on a candidate. Accepting does NOT apply anything."""
    if decision not in ("accepted", "rejected"):
        raise ValidationFailed("decision must be accepted or rejected")
    doc = await store.get("learning_records", learning_id)
    if doc is None:
        raise NotFoundError(f"learning record {learning_id} not found")
    if not 0 <= index < len(doc["candidates"]):
        raise ValidationFailed("unknown candidate")
    doc["candidates"][index].update(
        status=decision,
        decided_by=actor,
        decided_at=utcnow(),
        note=note,
        applied=False,  # changes go through a versioned contract/test/skill update
    )
    await store.put("learning_records", doc)
    return doc


def review_last_runs(runs: list[Run], learnings: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """For each run: did a human step in, and was it a named escalation or avoidable?"""
    rows = []
    for run in runs:
        learning = learnings.get(run.trace_id) or {}
        interventions = learning.get("human_interventions", [])
        classified = []
        for i in interventions:
            if i["kind"] == "emergency_stop":
                kind = "named: emergency_stop"
            elif i["kind"] == "approval" and run.action_profile.tier(i["capability"]) == "ask":
                kind = "named: action_requires_authority"
            elif i["kind"] == "approval":
                kind = "avoidable: the profile already authorises this action"
            else:
                kind = (
                    "operator: resume after a named stop"
                    if run.stop
                    else "avoidable: no named condition"
                )
            classified.append({**i, "classification": kind})
        if run.escalation:
            classified.append(
                {
                    "kind": "escalation",
                    "condition": run.escalation.condition,
                    "classification": f"named: {run.escalation.condition}",
                }
            )
        rows.append(
            {
                "trace_id": run.trace_id,
                "source": f"{run.source_item.repository}#{run.source_item.number}",
                "status": str(run.status),
                "decision": run.decision,
                "reason": run.decision_reason,
                "mode": run.mode,
                "interventions": classified,
                "autonomous_without_intervention": not classified and run.decision == "RESOLVE",
                "avoidable_dependencies": [
                    c for c in classified if c["classification"].startswith("avoidable")
                ],
                "closure_failed": [c.name for c in run.closure if not c.passed],
                "counters": run.counters.model_dump(),
            }
        )
    return rows
