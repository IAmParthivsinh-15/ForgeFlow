# ForgeFlow

Autonomous software-engineering control plane, built from the ForgeFlow V1 specification
(`Implementation_v1.md`, kept outside this repository). Section numbers (§) below refer to it.

**Current status: spec phases 0–44 + the L4 autonomy layer (`additional.md`):
requirement-first orchestration, parallel worktrees, verification (review, OWASP security, QA,
Jenkins CI) with a bounded repair loop and A2A, the extensibility gateway (GitHub, MCP, skills,
approvals, audit), searchable engineering history (Elasticsearch), Prometheus/Grafana/Langfuse,
Kubernetes + Argo CD manifests, browser QA through Playwright MCP, and an L4 commander that
processes labelled GitHub issues end to end under a versioned decision contract.**

```text
User request
  → Orchestrator (intake: intent + summary)
  → Requirement Analyzer (reads the repository, read-only)
      ├─ needs clarification → question with 2–3 options + AI recommendation + custom answer
      │                         → user answers → analysis runs again (spec version + 1)
      └─ finalized → checklist + acceptance criteria + required capabilities
  → Conditional routing (only the capabilities that are needed)
  → Development (if routed and a git repository is attached)
      T1  Developer agent plans subtasks, each with a file scope + dependencies
      T2…Tn  Developer subagents, one git worktree + branch each; independent subtasks run in
             parallel, overlapping scopes are serialized; ForgeFlow commits; repo checks run
      Tn+1   Integration: merge every branch into forgeflow/<workflow>/integration,
             Integrator agent resolves conflicts, post-merge checks
  → Verification round on the integration commit (only the routed stages)
      Code Review ─┐                  Change Analyzer adds Security when auth / infra /
      Security ────┴→ QA → CI (Jenkins)  dependency / database files changed
  → Blockers? → repair task (Developer) → next round re-runs the failed stages, everything
                after them, and review. At most MAX_REPAIR_ATTEMPTS, then a human decides
                (accept the risks, or allow one more repair).
  → Final report + PR title/description
  → GitHub bound to the project? push forgeflow/<workflow> and open the PR
      (asks for your approval first: WAITING_FOR_APPROVAL)  → Workflow = COMPLETED
```

Requests that need no code (e.g. "Review PR #142", "Check this app against OWASP Top 10",
"Run the CI pipeline") skip development and verify the repository's current HEAD; their
findings are reported, not repaired. ForgeFlow never pushes and never changes your own
branches: all work lands on `forgeflow/<workflow_id>/*` branches in the source repository.

**What blocks (decided by ForgeFlow, not by the agents):**

| Stage | Blocks when |
|---|---|
| Code review | decision `blocked`, or `changes_requested` with a high/critical finding |
| Security | a high/critical finding (agent or scanner) in a changed file — any file for a pure audit |
| QA | any acceptance criterion FAILs, or the repository's test command fails. A PASS without an executed passing test is downgraded to UNCERTAIN |
| CI | the Jenkins build result is not SUCCESS |

---

## 1. Quick start (Docker, no API key needed)

Requirements: Docker Desktop.

```bash
cp .env.example .env            # optional for fake mode
FAKE_LLM=true docker compose --profile ci up --build -d
```

PowerShell: `$env:FAKE_LLM="true"; docker compose --profile ci up --build -d`

The `ci` profile starts a local Jenkins (http://localhost:8081, user `forgeflow`, password
`forgeflow-local`; change both via `JENKINS_USER` / `JENKINS_PASSWORD`). Without the profile
everything works except the CI stage, which retries and then pauses with "Jenkins unavailable".

| What | Where |
|---|---|
| UI | http://localhost:8080 |
| API docs (OpenAPI) | http://localhost:8000/docs |
| Health (Mongo/Redis/Kafka) | http://localhost:8000/health |

`FAKE_LLM=true` uses deterministic fake agents, so you can test the whole flow (including the
clarification dialog, parallel worktrees, integration, verification and A2A) without an LLM.
Add `demo-repair` to a request to make the fake reviewer request changes once, which shows
the repair loop. Scanners, the repository's tests and Jenkins builds are always real. In fake mode the
subagents write small placeholder files under `forgeflow-demo/` in the target repository's
branches. Stop with `docker compose down`.

