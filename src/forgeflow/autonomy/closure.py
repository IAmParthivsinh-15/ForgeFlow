"""Machine-checkable closure (additional.md section 2.7).

A successful tool call or an agent statement is not proof. Each check reads durable
records or re-reads GitHub; every check returns its evidence.
"""

from __future__ import annotations

from typing import Any

from forgeflow.integrations.github.client import GitHubError, marker
from forgeflow.schemas.autonomy import ClosureCheck, Run
from forgeflow.schemas.task import TaskStatus

IMPLEMENTERS = ("developer_subagent", "developer", "integrator")


def _last(tasks: list[Any], kind: str) -> Any:
    done = [t for t in tasks if t.kind == kind and t.status == TaskStatus.COMPLETED]
    return max(done, key=lambda t: t.round) if done else None


def _models(agent_runs: list[Any], agents: tuple[str, ...]) -> set[str]:
    return {
        f"{a.provider}/{a.model}"
        for r in agent_runs
        if r.agent_type in agents
        for a in r.attempts
        if a.status == "succeeded"
    }


async def verify_closure(
    run: Run,
    wf: Any,
    tasks: list[Any],
    agent_runs: list[Any],
    client: Any,
    events: int,
) -> list[ClosureCheck]:
    checks: list[ClosureCheck] = []
    item = run.source_item
    names = run.plan.closure_checks if run.plan else []

    if "change_exists_in_provider" in names:
        pr = wf.pull_request
        if pr is None or client is None:
            checks.append(
                ClosureCheck(
                    name="change_exists_in_provider",
                    passed=False,
                    detail="no pull request was opened",
                )
            )
        else:
            try:
                live = await client.get_pull_request(pr.repository, pr.number)
                target = wf.execution.target_commit if wf.execution else None
                ok = (
                    live.state == "open"
                    and live.head == f"forgeflow/{wf.workflow_id}"
                    and (live.head_sha is None or live.head_sha == target)
                    and live.draft
                )
                checks.append(
                    ClosureCheck(
                        name="change_exists_in_provider",
                        passed=ok,
                        detail=f"PR #{live.number} {live.state}, head {live.head}"
                        f"@{(live.head_sha or '?')[:10]}, draft={live.draft}",
                        evidence={
                            "url": live.url,
                            "head_sha": live.head_sha,
                            "expected_sha": target,
                        },
                    )
                )
            except GitHubError as exc:
                checks.append(
                    ClosureCheck(
                        name="change_exists_in_provider",
                        passed=False,
                        detail=f"re-reading the PR failed: {exc}",
                    )
                )

    if "required_ci_passes" in names:
        ci, qa = _last(tasks, "ci"), _last(tasks, "qa")
        ci_ok = bool(ci and ci.result and ci.result.verdict == "pass")
        qa_failed = bool(qa and qa.result and qa.result.blocking)
        tests = [c for t in tasks if t.result for c in t.result.checks if c.kind == "test"]
        last_test_ok = bool(tests) and tests[-1].passed
        checks.append(
            ClosureCheck(
                name="required_ci_passes",
                passed=ci_ok and not qa_failed and last_test_ok,
                detail=f"CI {'passed' if ci_ok else 'missing or failed'}; tests "
                f"{'passed' if last_test_ok else 'missing or failed'}; QA "
                f"{'blocking' if qa_failed else 'not blocking'}",
                evidence={
                    "ci_build": ci.result.ci.build.model_dump()
                    if ci and ci.result and ci.result.ci
                    else None
                },
            )
        )

    if "independent_review_passes" in names:
        review = _last(tasks, "review")
        reviewers = _models(agent_runs, ("code_review",))
        implementers = _models(agent_runs, IMPLEMENTERS)
        independent = bool(reviewers) and not (reviewers & implementers)
        planned = run.plan.reviewer_model if run.plan else None
        review_ok = bool(review and review.result and review.result.verdict == "pass")
        passed = review_ok and independent and planned in reviewers
        checks.append(
            ClosureCheck(
                name="independent_review_passes",
                passed=passed,
                detail=f"review {'passed' if review_ok else 'missing or blocking'}"
                f"; reviewer {sorted(reviewers)} vs implementer {sorted(implementers)}"
                + ("" if independent else " - NOT independent"),
                evidence={
                    "reviewer_models": sorted(reviewers),
                    "implementer_models": sorted(implementers),
                    "planned_reviewer": planned,
                },
            )
        )

    if "security_checks_pass" in names:
        sec = _last(tasks, "security")
        ok = bool(sec and sec.result and not sec.result.blocking)
        scanners = (
            [s.tool + ":" + s.status for s in sec.result.security.scanners]
            if (sec and sec.result and sec.result.security)
            else []
        )
        checks.append(
            ClosureCheck(
                name="security_checks_pass",
                passed=ok,
                detail="security stage passed" if ok else "security stage missing or blocking",
                evidence={"scanners": scanners},
            )
        )

    if "source_state_reverified" in names:
        if client is None:
            checks.append(
                ClosureCheck(
                    name="source_state_reverified",
                    passed=False,
                    detail="the source system cannot be read (no connector)",
                )
            )
        else:
            try:
                issue = await client.get_issue(item.repository, item.number)
                comments = await client.list_comments(item.repository, item.number)
                pr_url = wf.pull_request.url if wf.pull_request else None
                linked = any(
                    marker(run.trace_id, "resolution") in c and (pr_url or "") in c
                    for c in comments
                )
                ok = issue.state == "open" and linked
                checks.append(
                    ClosureCheck(
                        name="source_state_reverified",
                        passed=ok,
                        detail=f"issue #{issue.number} is {issue.state}; resolution comment "
                        f"{'links' if linked else 'does not link'} the PR",
                        evidence={"labels": issue.labels, "updated_at": issue.updated_at},
                    )
                )
            except GitHubError as exc:
                checks.append(
                    ClosureCheck(
                        name="source_state_reverified",
                        passed=False,
                        detail=f"re-reading the issue failed: {exc}",
                    )
                )

    if "evidence_stored" in names:
        ok = run.plan is not None and run.plan.status == "VALIDATED_AND_SAVED" and events > 0
        checks.append(
            ClosureCheck(
                name="evidence_stored",
                passed=ok,
                detail=f"plan saved, {events} evidence events under {run.trace_id}",
            )
        )
    return checks
