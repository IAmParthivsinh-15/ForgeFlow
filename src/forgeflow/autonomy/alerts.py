"""Alerts on named conditions (additional.md section 9).

Stored durably (`alerts` collection, shown in the UI and CLI), counted in Prometheus,
and optionally POSTed to a webhook (Slack-compatible JSON) from the environment.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from forgeflow.core.ids import new_id, utcnow
from forgeflow.core.logging import log_event
from forgeflow.core.redaction import redact
from forgeflow.extensibility.store import DocumentStore
from forgeflow.observability import metrics

logger = logging.getLogger(__name__)

KINDS = frozenset(
    {
        "escalation",
        "emergency_stop",
        "circuit_breaker",
        "run_stuck",
        "source_failure",
        "budget_exhausted",
        "blocking_finding",
        "closure_mismatch",
        "backlog",
    }
)


class Alerts:
    def __init__(self, store: DocumentStore, webhook_env: str = "FORGEFLOW_ALERT_WEBHOOK_URL"):
        self.store = store
        self.webhook_env = webhook_env

    async def raise_alert(
        self,
        kind: str,
        summary: str,
        trace_id: str | None = None,
        severity: str = "warning",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # One open alert per (kind, run): repeated detection does not spam.
        dedupe = f"{kind}:{trace_id or 'global'}"
        open_alerts = await self.store.find("alerts", {"dedupe": dedupe, "status": "open"}, limit=1)
        if open_alerts:
            return open_alerts[0]
        alert = {
            "alert_id": new_id("alr"),
            "dedupe": dedupe,
            "kind": kind,
            "severity": severity,
            "trace_id": trace_id,
            "summary": redact(summary)[:500],
            "details": details or {},
            "status": "open",
            "raised_at": utcnow(),
            "acknowledged_by": None,
        }
        await self.store.put("alerts", alert)
        metrics.L4_ALERTS.labels(kind).inc()
        log_event(
            logger, "alert", logging.WARNING, kind=kind, trace_id=trace_id, summary=alert["summary"]
        )
        url = os.environ.get(self.webhook_env, "")
        if url:
            try:
                async with httpx.AsyncClient(timeout=5) as http:
                    await http.post(
                        url, json={"text": f"[ForgeFlow {severity}] {kind}: {alert['summary']}"}
                    )
            except httpx.HTTPError:
                log_event(logger, "alert webhook failed", logging.WARNING, kind=kind)
        return alert

    async def acknowledge(self, alert_id: str, actor: str) -> bool:
        return await self.store.compare_and_set(
            "alerts",
            alert_id,
            {"status": "open"},
            {"status": "acknowledged", "acknowledged_by": actor, "acknowledged_at": utcnow()},
        )

    async def list_alerts(
        self, status: str | None = "open", limit: int = 100
    ) -> list[dict[str, Any]]:
        query = {"status": status} if status else {}
        return await self.store.find("alerts", query, sort="-raised_at", limit=limit)
