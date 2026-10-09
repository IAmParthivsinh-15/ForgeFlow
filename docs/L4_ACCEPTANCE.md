# L4 acceptance record

Tracks `additional.md` section 11, item by item: where it is implemented, how it was verified,
and what still needs evidence from **real runs** (a manually launched demo or an in-process
test is not sufficient evidence of L4 operation; see section 10 of the plan).

Status key: **built** = implemented and covered by automated tests; **needs real runs** =
the mechanism exists, but the plan requires evidence from repeated runs against the real
work source, which has not happened yet.

Verification so far (2026-10-09): the test suite (`uv run pytest`), including
`tests/unit/test_autonomy.py` and `tests/unit/test_l4_ops.py`. These run the real commander,
guards, scheduler, executor, git worktrees and GitHub client in one process, against a GitHub
API fake, a local bare git remote and a CI fake, with fake LLM agents pinned to distinct model
ids. Docker was not run in this pass, and no real LLM or real GitHub repository was used.

| # | Checklist item | Where | Status |
|---|---|---|---|
| 1 | Bounded responsibility + versioned seven-part contract | `config/autonomy/contract.yaml` (`version`, `responsibility`, trigger, sources, decision set, evidence, authority tiers, escalation conditions, closure checks); `schemas/autonomy.py` validates it; every run records `contract_version` + `contract_hash` | built |
| 2 | Real event trigger + backup sweep active | `POST /api/v1/autonomy/webhooks/github` (HMAC `X-Hub-Signature-256`), sweep loop in the orchestrator worker (`apps/worker/autonomy_loop.py`) | built; **needs real runs** (connect the service identity, set `source.*`, register the webhook) |
| 3 | Event and sweep share one idempotent path | `autonomy/intake.py` → `RunStore.create` (atomic `run_keys` insert); test: webhook + redelivery + sweep → one run, two `trigger.duplicate` events | built |
| 4 | Plan saved before any worker starts | `Commander._plan` saves `task_plan.json` (`plan.saved` event) before `start_execution`; test asserts `plan.saved` precedes the first task start | built |
| 5 | Plan validation blocks out-of-scope plans | `autonomy/planner.py::validate_plan`; tests: equal reviewer/implementer, model off the allow-list, cap exceeded, ASK action in plan → escalation | built |
| 6 | Caps, models, authority, budgets, breakers enforced outside the LLM | `ExecutionService.start_guard/spawn_guard`, `CapabilityGateway.run_guard` + action profile, `autonomy/guard.py`; test: runtime budget breaker → `PAUSED_BY_GUARDRAIL`, dispatch blocked, resume refused | built |
| 7 | ≥2 workers in parallel on independent work | parallel implement worktrees; `counters.max_parallel_observed` measured from task start/finish windows (test asserts ≥ 2 and ≤ cap) | built with fake agents; **needs real runs** with real decomposition |
| 8 | A different model independently challenges the output | plan rule `reviewer_model ≠ implementer_model`; closure check compares the models that actually ran (agent run attempts) | built with distinct fake model ids; **needs real runs** with two configured providers (e.g. NVIDIA for coding, another for review in `config/models.yaml`) |
| 9 | AUTO / ASK / DENY enforced by the permission layer | `ActionProfile.tier` in the gateway (unlisted = ASK); tests for DENY and AUTO-over-catalog-default | built |
| 10 | Non-human identity + runtime secret injection | contract `source.connector_id` (service identity's token); secrets referenced, never in plans/events/logs (`runs._clean`, `redact`); `SECRET_BACKEND=vault` (`VaultBackend`, KV v2) | built; **needs** a real machine user / GitHub App token and (for production) Vault |
| 11 | Runs operate without a terminal and survive restart | commander + sweep run in the orchestrator-worker container; state in MongoDB; a restarted worker continues non-terminal runs | built; **needs real runs** (restart the worker mid-run in Docker and keep the record) |
| 12 | Emergency stop exercised during a real run, record retained | `forgeflow run stop <trace_id> --reason …`, `POST /runs/{trace}/stop`; test stops while a task is RUNNING and checks blocked dispatch, blocked external call, checkpoint, idempotency | built; **needs the required stop test on a real (Docker) run** - in-process test evidence is not a substitute |
| 13 | Resume re-reads authoritative state, no duplicate side effects | `RunControl.resume` (re-reads the issue, re-checks profile/budgets, re-queues halted tasks); test: one PR and one comment after stop + resume | built; **needs real runs** |
| 14 | Closure verified by re-reading the source of truth | `autonomy/closure.py` (PR state/draft/head SHA, CI+tests, independent review, security, issue + resolution comment, evidence) | built against the GitHub fake; **needs real runs** |
| 15 | Alerts, metrics, run history, evidence portfolio | `autonomy/alerts.py` (+ webhook), `forgeflow_l4_*` metrics, `ops/prometheus/alerts.yml`, Grafana *Autonomy* dashboard, `/runs/{trace}/portfolio`, UI *Autonomy* + run page | built; SLO thresholds are examples until measured |
| 16 | Learning produces governed candidates | `autonomy/learning.py`: candidates `proposed`; accepting records the decision with `applied: false` | built |
| 17 | Last-five-run review | `GET /api/v1/autonomy/review?last=5`, `forgeflow review`, UI section; classifies interventions as named vs avoidable | built; meaningful only after real runs |

## Rollout (plan section 10)

1. **Observe** (current default, `mode: observe`): runs build and verify locally and record the
   decision; push, PR and comments are blocked by the gateway and audited.
2. **Bounded autonomy**: publish a new contract version with `mode: autonomous` after repeated
   observe runs look right.
3. **Independent verification**: configure a second provider for the `review` profile.
4. **Resilience**: in Docker, test duplicate/missed webhooks, a killed agent worker, a provider
   outage, worker restart + resume, and an emergency stop during active work; keep each run's
   portfolio (`/runs/{trace}/portfolio`).
5. **Expand only from evidence**: widen labels, risk or actions only after the review of real
   runs supports it.