To try development end to end, put a git repository with at least one commit in `repos/`
(see section 3) and submit a feature request against it.

## 2. Using real models

1. In `.env`, set `FAKE_LLM=false` and fill in at least one provider: an API key **and** model ids.
2. NVIDIA NIM is tried first, then OpenAI/LiteLLM, Groq, and Ollama. A provider without a
   key or model id is skipped. The order and model profiles are defined in
   [config/models.yaml](config/models.yaml).
3. Restart: `docker compose up -d`.

`GET /health` shows the active fallback chain per profile (model ids only, never keys).

Every agent run records each provider attempt (provider, model, latency, reason for falling back).
These are shown under **Agent runs** in the UI.

## 3. Repositories, worktrees and checks

Put or clone repositories into [repos/](repos/). Each folder appears in the UI's **Repository**
drop-down. Development requires the folder to be the top level of a git repository with at
least one commit (a plain folder is analysed but not developed).

- **Reading:** agents can only list, read and search inside the selected repository or their own
  worktree, never `.env`, key files, `.git`, or `node_modules`.
- **Writing:** each subtask works in `workspaces/<workflow>/<task>` on branch
  `forgeflow/<workflow>/<task>`, and may only write inside its declared file scope. Changes
  outside the scope (e.g. build artefacts) are discarded before ForgeFlow commits.
- **Results:** inspect from the source repository, e.g.
  `git log --graph forgeflow/<workflow>/integration` or `git diff main forgeflow/<workflow>/integration`.
  The UI shows each task's diff and the full base → integration diff.
- **Checks:** agents never run arbitrary shell. They ask for a check kind (`test`, `lint`,
  `typecheck`, `build`) and ForgeFlow runs the repository's own command, from `forgeflow.yaml`
  if present, otherwise auto-detected (`npm run …`, `python -m pytest`). Commands are allowlisted,
  run without a shell, with a timeout and an environment that carries no ForgeFlow secrets:

  ```yaml
  # repos/<your-repo>/forgeflow.yaml
  commands:
    setup: npm ci                 # optional, runs once per worktree before other checks
    test: npm test --silent
    lint: npm run lint --silent
  ```

  The worker and Jenkins images include Python 3.12, Node.js and npm. Repositories needing other
  toolchains report "executable not found" for their checks.
- **CI:** the CI stage renders a Jenkinsfile from these same commands (Checkout → Setup → Lint →
  Typecheck → Test → Build), creates or updates the job `forgeflow-<repo>`, triggers it for the
  exact commit under verification, and collects stages and the log. The LLM never writes
  pipeline code; it only diagnoses failures.
- **Security scanners** (worker image): Gitleaks (secrets), Bandit (Python), Semgrep with the
  OWASP-tagged rules in [config/semgrep/](config/semgrep/) (offline, metrics off), and
  `npm audit` / `pip-audit` (need network; reported as errors when offline). A scanner that is
  missing reports `unavailable`, and the affected OWASP categories become `uncertain`, never
  `pass`.

## 4. Extensibility: GitHub, MCP servers, skills

Open **Extensibility** in the UI (or use the API, see below).

**Before anything else**, set a secret key in `.env`. It encrypts every connector and MCP
credential; only ciphertext is stored, and API responses never return credentials.

```bash
uv run python -m forgeflow.scripts.generate_secret_key   # paste into FORGEFLOW_SECRET_KEY
```

