"""Orchestrator tools.

Intake needs no tools. Orchestration tools from spec section 8
(dispatch_task, get_task_status, request_review, ...) are registered here once
the Task Graph Engine exists; until then workflow control stays in
`forgeflow.platform.orchestration`, which is deterministic.
"""

from __future__ import annotations

from agents import Tool


def get_tools() -> list[Tool]:
    return []
