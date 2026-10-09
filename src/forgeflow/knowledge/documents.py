"""Engineering history documents built from tasks and agent runs (spec section 36).

Every document has a deterministic id, so re-indexing the same task (an event
delivered twice, a retried attempt) overwrites rather than duplicates.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from forgeflow.core.redaction import redact
from forgeflow.schemas.task import Task, TaskStatus
from forgeflow.schemas.workflow import AgentRunRecord

_VOLATILE = re.compile(
    r"(0x[0-9a-f]+|\b[0-9a-f]{7,40}\b|\b\d+(\.\d+)?\b|'[^']*'|\"[^\"]*\"|wf_[a-z0-9]+)", re.I
)


def signature(text: str) -> str:
    """Stable fingerprint of a failure message: numbers, ids and quoted values removed."""
    normalized = _VOLATILE.sub("#", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()[:300]
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _base(task: Task, repository_id: str, commit: str | None) -> dict[str, Any]:
    return {
        "repository_id": repository_id,
        "workflow_id": task.workflow_id,
        "task_id": task.task_id,
        "stage": task.kind,
        "round": task.round,
        "commit": commit,
        "timestamp": (task.completed_at or task.updated_at).isoformat(),
    }


def _failure_context(task: Task) -> str:
    """Evidence that explains a blocking result (cause, failing criteria, test output)."""
    r = task.result
    if r is None:
        return ""
    parts: list[str] = []
    if r.ci and r.ci.analysis:
        a = r.ci.analysis
        parts += [a.summary, f"cause: {a.suspected_cause}", f"fix: {a.recommended_fix}"]
        parts += a.evidence[:8]
    if r.qa:
        parts += [f"{c.id}: {c.evidence}" for c in r.qa.criteria if c.status == "FAIL"]
    parts += [c.output[-1500:] for c in r.checks if not c.passed and c.kind != "setup"]
    return "\n".join(parts)


def task_documents(
    task: Task, repository_id: str, commit: str | None, runs: list[AgentRunRecord]
) -> dict[str, list[dict[str, Any]]]:
    docs: dict[str, list[dict[str, Any]]] = {
        k: [] for k in ("failures", "builds", "tests", "reviews", "security", "agent-events")
    }
    base = _base(task, repository_id, commit)
    r = task.result

    if task.status == TaskStatus.FAILED and task.error:
        docs["failures"].append(
            {
                **base,
                "id": f"{task.task_id}:error:{task.attempt}",
                "title": f"{task.key} failed: {task.error[:160]}",
                "text": redact(task.error),
                "signature": signature(task.error),
                "status": "open",
                "source": "task",
                "resolved": False,
                "resolution": "",
            }
        )

    if r is not None:
        context = redact(_failure_context(task))
        for i, reason in enumerate(r.blocking_reasons if r.blocking else []):
            docs["failures"].append(
                {
                    **base,
                    "id": f"{task.task_id}:{i}",
                    "title": redact(reason[:200]),
                    "text": redact(reason) + ("\n" + context if context else ""),
                    "signature": signature(reason),
                    "status": "open",
                    "source": "verification",
                    "resolved": False,
                    "resolution": "",
                }
            )
        for i, check in enumerate(r.checks):
            docs["tests"].append(
                {
                    **base,
                    "id": f"{task.task_id}:check:{i}",
                    "category": check.kind,
                    "status": "passed" if check.passed else "failed",
                    "title": check.command,
                    "text": redact(
                        check.output[-3000:] if not check.passed else check.output[-500:]
                    ),
                    "signature": signature(check.output[-400:]) if not check.passed else None,
                }
            )
        if r.ci:
            build = r.ci.build
            analysis = r.ci.analysis
            docs["builds"].append(
                {
                    **base,
                    "id": f"{task.task_id}:build",
                    "status": build.status,
                    "source": build.provider,
                    "title": f"{build.job} #{build.build_number} {build.status}",
                    "text": redact(
                        "\n".join(
                            [
                                *(
                                    [
                                        analysis.summary,
                                        analysis.suspected_cause,
                                        analysis.recommended_fix,
                                    ]
                                    if analysis
                                    else []
                                ),
                                ", ".join(f"{s.name}={s.status}" for s in build.stages),
                                build.log_tail[-4000:],
                            ]
                        )
                    ),
                    "category": analysis.failing_stage if analysis else None,
                }
            )
        if r.review:
            for i, f in enumerate(r.review.findings):
                docs["reviews"].append(
                    {
                        **base,
                        "id": f"{task.task_id}:review:{i}",
                        "severity": f.severity,
                        "category": f.category,
                        "path": f.file,
                        "title": redact(f.message[:200]),
                        "text": redact(f"{f.message}\n{f.suggestion or ''}"),
                        "start_line": f.line,
                    }
                )
        if r.security:
            for i, sf in enumerate(r.security.findings):
                docs["security"].append(
                    {
                        **base,
                        "id": f"{task.task_id}:security:{i}",
                        "severity": sf.severity,
                        "category": sf.category,
                        "path": sf.file,
                        "source": sf.source,
                        "title": redact(sf.impact[:200]),
                        "text": redact(f"{sf.evidence}\n{sf.impact}\n{sf.remediation}"),
                        "start_line": sf.line,
                    }
                )

    for run in runs:
        providers = ", ".join(f"{a.provider}:{a.model}:{a.status}" for a in run.attempts)
        docs["agent-events"].append(
            {
                **base,
                "id": run.run_id,
                "agent": run.agent_type,
                "status": run.status,
                "title": f"{run.agent_type} {run.status}",
                "text": redact(
                    f"{run.error or ''}\nproviders: {providers}\n"
                    f"tools: {', '.join(str(c.get('tool', '')) for c in run.tool_calls)}"
                ),
            }
        )
    return docs