| What | How |
|---|---|
| **GitHub** | *Connectors → Connect GitHub*: a fine-grained personal access token limited to your repositories, with **Contents: Read and write** and **Pull requests: Read and write**. Then *Projects → (your repo) → GitHub*: pick the connector and `owner/repo` (detected from `origin` when possible). When verification passes, ForgeFlow pushes `forgeflow/<workflow>` and opens a PR — after you approve it. It never pushes to your own branches and never merges. |
| **MCP servers** | *MCP servers → Add*: Streamable HTTP / SSE by URL (token sent as a header, never in the URL), or a **stdio** server chosen from [config/mcp_stdio_allowlist.yaml](config/mcp_stdio_allowlist.yaml) (Playwright, the MCP reference server). Users cannot supply their own commands. Tools are discovered and classified read / write / destructive; read tools run automatically, write tools ask, destructive tools are denied unless you set a per-tool policy. Enable a server per project and per agent. |
| **Skills** | *Skills → Create* or *Upload .zip* (`skill.md` + `metadata.json`, optional `references/`, `examples/`, `assets/` — Markdown/JSON/YAML/text only, 2 MB max). Content is checked for secrets, hidden characters, instruction-override and exfiltration wording, and undeclared URLs (stricter when publishing). Versions are immutable; enabling pins a version (roll back by enabling an older one). Skills can be private or public; others can use or fork public skills but not edit them. |
| **Approvals** | Anything with an `ask` policy (e.g. opening a PR, MCP write tools) waits for you on the workflow page and under *Approvals* (default timeout 30 minutes). Rejecting is a normal outcome — the workflow completes without that action. |
| **Audit** | *Audit log* lists every capability use: agent, capability, approval decision, result, latency, skill versions injected. |

How agents receive capabilities: at execution start ForgeFlow resolves, per agent, the skills
and tools the project enables and policy allows, and saves that **capability snapshot** on the
workflow (pinned skill versions, MCP configuration hashes, connector scopes). Skills are added
to the agent's instructions as clearly-delimited guidance (metadata only for the Requirement
Analyzer; full text for relevant skills within a budget; summaries otherwise). MCP tools are
never handed to agents directly: each becomes a proxy whose every call goes through policy,
approval, revocation check and audit. Revoking a connector or MCP server blocks further calls
immediately. *Projects → What an agent receives here* previews any agent's manifest.

### MongoDB Atlas

1. In Atlas, create a database user and allow your IP (*Network Access*).
2. In `.env` set both (the first is used by the containers, the second by processes on the host):
   ```env
   MONGODB_URI_CONTAINERS=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority
   MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority
   ```
3. `docker compose --profile ci up -d --build`. (The local `mongodb` container still starts but
   is unused; transactions work on Atlas, including the free tier.)

The free tier has 512 MB. Published events expire after `EVENT_RETENTION_DAYS` (30) and audit
records after `AUDIT_RETENTION_DAYS` (90); events still waiting to be published never expire.

## 5. Search, monitoring, browser QA, deployments (spec §40–44)

Each is an optional docker compose profile; ForgeFlow keeps working when a profile is off.

| Profile | What it adds | Where |
|---|---|---|
| `search` | Elasticsearch: past failures, CI builds, test runs, review/security findings, agent runs and the code of each project, indexed by the orchestrator worker. Repair and CI-diagnosis agents receive similar earlier failures *and how they were fixed*; the Developer agents get `search_engineering_history` and `search_repository_index`. Keyword (BM25) search works offline; set `EMBEDDING_*` for hybrid semantic search. | UI *Knowledge* |
| `observability` | Prometheus (spec §67 metrics, from the event stream + API latency, Kafka lag, Redis latency, worktrees) and Grafana with 7 provisioned dashboards (`ops/grafana/generate_dashboards.py`). Langfuse Cloud tracing turns on when `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` are set; prompt/response text is exported only with `TRACE_INCLUDE_CONTENT=true`. | http://localhost:9090, http://localhost:3000 |
| `browser` | Microsoft Playwright MCP in its own container on an internal network (no internet). A `browser_test` acceptance criterion makes ForgeFlow serve the commit under test (static files, or the `preview:` command in `forgeflow.yaml`) and the QA agent verifies it in Chromium through MCP tools - no hand-written scripts. Screenshots are stored as evidence. Enable per project: *Projects → Browser QA*. | UI QA report |

```bash
docker compose --profile ci --profile search --profile observability --profile browser up -d --build
```

**Kubernetes and Argo CD:** see [k8s/README.md](k8s/README.md) - Kustomize base + `local`
(minikube) / `atlas` / `gitops` overlays, default-deny NetworkPolicies, HPAs, and an Argo CD
Application with manual sync. Connect Argo CD under *Connectors*, bind an application to a
project, then *Verify deployment*: Argo CD status + health URL + smoke paths; an unhealthy
deployment offers a rollback that waits for your approval. The `Jenkinsfile` is ForgeFlow's own
CI (lint, types, tests, images, optional GitOps tag bump).

