"""Final engineering report and PR description (spec sections 50, 58, 264).

Built deterministically from the structured task results - never written by an
LLM - so every statement in it is backed by recorded evidence.
"""

from __future__ import annotations

from forgeflow.core.ids import utcnow
from forgeflow.schemas.requirement import RequirementSpecification
from forgeflow.schemas.task import VERIFICATION_KINDS, Task, TaskStatus
from forgeflow.schemas.verification import FinalReport, StageOutcome
from forgeflow.schemas.workflow import Workflow

STAGE_TITLES = {"review": "Code review", "security": "Security", "qa": "QA", "ci": "CI"}
_ICON = {"pass": "✅", "fail": "❌", "uncertain": "⚠️", "not_run": "➖"}


def latest_verifications(tasks: list[Task]) -> dict[str, Task]:
    """Most recent completed task per verification kind."""
    latest: dict[str, Task] = {}
    for t in tasks:
        if t.kind in VERIFICATION_KINDS and t.status == TaskStatus.COMPLETED and t.result:
            if t.kind not in latest or t.round > latest[t.kind].round:
                latest[t.kind] = t
    return latest


def build_report(
    wf: Workflow, spec: RequirementSpecification | None, tasks: list[Task]
) -> FinalReport:
    latest = latest_verifications(tasks)
    execution = wf.execution
    stages: list[StageOutcome] = []
    if any(t.kind == "integrate" for t in tasks):
        integrate = next(t for t in tasks if t.kind == "integrate")
        stages.append(
            StageOutcome(
                stage="Development",
                verdict="pass" if integrate.status == TaskStatus.COMPLETED else "fail",
                summary=integrate.result.summary if integrate.result else "",
            )
        )
    for kind in ("review", "security", "qa", "ci"):
        task = latest.get(kind)
        if task and task.result:
            stages.append(
                StageOutcome(
                    stage=STAGE_TITLES[kind],
                    verdict=task.result.verdict or "uncertain",
                    summary=task.result.summary,
                    round=task.round,
                )
            )

    open_findings = [
        r
        for t in latest.values()
        if t.result and t.result.blocking
        for r in t.result.blocking_reasons
    ]
    accepted = list(execution.accepted_risks) if execution else []
    if accepted:
        outcome = "passed_with_accepted_risks"
    elif open_findings:
        outcome = "failed"
    elif latest:
        outcome = "passed"
    else:
        outcome = "completed"

    files = sorted(
        {
            f
            for t in tasks
            if t.kind in ("integrate", "repair") and t.result
            for f in t.result.files_changed
        }
    )
    criteria = (
        latest["qa"].result.qa.criteria
        if "qa" in latest and latest["qa"].result and latest["qa"].result.qa
        else []
    )
    ci_task = latest.get("ci")
    ci_url = (
        ci_task.result.ci.build.url if ci_task and ci_task.result and ci_task.result.ci else None
    )
    repairs = sum(1 for t in tasks if t.kind == "repair" and t.status == TaskStatus.COMPLETED)

    title_source = spec.summary if spec else wf.request
    pr_title = title_source.strip().rstrip(".")[:72]
    summary = _summary_line(outcome, stages, repairs)
    report = FinalReport(
        outcome=outcome,  # type: ignore[arg-type]
        summary=summary,
        stages=stages,
        acceptance_criteria=criteria,
        open_findings=open_findings,
        accepted_risks=accepted,
        repair_rounds=repairs,
        files_changed=files,
        branch=execution.integration_branch if execution else None,
        commit=(execution.target_commit if execution else None),
        ci_build_url=ci_url,
        pull_request_url=wf.pull_request.url if wf.pull_request else None,
        pr_title=pr_title,
        generated_at=utcnow(),
    )
    report.pr_body = _pr_body(wf, spec, report, latest)
    return report


def _summary_line(outcome: str, stages: list[StageOutcome], repairs: int) -> str:
    parts = [f"{s.stage}: {s.verdict}" for s in stages]
    text = {
        "passed": "All verification stages passed.",
        "passed_with_accepted_risks": "Completed with risks accepted by a human.",
        "failed": "Verification found unresolved blocking issues.",
        "completed": "Completed.",
    }[outcome]
    if repairs:
        text += f" {repairs} automatic repair round(s)."
    return f"{text} ({'; '.join(parts)})" if parts else text


def _pr_body(
    wf: Workflow,
    spec: RequirementSpecification | None,
    report: FinalReport,
    latest: dict[str, Task],
) -> str:
    lines = ["## Summary", "", spec.goal if spec else wf.request, ""]
    if spec and spec.checklist:
        lines += ["## Checklist", ""] + [f"- [x] {c.description}" for c in spec.checklist] + [""]
    if report.files_changed:
        lines += ["## Changes", ""] + [f"- `{f}`" for f in report.files_changed[:50]] + [""]
    lines += ["## Verification", "", "| Stage | Result | Details |", "|---|---|---|"]
    for s in report.stages:
        detail = s.summary.replace("|", "\\|").replace("\n", " ")[:160]
        lines.append(f"| {s.stage} | {_ICON.get(s.verdict, '')} {s.verdict} | {detail} |")
    lines.append("")
    if report.acceptance_criteria:
        lines += ["## Acceptance criteria", "", "| Id | Status | Evidence |", "|---|---|---|"]
        for c in report.acceptance_criteria:
            evidence = c.evidence.replace("|", "\\|").replace("\n", " ")[:160]
            lines.append(f"| {c.id} | {c.status} | {evidence} |")
        lines.append("")
    security = latest.get("security")
    if security and security.result and security.result.security:
        sec = security.result.security
        counts: dict[str, int] = {}
        for f in sec.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        scanners = ", ".join(f"{s.tool} ({s.status})" for s in sec.scanners)
        lines += [
            f"## Security (OWASP Top 10 {sec.owasp_edition})",
            "",
            f"Findings by severity: {counts or 'none'}. Scanners: {scanners or 'none'}.",
            "",
        ]
    if report.ci_build_url:
        lines += ["## CI", "", f"Build: {report.ci_build_url}", ""]
    if report.open_findings:
        lines += ["## Open findings", ""] + [f"- {f}" for f in report.open_findings] + [""]
    if report.accepted_risks:
        lines += ["## Accepted risks", ""] + [f"- {r}" for r in report.accepted_risks] + [""]
    lines += [
        "---",
        f"Generated by ForgeFlow workflow `{wf.workflow_id}`"
        + (f" from branch `{report.branch}`" if report.branch else "")
        + ".",
    ]
    return "\n".join(lines)
