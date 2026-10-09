"""Commander planning (additional.md section 3): build, validate, save `task_plan.json`.

The commander writes the plan from the finalised specification; validation is code,
not prompt: worker caps, the model allow-list, an independent reviewer model, the
action-profile tiers, budgets and closure checks all come from the run's immutable
snapshot. A plan cannot grant itself authority.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from forgeflow.core.ids import utcnow
from forgeflow.schemas.autonomy import DecisionContract, PlannedModel, PlanSubtask, Run, TaskPlan

# Actions every code-change run plans to take (all must be AUTO in the profile).
CODE_CHANGE_ACTIONS = [
    "github.issue.read",
    "workspace.write",
    "checks.run",
    "github.branch.push",
    "github.pull_request.create_draft",
    "github.issue.comment",
]
TOKENS_PER_AGENT_STEP = 8_000


def role_models(fake_llm: bool, registry: Any) -> dict[str, PlannedModel]:
    """First-choice model per role. Fallbacks may differ at run time; closure checks the
    models that actually ran."""
    if fake_llm or registry is None:
        from forgeflow.platform.orchestration.fake_gateway import FAKE_MODELS

        return {role: PlannedModel(provider="fake", model=m) for role, m in FAKE_MODELS.items()}

    def first(profile: str) -> PlannedModel:
        chain = registry.chain(profile)
        if not chain:
            return PlannedModel(provider="unconfigured", model=profile)
        return PlannedModel(provider=chain[0].provider, model=chain[0].model)

    return {"implementer": first("coding"), "reviewer": first("review"), "analyst": first("fast")}


def build_plan(
    run: Run,
    workflow_id: str,
    spec: Any,
    contract: DecisionContract,
    models: dict[str, PlannedModel],
    max_parallel_tasks: int,
    workers: int | None = None,
) -> TaskPlan:
    implementer, reviewer, analyst = models["implementer"], models["reviewer"], models["analyst"]
    requested = workers or min(
        run.limits.max_workers, max_parallel_tasks, max(contract.plan.min_parallel_workers, 2)
    )
    criteria = [ac.id for ac in spec.acceptance_criteria] if spec else []
    subtasks = [
        PlanSubtask(
            task_id="prepare",
            purpose="Intake, readiness and scope analysis -> requirement specification",
            role="prepare",
            worker_role="requirement_analyzer",
            model=analyst.ref,
            expected_output="requirement_specification.json",
        ),
        PlanSubtask(
            task_id="decompose",
            purpose="Split the fix into independent subtasks with disjoint file scopes",
            role="do",
            worker_role="developer",
            model=implementer.ref,
            expected_output="development_plan.json",
            dependencies=["prepare"],
        ),
        PlanSubtask(
            task_id="implement",
            purpose=f"Implement subtasks in parallel worktrees (up to {requested} workers)",
            role="do",
            worker_role="developer_subagent",
            model=implementer.ref,
            expected_output="commits on forgeflow/* branches",
            dependencies=["decompose"],
            parallel_group="implement",
        ),
        PlanSubtask(
            task_id="integrate",
            purpose="Merge subtask branches and run post-merge checks",
            role="do",
            worker_role="integrator",
            model=implementer.ref,
            expected_output="integration commit",
            dependencies=["implement"],
        ),
        PlanSubtask(
            task_id="review",
            purpose="Independent code review with a different model than the implementer",
            role="review",
            worker_role="code_review",
            model=reviewer.ref,
            expected_output="review_report.json",
            dependencies=["integrate"],
            parallel_group="verify",
        ),
        PlanSubtask(
            task_id="security",
            purpose="OWASP assessment plus scanners (gitleaks, bandit, semgrep)",
            role="review",
            worker_role="security",
            model=reviewer.ref,
            expected_output="security_report.json",
            dependencies=["integrate"],
            parallel_group="verify",
        ),
        PlanSubtask(
            task_id="qa",
            purpose=f"Verify acceptance criteria {', '.join(criteria) or '(none)'} with evidence",
            role="verify",
            worker_role="qa",
            model=analyst.ref,
            expected_output="qa_report.json",
            dependencies=["integrate"],
            parallel_group="verify",
        ),
        PlanSubtask(
            task_id="ci",
            purpose="Run the repository pipeline in Jenkins",
            role="verify",
            worker_role="ci",
            model="none",
            expected_output="ci_report.json",
            dependencies=["review", "security", "qa"],
        ),
        PlanSubtask(
            task_id="publish",
            purpose="Push forgeflow/<workflow> and open a draft pull request",
            role="do",
            worker_role="github",
            model="none",
            expected_output="draft pull request",
            dependencies=["ci"],
        ),
        PlanSubtask(
            task_id="closure",
            purpose="Re-read GitHub and verify every closure check",
            role="verify",
            worker_role="commander",
            model="none",
            expected_output="closure_checks.json",
            dependencies=["publish"],
        ),
        PlanSubtask(
            task_id="learn",
            purpose="Record decision, corrections, failures and candidate improvements",
            role="learn",
            worker_role="commander",
            model="none",
            expected_output="learning_record.json",
            dependencies=["closure"],
        ),
    ]
    price = run.limits.model_prices_per_1k_tokens
    estimate = sum(
        price.get(m.ref, 0.0) * TOKENS_PER_AGENT_STEP / 1000
        for m in (analyst, implementer, implementer, implementer, reviewer, reviewer, analyst)
    )
    plan = TaskPlan(
        trace_id=run.trace_id,
        workflow_id=workflow_id,
        objective=f"Resolve {run.source_item.repository}#{run.source_item.number} "
        f"({run.source_item.title[:120]}) and verify closure on GitHub",
        source_item={
            "system": run.source_item.system,
            "item_id": run.source_item.item_id,
            "url": run.source_item.url,
            "content_hash": run.source_item.content_hash,
        },
        subtasks=subtasks,
        worker_count_requested=requested,
        worker_count_max=run.limits.max_workers,
        parallelism_note=(
            "implementation subtasks run in parallel worktrees when the developer finds "
            "disjoint file scopes; review, security and QA run in parallel after integration"
        ),
        cost_estimate_usd=round(estimate, 4),
        planned_models=list({m.ref: m for m in (analyst, implementer, reviewer)}.values()),
        implementer_model=implementer.ref,
        reviewer_model=reviewer.ref,
        action_profile_id=run.action_profile_id,
        policy_version=run.policy_version,
        contract_version=run.contract_version,
        planned_actions=list(CODE_CHANGE_ACTIONS),
        budgets=run.limits.budgets.model_dump(),
        closure_checks=list(contract.closure_checks),
        created_at=utcnow(),
        status="PROPOSED",
    )
    plan.hash = plan_hash(plan)
    return plan


def plan_hash(plan: TaskPlan) -> str:
    payload = plan.model_dump(mode="json", exclude={"hash", "status", "validation_errors"})
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]


def validate_plan(
    plan: TaskPlan, run: Run, contract: DecisionContract
) -> tuple[list[str], list[str]]:
    """(errors, authority_violations). Authority violations are not fixable by re-planning."""
    errors: list[str] = []
    authority: list[str] = []
    if plan.trace_id != run.trace_id:
        errors.append("plan belongs to another run")
    if plan.action_profile_id != run.action_profile_id:
        errors.append(
            f"plan uses action profile {plan.action_profile_id}, run has {run.action_profile_id}"
        )
    if plan.contract_version != run.contract_version or plan.policy_version != run.policy_version:
        errors.append("plan contract/policy version differs from the run snapshot")
    if plan.worker_count_max > run.limits.max_workers:
        errors.append(
            f"worker_count_max {plan.worker_count_max} exceeds the cap {run.limits.max_workers}"
        )
    if plan.worker_count_requested > plan.worker_count_max:
        errors.append("worker_count_requested exceeds worker_count_max")
    allowed = set(run.limits.allowed_models)
    for model in plan.planned_models:
        if model.ref not in allowed:
            errors.append(f"model {model.ref} is not on the allow-list")
    if contract.plan.require_independent_reviewer and plan.reviewer_model == plan.implementer_model:
        errors.append("the reviewer model must differ from the implementer model")
    for sub in plan.subtasks:
        if sub.model != "none" and sub.model not in allowed:
            errors.append(f"subtask {sub.task_id} uses a model off the allow-list: {sub.model}")
    for action in plan.planned_actions:
        tier = run.action_profile.tier(action)
        if tier != "auto":
            authority.append(f"{action} is {tier.upper()} in {run.action_profile_id}")
    budgets = run.limits.budgets
    if plan.cost_estimate_usd > budgets.max_cost_usd:
        errors.append(
            f"cost estimate {plan.cost_estimate_usd} exceeds max_cost_usd {budgets.max_cost_usd}"
        )
    if plan.budgets != budgets.model_dump():
        errors.append("plan budgets differ from the run's immutable budgets")
    missing = [c for c in contract.closure_checks if c not in plan.closure_checks]
    if missing:
        errors.append(f"closure checks missing from the plan: {missing}")
    if not any(s.role == "review" for s in plan.subtasks):
        errors.append("the plan has no independent review step")
    return errors, authority