## 6. Autonomy (L4): GitHub issues end to end

ForgeFlow owns one bounded outcome, defined once in
[config/autonomy/contract.yaml](config/autonomy/contract.yaml): *process eligible low-risk bug
issues labelled `forgeflow` in one repository, resolve them as a verified draft PR linked on the
issue, and escalate anything else with evidence*.

```text
issue labelled forgeflow+bug ──webhook──┐
scheduled sweep (backup) ───────────────┴─► one idempotent run per issue (trace_id)
  → re-read GitHub, eligibility        → CLOSE_NO_ACTION (with evidence) | continue
  → Prepare: requirement analysis      → out of scope / high risk → ESCALATE
  → task_plan.json validated in code   (worker cap, model allow-list, reviewer ≠ implementer,
                                        AUTO-only actions, budgets) and SAVED - no plan, no workers
  → Do: parallel worktrees (≥2 when separable)   Review: a different model   QA, security, CI
  → draft PR (AUTO) + resolution comment → closure re-reads GitHub → RESOLVED | ESCALATED
```

| Guarantee | How it is enforced |
|---|---|
| Plan before workers | `start_guard`/`spawn_guard` in the scheduler refuse execution and every dispatch without a validated, saved plan |
| Authority tiers | the action profile (AUTO / ASK / DENY) is applied by the capability gateway on every external call; anything unlisted asks |
| Immutable limits | budgets, worker cap, models and profile are snapshotted on the run at intake |
| Circuit breakers | tokens, cost, runtime, retries, failed tasks, provider failures, tool errors → `PAUSED_BY_GUARDRAIL` + alert |
| Independent review | closure fails unless the review ran on a different model than every implementer run |
| Closure | PR head SHA = verified commit, draft, CI + tests passed, security passed, issue still open with the resolution comment - all re-read from GitHub |
| Emergency stop | `forgeflow run stop <trace_id> --reason "..."` (or the run page): blocks spawns and external actions at once, halts workers, rejects pending approvals, checkpoints, idempotent; resume re-reads GitHub and avoids duplicate side effects |
| Service identity, secrets | the contract's `source.connector_id` (machine user / GitHub App token); secrets in the encrypted store or Vault (`SECRET_BACKEND=vault`) |

**Set it up (observe mode first, as the rollout plan requires):**

1. Connect GitHub with the *service identity's* fine-grained token (*Connectors*).
2. In `config/autonomy/contract.yaml` set `source.repository`, `source.project_repository_path`
   (the folder under `repos/`) and `source.connector_id`. Keep `mode: observe`.
3. Set `GITHUB_WEBHOOK_SECRET` in `.env` and add a GitHub webhook for *Issues* to
   `https://<your host>/api/v1/autonomy/webhooks/github` (the sweep catches issues anyway).
4. Label an issue `forgeflow` + `bug`. Watch *Autonomy* in the UI or the cockpit
   (`ops/cockpit/cockpit.sh`, or `ops/cockpit/cockpit.ps1` on Windows).
5. After repeated observe-mode runs look right, publish a new contract version with
   `mode: autonomous`.

The evidence portfolio of a run is `GET /api/v1/autonomy/runs/{trace_id}/portfolio`; the
last-five-run review is `GET /api/v1/autonomy/review`. What is verified and what still needs real
runs is tracked in [docs/L4_ACCEPTANCE.md](docs/L4_ACCEPTANCE.md).

## 7. Local development (without containers for the app)

