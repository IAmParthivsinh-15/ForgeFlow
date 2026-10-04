# ForgeFlow

Autonomous software-engineering control plane. Full specification: [Implementation_v1.md](Implementation_v1.md).

**Current status: Phase 0 (bootstrap) + Milestone 1 (requirement-first orchestration).**

```text
User request
  → Orchestrator (intake: intent + summary)
  → Requirement Analyzer (reads the repository, read-only)
      ├─ needs clarification → question with 2–3 options + AI recommendation + custom answer
      │                         → user answers → analysis runs again (spec version + 1)
      └─ finalized → checklist + acceptance criteria + required capabilities
  → Conditional routing (only the capabilities that are needed)
  → Workflow = PLANNED
```

The routed stages (Developer, Code Review, Security, QA, CI) are recorded in the plan but not
executed yet. Those agents arrive in Milestones 2 and 3 (spec §198).

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
clarification dialog) without an LLM. Stop with `docker compose down`.

## 2. Using real models

1. In `.env`, set `FAKE_LLM=false` and fill in at least one provider: an API key **and** model ids.
2. NVIDIA NIM is tried first, then OpenAI/LiteLLM, Groq, and Ollama. A provider without a
   key or model id is skipped. The order and model profiles are defined in
   [config/models.yaml](config/models.yaml).
3. Restart: `docker compose up -d`.

`GET /health` shows the active fallback chain per profile (model ids only, never keys).

Every agent run records each provider attempt (provider, model, latency, reason for falling back).
These are shown under **Agent runs** in the UI.

## 3. Analysing a repository

Put or clone repositories into [repos/](repos/). Each folder appears in the UI's **Repository**
drop-down. The folder is mounted read-only. The Requirement Analyzer can only list, read and
search inside the selected repository, and cannot read `.env`, key files, `.git`, or
`node_modules`.

## 4. Local development (without containers for the app)

Requirements: [uv](https://docs.astral.sh/uv/), Node 20+, Docker (for MongoDB/Redis/Kafka).

```bash
docker compose up -d mongodb redis kafka
uv sync
uv run uvicorn forgeflow.apps.api.main:app --reload --port 8000   # terminal 1
uv run python -m forgeflow.apps.worker.main                         # terminal 2
cd apps/frontend && npm install && npm run dev                      # terminal 3 → http://localhost:5173
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
                          └───────────── MongoDB ◄── WorkflowService ◄── consumer ◄────────┘
                                                        │        (Redis: lock + dedup)
                                                        ▼
                                          Agents SDK agents → provider fallback chain
```

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
| Frontend | `apps/frontend/` | §63, §174 |

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

## API (Milestone 1)

| Method | Path |
|---|---|
| POST | `/api/v1/workflows` — `{request, repository_path?}` |
| GET | `/api/v1/workflows`, `/api/v1/workflows/{id}` |
| POST | `/api/v1/workflows/{id}/cancel` |
| GET | `/api/v1/workflows/{id}/requirements` (all spec versions) |
| GET | `/api/v1/workflows/{id}/questions` |
| POST | `/api/v1/questions/{question_id}/answer` — `{selected_option, custom_text?}` |
| GET | `/api/v1/workflows/{id}/events?after=N`, `/api/v1/workflows/{id}/stream` (SSE) |
| GET | `/api/v1/repositories`, `/health`, `/health/live` |

## Next milestones (spec §198)

2. Developer agent → subtask graph → parallel git worktrees → subagents → integration.
3. Code Review + Security (OWASP) + QA (acceptance-criteria driven) + CI (Jenkins).
