"""Triggers (additional.md section 2.1): GitHub webhook + scheduled sweep, one intake path.

Both triggers normalise the issue into a `SourceItem` and call `intake`, which creates
at most one run per source item (idempotency key `github:<repo>#<number>`). An explicit
re-run (after an escalation or emergency stop) gets a new generation suffix.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from forgeflow.autonomy.runs import RunStore, new_trace_id
from forgeflow.core.ids import new_id, utcnow
from forgeflow.integrations.github.client import Issue, issue_from_api
from forgeflow.observability import metrics
from forgeflow.schemas.autonomy import DecisionContract, Run, RunStatus, SourceItem

RISK_ORDER = ["low", "medium", "high", "critical"]
WEBHOOK_ACTIONS = frozenset({"opened", "reopened", "labeled", "edited"})


def source_item(issue: Issue) -> SourceItem:
    digest = hashlib.sha256(
        json.dumps([issue.title, issue.body, issue.labels], sort_keys=True).encode()
    ).hexdigest()
    return SourceItem(
        repository=issue.repository,
        item_id=f"issue_{issue.number}",
        number=issue.number,
        url=issue.url,
        title=issue.title[:500],
        body=issue.body,
        labels=issue.labels,
        state=issue.state,
        updated_at=issue.updated_at,
        content_hash=f"sha256:{digest[:32]}",
    )


def idempotency_key(item: SourceItem, generation: int = 0) -> str:
    key = f"github:{item.repository}#{item.number}"
    return key if generation == 0 else f"{key}:rerun:{generation}"


def eligibility(item: SourceItem, contract: DecisionContract) -> tuple[bool, str]:
    """Machine-checkable intake criteria. (eligible, reason)."""
    rules = contract.eligibility
    labels = {label.lower() for label in item.labels}
    if contract.source.repository and item.repository.lower() != contract.source.repository.lower():
        return False, f"{item.repository} is not the configured repository"
    if item.state not in rules.states:
        return False, f"the issue is {item.state}"
    missing = [label for label in rules.required_labels if label.lower() not in labels]
    if missing:
        return False, f"missing required label(s): {', '.join(missing)}"
    if rules.any_of_labels and not labels & {label.lower() for label in rules.any_of_labels}:
        return False, f"needs one of the labels: {', '.join(rules.any_of_labels)}"
    excluded = sorted(labels & {label.lower() for label in rules.excluded_labels})
    if excluded:
        return False, f"excluded by label(s): {', '.join(excluded)}"
    if len(item.body) > rules.max_body_chars:
        return False, f"the description exceeds {rules.max_body_chars} characters"
    if not (item.title.strip() or item.body.strip()):
        return False, "the issue has no title or description"
    return True, "eligible"


def risk_allowed(risk: str, contract: DecisionContract) -> bool:
    return RISK_ORDER.index(risk) <= RISK_ORDER.index(contract.eligibility.max_risk_level)


async def intake(
    runs: RunStore,
    contract: DecisionContract,
    item: SourceItem,
    trigger: Literal["webhook", "sweep", "manual"],
    generation: int = 0,
) -> tuple[Run, bool]:
    """Create the run for this source item, or return the existing one."""
    now = utcnow()
    run = Run(
        run_id=new_id("run"),
        trace_id=new_trace_id(),
        idempotency_key=idempotency_key(item, generation),
        trigger=trigger,
        source_item=item,
        status=RunStatus.RECEIVED,
        contract_version=contract.version,
        contract_hash=contract.hash,
        policy_version=contract.policy_version,
        action_profile_id=contract.action_profile.id,
        action_profile=contract.action_profile,
        mode=contract.mode,
        limits=contract.limits,
        circuit_breakers=contract.circuit_breakers,
        created_at=now,
        updated_at=now,
    )
    stored, created = await runs.create(run)
    metrics.L4_TRIGGERS.labels(trigger, "created" if created else "duplicate").inc()
    return stored, created


async def handle_webhook(
    runs: RunStore, contract: DecisionContract, event: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """GitHub `issues` webhook (signature already verified by the API)."""
    if event == "ping":
        return {"status": "pong"}
    if event not in contract.source.webhook_events:
        return {"status": "ignored", "reason": f"event {event} is not configured"}
    action = str(payload.get("action", ""))
    if action not in WEBHOOK_ACTIONS:
        return {"status": "ignored", "reason": f"action {action} is not a trigger"}
    repository = str((payload.get("repository") or {}).get("full_name", ""))
    issue = issue_from_api(repository, payload.get("issue") or {})
    if issue.is_pull_request:
        return {"status": "ignored", "reason": "pull requests are not work items"}
    item = source_item(issue)
    eligible, reason = eligibility(item, contract)
    if not eligible and action in ("edited", "labeled"):
        # Not (yet) eligible: wait for a later event or the sweep rather than closing it.
        return {"status": "ignored", "reason": reason}
    run, created = await intake(runs, contract, item, "webhook")
    return {"status": "created" if created else "duplicate", "trace_id": run.trace_id}


async def sweep(runs: RunStore, contract: DecisionContract, client: Any) -> dict[str, Any]:
    """Backup trigger: catch eligible issues the webhook path missed."""
    repository = contract.source.repository
    issues = await client.list_issues(repository, contract.eligibility.required_labels)
    created, duplicates, skipped = 0, 0, 0
    for issue in issues:
        item = source_item(issue)
        eligible, _ = eligibility(item, contract)
        if not eligible:
            skipped += 1
            continue
        _, is_new = await intake(runs, contract, item, "sweep")
        created += int(is_new)
        duplicates += int(not is_new)
    return {"seen": len(issues), "created": created, "duplicates": duplicates, "skipped": skipped}