Requirements: [uv](https://docs.astral.sh/uv/), Node 20+, Docker (for MongoDB/Redis/Kafka).

```bash
docker compose up -d mongodb redis kafka
uv sync
uv run uvicorn forgeflow.apps.api.main:app --reload --port 8000   # terminal 1
uv run python -m forgeflow.apps.worker.main                         # terminal 2
uv run python -m forgeflow.apps.agent_worker.main                   # terminal 3
cd apps/frontend && npm install && npm run dev                      # terminal 4 → http://localhost:5173
```

The `Makefile` wraps these (`make dev-api`, `make dev-worker`, `make dev-frontend`); on Windows
without `make`, run the commands directly.

## 8. Checks

```bash
uv run pytest                 # unit + API tests (in-memory store, fake agents; no infrastructure)
uv run ruff check src tests   # lint
uv run mypy                   # types
cd apps/frontend && npm run build
```

---

## Architecture (as built)

```text
React UI ──► nginx ──► Platform API (FastAPI) ──► MongoDB (state + outbox, one transaction)
                          ▲  SSE (reads event log)            │
                          │                                   ▼
                          │                         Orchestrator worker: outbox relay ──► Kafka
                          │                                   │                            │
                          └──── MongoDB ◄── WorkflowService / Scheduler (tick) ◄── consumer ◄┤
                                    ▲            │  (Redis: workflow lock + dedup, reaper)   │
                                    │            ▼                                           │
                                    │     task.dispatched ──────────────────────────────────►│
                                    │                                                        ▼
                                    └──────── Agent worker(s): claim → worktree → agent → commit
                                                  (heartbeats; Redis repository lock for git)
```

Kafka carries notifications; MongoDB decides ownership. A task runs only on the worker that wins
the `DISPATCHED → RUNNING` claim, so duplicate events are harmless. Workers heartbeat while
running; the orchestrator's reaper re-dispatches unclaimed tasks and retries tasks whose worker
stopped heart-beating (spec §48). Scale agents with `docker compose up -d --scale agent-worker=3`.

| Concern | Where | Spec |
|---|---|---|
| Agents (one folder each: `agent.py`, `prompt.md`, `tools.py`) | `src/forgeflow/agents/` | §6, §195 |
| Requirement Specification, clarification schemas | `src/forgeflow/schemas/requirement.py` | §172, §175 |
| Workflow state machine | `src/forgeflow/platform/orchestration/state_machine.py` | §18 |
| Conditional routing | `src/forgeflow/platform/orchestration/routing.py` | §177 |
| Workflow service (clarification loop) | `src/forgeflow/platform/orchestration/service.py` | §173, §176 |
| Outbox + Kafka relay, Redis coordination | `src/forgeflow/platform/events/` | §31–34, §136 |
| Model profiles + provider fallback | `src/forgeflow/models/`, `config/models.yaml` | §38, §141 |
| Read-only repository tools | `src/forgeflow/tools/filesystem/repository.py` | §27, §29 |
| Task model, task state machine, DAG validation, conflicts | `src/forgeflow/schemas/task.py`, `src/forgeflow/platform/task_graph/` | §16–20 |
| Scheduler, retries, pause/resume, reaper | `src/forgeflow/platform/orchestration/execution.py` | §46, §48, §49 |
| Task executor (decompose / implement / integrate) | `src/forgeflow/platform/execution/executor.py` | §24, §47, §50–52 |
| Worktree manager | `src/forgeflow/platform/worktrees/manager.py` | §21, §22, §184 |
| Git, scoped writes, allowlisted commands | `src/forgeflow/tools/{git,filesystem,shell,testing}/` | §73–77 |
| Change Analyzer (review routing) | `src/forgeflow/platform/verification/change_analyzer.py` | §53, §54, §189 |
| Review / Security / QA / CI stages | `src/forgeflow/platform/execution/verification.py` | §11–14, §55, §190 |
| Security scanners + Semgrep rules | `src/forgeflow/tools/security/scanners.py`, `config/semgrep/` | §14, §190 |
| Jenkins provider + pipeline templates | `src/forgeflow/platform/ci/` | §56, §191 |
| Repair loop, rounds, human decision | `src/forgeflow/platform/orchestration/execution.py` | §57 |
| A2A channel | `src/forgeflow/platform/a2a/channel.py` | §186–188 |
| Final report + PR text | `src/forgeflow/platform/orchestration/report.py` | §50, §58 |
| Extensibility gateway (facade) | `src/forgeflow/extensibility/facade.py` | §202 |
| Secret store (encrypted credentials) | `src/forgeflow/extensibility/secrets.py` | §71, §204 |
| Policy engine, approvals, capability gateway + audit | `src/forgeflow/extensibility/{policy,approvals,gateway}.py` | §29, §235–238 |
| Connectors, projects | `src/forgeflow/extensibility/{connectors,projects}.py` | §204–206, §241, §254 |
| GitHub plugin (API + safe push) | `src/forgeflow/integrations/github/client.py` | §58, §192 |
| MCP gateway + per-run proxies | `src/forgeflow/extensibility/mcp/service.py`, `extensibility/runtime.py` | §207–211, §225, §246 |
| Skills (validation, packages, registry, injection) | `src/forgeflow/extensibility/skills/` | §212–234, §251–253 |
| Capability resolver, manifests, snapshot | `src/forgeflow/extensibility/resolver.py` | §223–224, §242–245 |
| Knowledge: index, chunker, history, retrieval tools | `src/forgeflow/knowledge/`, `tools/knowledge_tools.py` | §36–37, §101 |
| Metrics, tracing (Langfuse via OTLP) | `src/forgeflow/observability/`, `ops/prometheus`, `ops/grafana` | §67–69, §102 |
| Browser QA: preview server, Playwright preset + skill | `src/forgeflow/tools/browser/`, `config/mcp_presets.yaml`, `config/skills/playwright-mcp/` | §28, §156, §266A |
| Artifacts (evidence files) | `src/forgeflow/platform/artifacts.py` | §155 |
| Argo CD plugin, deployment checks | `src/forgeflow/integrations/argocd/`, `src/forgeflow/platform/deployments.py` | §43, §157–158 |
| Kubernetes, Argo CD manifests | `k8s/` | §107–112 |
| L4 contract, runs, commander, guardrails, closure, stop/resume, learning | `config/autonomy/`, `src/forgeflow/autonomy/` | additional.md |
| Operator CLI, cockpit | `src/forgeflow/cli.py`, `ops/cockpit/` | additional.md §5, §8 |
| Frontend ("Obsidian" theme) | `apps/frontend/` | §63–65, §174 |

### Why the Python code is under `src/forgeflow/`

The spec puts `agents/` and `platform/` at the repository root. As top-level packages these would
shadow the OpenAI Agents SDK (`import agents`) and Python's standard-library `platform` module.
Nesting them under `forgeflow` keeps the spec's layout without those collisions.

### Design decisions

- **LLMs decide, deterministic code acts.** The Orchestrator agent only does intake. Workflow
  state, routing, IDs, versions and events are handled by deterministic code.
- **The user is the authority.** The AI recommendation is pre-selected in the dialog, but nothing
  is submitted until the user presses Continue.
- **Clarification rounds are capped** (`MAX_CLARIFICATION_ROUNDS`, default 3). In the last
  allowed round the analyzer is told to finalize. If it still asks a question, the workflow fails
  with a clear error instead of looping forever.
- **Structured output is checked.** Providers marked `structured_output: prompt` get the JSON
  schema in their instructions. Replies are checked with Pydantic, and the model is asked to fix
  invalid JSON (up to 2 times) before ForgeFlow falls back to the next provider.
- **Delivery is at-least-once.** Kafka consumers commit after handling; duplicates are absorbed
  by Redis de-duplication and a version check in the service.
- **Correctness over parallelism.** Two subtasks whose file scopes *may* overlap never run at the
  same time; the later one starts from the earlier one's commit. Dependent subtasks start from
  their dependencies' code.
- **Retries are bounded.** Provider errors, timeouts and lost workers retry with backoff up to
  `TASK_MAX_ATTEMPTS`; invalid plans, policy violations and unresolved merge conflicts do not.
  When nothing can progress the workflow is **PAUSED** with the reason, and a task can be retried
  manually (`POST /api/v1/tasks/{id}/retry`) or the workflow cancelled.
- **Agents don't commit.** Subagents edit files; ForgeFlow stages, commits and merges with git
  commands it builds itself from validated names.
- **A2A is bounded.** Review, Security and QA agents may ask the Developer up to
  `A2A_MAX_PER_RUN` questions (each with `A2A_TIMEOUT_SECONDS`). Answers are information only;
  every exchange is recorded (sender, receiver, task, request, response, status).
- **Reports are evidence, not prose.** The final report and PR description are assembled from
  the recorded task results, never written by an LLM.

## API

| Method | Path |
|---|---|
| POST | `/api/v1/workflows` — `{request, repository_path?}` |
| GET | `/api/v1/workflows`, `/api/v1/workflows/{id}` |
| POST | `/api/v1/workflows/{id}/cancel` |
| GET | `/api/v1/workflows/{id}/requirements` (all spec versions) |
| GET | `/api/v1/workflows/{id}/questions` |
| POST | `/api/v1/questions/{question_id}/answer` — `{selected_option, custom_text?}` |
| GET | `/api/v1/workflows/{id}/events?after=N`, `/api/v1/workflows/{id}/stream` (SSE) |
| GET | `/api/v1/workflows/{id}/tasks`, `/api/v1/tasks/{task_id}` |
| POST | `/api/v1/tasks/{task_id}/retry`, `/api/v1/tasks/{task_id}/cancel` |
| GET | `/api/v1/tasks/{task_id}/diff`, `/api/v1/workflows/{id}/diff` (base → integration) |
| GET | `/api/v1/workspaces?workflow_id=…`, `/api/v1/workspaces/{id}` |
| GET | `/api/v1/workflows/{id}/report` (final report incl. PR title/body) |
| POST | `/api/v1/workflows/{id}/decision` — `{action: "accept" \| "repair"}` when the repair limit is reached |
| GET | `/api/v1/workflows/{id}/a2a` (recorded agent-to-agent exchanges) |
| GET/POST | `/api/v1/connectors`, `/connectors/{id}` + `/test`, `/disable`, `/enable`, `/revoke`, `DELETE` |
| GET/POST | `/api/v1/mcps`, `/mcps/allowlist`, `/mcps/{id}` + `/test`, `/refresh-tools`, `/disable`, `/enable`, `/revoke`, `DELETE`; `PATCH /mcps/{id}/tools/{name}` |
| GET/POST | `/api/v1/skills?scope=&q=`, `/skills/upload`, `/skills/{id}` + `/versions`, `/publish`, `/fork`, `/enable`, `/disable`, `/status` |
| GET/PATCH | `/api/v1/projects`, `/projects/{id}` (GitHub binding, MCP servers, capability policies) |
| GET/POST | `/api/v1/approvals?status=&workflow_id=`, `/approvals/{id}/approve`, `/reject` |
| GET | `/api/v1/capabilities`, `/capabilities/available?project_id=&agent=`, `/audit/capabilities`, `/workflows/{id}/capabilities` |
| GET/POST | `/api/v1/knowledge/status`, `/knowledge/search?q=&kind=&project_id=`, `/knowledge/projects/{id}/index` |
| GET | `/api/v1/workflows/{id}/artifacts`, `/api/v1/artifacts/{id}` |
| GET/POST | `/api/v1/mcps/presets`, `/mcps/presets/{key}` |
| POST/GET | `/api/v1/projects/{id}/deployments/verify`, `/deployments?project_id=`, `/deployments/{id}`, `/deployments/{id}/rollback` |
| POST | `/api/v1/autonomy/webhooks/github` (HMAC), `/autonomy/intake`, `/autonomy/sweep` |
| GET/POST | `/api/v1/autonomy/status`, `/contract`, `/runs`, `/runs/{trace}` + `/plan`, `/events`, `/portfolio`, `/stop`, `/resume`, `/rerun` |
| GET/POST | `/api/v1/autonomy/alerts`, `/alerts/{id}/ack`, `/learning`, `/learning/{id}/candidates/{n}`, `/review?last=5` |
| GET | `/api/v1/repositories`, `/health`, `/health/live`, `/metrics` |

## Next phases

Spec §198 items 45–46 (evaluation suite, production hardening); real-run L4 evidence (see
[docs/L4_ACCEPTANCE.md](docs/L4_ACCEPTANCE.md)); per-task Kubernetes Jobs; authentication and
multi-user ownership (records already carry `owner_id`); OAuth connectors; Jira/Linear/Slack.
