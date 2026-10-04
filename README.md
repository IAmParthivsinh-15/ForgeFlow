# ForgeFlow

Autonomous software-engineering control plane. Full specification: [Implementation_v1.md](Implementation_v1.md).

**Current status: Phase 0 + Milestone 1 (requirement-first orchestration) + Milestone 2
(development: task graph, parallel worktrees, integration).**

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
  → Workflow = PAUSED ("development complete; next stages not implemented yet")
```

Code Review, Security, QA and CI stages are recorded in the plan but not executed yet; they
arrive in Milestone 3 (spec §198). ForgeFlow never pushes and never changes your own branches:
all work lands on `forgeflow/<workflow_id>/*` branches in the source repository.

---

## 1. Quick start (Docker, no API key needed)

Requirements: Docker Desktop.

```bash
cp .env.example .env            # optional for fake mode
FAKE_LLM=true docker compose up --build -d
```

PowerShell: `$env:FAKE_LLM="true"; docker compose up --build -d`

| What | Where |
|---|---|
| UI | http://localhost:8080 |
| API docs (OpenAPI) | http://localhost:8000/docs |
| Health (Mongo/Redis/Kafka) | http://localhost:8000/health |

`FAKE_LLM=true` uses deterministic fake agents, so you can test the whole flow (including the
clarification dialog, parallel worktrees and integration) without an LLM. In fake mode the
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

  The worker image includes Python 3.12, Node.js and npm. Repositories needing other toolchains
  report "executable not found" for their checks.

## 4. Local development (without containers for the app)

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

## 5. Checks

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
| Frontend | `apps/frontend/` | §63–65, §174 |

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
| GET | `/api/v1/repositories`, `/health`, `/health/live` |

## Next milestone (spec §198)

3. Code Review + Security (OWASP) + QA (acceptance-criteria driven) + CI (Jenkins), the
   CI failure → fix → retest loop, and A2A collaboration between those agents and the Developer.
