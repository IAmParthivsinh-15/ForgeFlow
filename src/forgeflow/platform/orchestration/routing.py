"""Conditional agent routing (spec sections 177-181).

Deterministic: only the capabilities the Requirement Specification marks as
required become stages. Review and security run after development (in
parallel with each other); QA follows them; CI runs last.
"""

from __future__ import annotations

from forgeflow.schemas.requirement import RequiredCapabilities
from forgeflow.schemas.workflow import Capability, RoutePlan, RouteStage

# Capabilities whose agents are implemented in this milestone. Planned stages for
# others are recorded so the plan is visible, but not executed yet.
IMPLEMENTED_CAPABILITIES: frozenset[Capability] = frozenset()

_ORDER: tuple[Capability, ...] = ("development", "code_review", "security", "qa", "ci")
_AGENT: dict[Capability, str] = {
    "development": "developer",
    "code_review": "code_review",
    "security": "security",
    "qa": "qa",
    "ci": "ci",
}
_UPSTREAM: dict[Capability, tuple[Capability, ...]] = {
    "development": (),
    "code_review": ("development",),
    "security": ("development",),
    "qa": ("code_review", "security", "development"),
    "ci": ("qa", "code_review", "security", "development"),
}
_REVIEW_LAYER: tuple[Capability, ...] = ("code_review", "security")
_REASON: dict[Capability, str] = {
    "development": "Code or configuration changes are required.",
    "code_review": "There is a change to review.",
    "security": "The work has a security-relevant surface.",
    "qa": "Acceptance criteria must be verified by tests.",
    "ci": "The CI pipeline must run.",
}


def plan_route(required: RequiredCapabilities) -> RoutePlan:
    selected = [c for c in _ORDER if getattr(required, c)]
    stage_id = {c: f"S{i + 1}-{c}" for i, c in enumerate(selected)}
    stages: list[RouteStage] = []
    for capability in selected:
        deps = _nearest_upstream(capability, set(selected))
        stages.append(
            RouteStage(
                stage_id=stage_id[capability],
                capability=capability,
                agent=_AGENT[capability],
                depends_on=[stage_id[d] for d in deps],
                implemented=capability in IMPLEMENTED_CAPABILITIES,
                reason=_REASON[capability],
            )
        )
    skipped = [c for c in _ORDER if c not in selected]
    if stages:
        rationale = "Route: " + " -> ".join(_describe_layers(stages))
    else:
        rationale = "No execution capabilities are required; the specification is the deliverable."
    return RoutePlan(stages=stages, skipped=skipped, rationale=rationale)


def _nearest_upstream(capability: Capability, selected: set[Capability]) -> list[Capability]:
    """Depend on the closest selected upstream layer only.

    QA depends on review+security if either is selected, otherwise on development.
    """
    upstream = [c for c in _UPSTREAM[capability] if c in selected]
    if not upstream or capability not in ("qa", "ci"):
        return upstream
    if capability == "ci" and "qa" in selected:
        return ["qa"]
    review_layer: list[Capability] = [c for c in _REVIEW_LAYER if c in selected]
    return review_layer or ["development"]


def _describe_layers(stages: list[RouteStage]) -> list[str]:
    layers: list[list[str]] = []
    placed: dict[str, int] = {}
    for stage in stages:
        level = max((placed[d] + 1 for d in stage.depends_on), default=0)
        placed[stage.stage_id] = level
        while len(layers) <= level:
            layers.append([])
        layers[level].append(stage.agent)
    return [" + ".join(layer) for layer in layers]
