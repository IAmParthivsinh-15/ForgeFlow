// Extensibility API client and types (mirrors forgeflow.schemas.extensibility).
import { ApiError, request } from "./api";

export type Policy = "auto" | "ask" | "deny";

export interface Connector {
  connector_id: string;
  type: "github" | "argocd";
  name: string;
  config: Record<string, unknown>;
  status: "configured" | "active" | "disabled" | "revoked" | "error";
  repositories: string[];
  scopes: string[];
  account: string | null;
  last_tested_at: string | null;
  last_error: string | null;
}

export interface MCPTool {
  tool_id: string;
  name: string;
  description: string;
  operation: "read" | "write" | "destructive";
  risk: string;
  policy_override: Policy | null;
  enabled: boolean;
}

export interface MCPServer {
  mcp_id: string;
  name: string;
  description: string;
  transport: "stdio" | "streamable_http" | "sse";
  url: string | null;
  stdio_server: string | null;
  status: string;
  approval_policy: string;
  allowed_agents: string[];
  tools_refreshed_at: string | null;
  last_error: string | null;
  preset: string | null;
}

export interface MCPPreset {
  key: string;
  name: string;
  description: string;
  transport: string;
  url: string | null;
  allowed_agents: string[];
  skill: string | null;
  denied_tools: string[];
}

export interface SkillMetadata {
  name: string;
  slug: string;
  version: string;
  description: string;
  when_to_use?: string;
  tags?: string[];
  allowed_agents?: string[];
  endpoints?: string[];
}

export interface Skill {
  skill_id: string;
  owner_id: string;
  slug: string;
  name: string;
  description: string;
  tags: string[];
  visibility: "private" | "public";
  status: string;
  trust: string;
  latest_version: string;
  forked_from: string | null;
}

export interface SkillVersion {
  version: string;
  metadata: SkillMetadata;
  instructions: string;
  files: Record<string, string>;
  checksum: string;
  validation: { passed: boolean; errors: string[]; warnings: string[] };
  created_at: string;
}

export interface SkillInstallation {
  installation_id: string;
  skill_id: string;
  version: string;
  project_id: string | null;
  enabled: boolean;
  enabled_agents: string[];
}

export interface Project {
  project_id: string;
  name: string;
  repository_path: string;
  github: {
    connector_id: string;
    repository: string;
    base_branch: string | null;
    auto_pull_request: boolean;
    draft: boolean;
  } | null;
  enabled_mcp_ids: string[];
  capability_policies: Record<string, Policy>;
  deployment: {
    connector_id: string;
    application: string;
    health_url: string | null;
    smoke_paths: string[];
    expected_status: number;
  } | null;
  browser_allowed_origins: string[];
}

export interface DeploymentProbe {
  name: string;
  target: string;
  passed: boolean;
  detail: string;
  latency_ms: number;
}

export interface DeploymentCheck {
  check_id: string;
  project_id: string;
  application: string;
  status: "healthy" | "unhealthy" | "unknown";
  sync_status: string | null;
  health_status: string | null;
  revision: string | null;
  rollback_to: number | null;
  probes: DeploymentProbe[];
  summary: string;
  rollback: "not_needed" | "available" | "requested" | "done" | "rejected" | "failed";
  rollback_detail: string | null;
  created_at: string;
}

export interface KnowledgeStatus {
  enabled: boolean;
  available: boolean;
  embeddings: string | null;
  counts: Record<string, number>;
  repositories: { repository_id: string; commit: string; files: number; chunks: number; indexed_at: string }[];
  last_error: string | null;
}

export interface KnowledgeHit {
  kind: string;
  id: string;
  score: number;
  title: string;
  text: string;
  path: string | null;
  workflow_id: string | null;
  status: string | null;
  severity: string | null;
  resolution: string | null;
  start_line: number | null;
  end_line: number | null;
  timestamp: string | null;
}

export interface Artifact {
  artifact_id: string;
  workflow_id: string | null;
  task_id: string | null;
  type: string;
  name: string;
  content_type: string;
  size_bytes: number;
  source: string;
  created_at: string;
}

export const artifactUrl = (id: string) => `/api/v1/artifacts/${id}`;

export interface Approval {
  approval_id: string;
  workflow_id: string | null;
  task_id: string | null;
  agent: string;
  capability_id: string;
  action: string;
  summary: string;
  details: Record<string, unknown>;
  risk: string;
  status: "pending" | "approved" | "rejected" | "expired";
  requested_at: string;
  expires_at: string;
  note: string | null;
}

export interface AuditEntry {
  audit_id: string;
  timestamp: string;
  workflow_id: string | null;
  agent: string;
  capability_id: string;
  capability_type: string;
  action: string;
  approval: string;
  result: string;
  latency_ms: number;
  error: string | null;
  skill_version: string | null;
}

export interface Manifest {
  agent: string;
  skills: { slug: string; name: string; version: string; level: string }[];
  mcp_tools: { name: string; policy: Policy }[];
  connector_capabilities: { name: string; policy: Policy }[];
  native_tools: string[];
  unavailable: string[];
  hash: string;
}

export const AGENTS = [
  "requirement_analyzer",
  "developer",
  "developer_subagent",
  "code_review",
  "security",
  "qa",
  "ci",
];

const post = (body: unknown = {}): RequestInit => ({ method: "POST", body: JSON.stringify(body) });
const patch = (body: unknown): RequestInit => ({ method: "PATCH", body: JSON.stringify(body) });

async function noContent(path: string, init: RequestInit): Promise<void> {
  const r = await fetch(path, { ...init, headers: { "Content-Type": "application/json" } });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new ApiError(r.status, body.detail ?? r.statusText);
  }
}

export const ext = {
  connectors: () => request<{ connector: Connector; has_credential: boolean }[]>("/api/v1/connectors"),
  createConnector: (body: {
    type?: "github" | "argocd";
    name: string;
    token: string;
    repositories?: string[];
    url?: string;
    applications?: string[];
    verify_tls?: boolean;
  }) =>
    request<{ connector: Connector }>("/api/v1/connectors", post(body)),
  connectorAction: (id: string, action: "test" | "disable" | "enable" | "revoke") =>
    request<unknown>(`/api/v1/connectors/${id}/${action}`, { method: "POST" }),
  deleteConnector: (id: string) => noContent(`/api/v1/connectors/${id}`, { method: "DELETE" }),

  mcps: () => request<{ server: MCPServer; has_credential: boolean; tools: MCPTool[] }[]>("/api/v1/mcps"),
  mcpAllowlist: () => request<Record<string, string>>("/api/v1/mcps/allowlist"),
  mcpPresets: () => request<MCPPreset[]>("/api/v1/mcps/presets"),
  enablePreset: (key: string, project_id: string | null) =>
    request<unknown>(`/api/v1/mcps/presets/${key}`, post({ project_id })),
  createMcp: (body: Record<string, unknown>) => request<unknown>("/api/v1/mcps", post(body)),
  mcpAction: (id: string, action: "refresh-tools" | "disable" | "enable" | "revoke") =>
    request<unknown>(`/api/v1/mcps/${id}/${action}`, { method: "POST" }),
  deleteMcp: (id: string) => noContent(`/api/v1/mcps/${id}`, { method: "DELETE" }),
  updateTool: (id: string, tool: string, body: { enabled?: boolean; policy?: Policy; clear_policy?: boolean }) =>
    request<MCPTool>(`/api/v1/mcps/${id}/tools/${encodeURIComponent(tool)}`, patch(body)),

  skills: (scope: "all" | "mine" | "public", q = "") =>
    request<Skill[]>(`/api/v1/skills?scope=${scope}&q=${encodeURIComponent(q)}`),
  skill: (id: string) =>
    request<{ skill: Skill; versions: SkillVersion[]; installations: SkillInstallation[]; owned: boolean }>(
      `/api/v1/skills/${id}`,
    ),
  createSkill: (body: { metadata: SkillMetadata; instructions: string }) => request<unknown>("/api/v1/skills", post(body)),
  uploadSkill: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    const r = await fetch("/api/v1/skills/upload", { method: "POST", body: form });
    if (!r.ok) throw new ApiError(r.status, (await r.json().catch(() => ({}))).detail ?? r.statusText);
    return r.json();
  },
  publishSkill: (id: string) => request<unknown>(`/api/v1/skills/${id}/publish`, post()),
  forkSkill: (id: string) => request<Skill>(`/api/v1/skills/${id}/fork`, post()),
  enableSkill: (id: string, body: { version?: string; project_id?: string | null; agents?: string[] }) =>
    request<SkillInstallation>(`/api/v1/skills/${id}/enable`, post(body)),
  disableSkill: (id: string, project_id: string | null) => noContent(`/api/v1/skills/${id}/disable`, post({ project_id })),

  projects: () => request<Project[]>("/api/v1/projects"),
  project: (id: string) => request<{ project: Project; detected_github_repository: string | null }>(`/api/v1/projects/${id}`),
  updateProject: (id: string, body: Record<string, unknown>) => request<Project>(`/api/v1/projects/${id}`, patch(body)),
  manifest: (project_id: string, agent: string) =>
    request<Manifest>(`/api/v1/capabilities/available?project_id=${project_id}&agent=${agent}`),

  approvals: (status?: string, workflow_id?: string) => {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (workflow_id) params.set("workflow_id", workflow_id);
    return request<Approval[]>(`/api/v1/approvals?${params}`);
  },
  decide: (id: string, approve: boolean, note?: string) =>
    request<Approval>(`/api/v1/approvals/${id}/${approve ? "approve" : "reject"}`, post({ note })),
  deployments: (project_id?: string) =>
    request<DeploymentCheck[]>(`/api/v1/deployments${project_id ? `?project_id=${project_id}` : ""}`),
  verifyDeployment: (project_id: string) =>
    request<DeploymentCheck>(`/api/v1/projects/${project_id}/deployments/verify`, post()),
  rollback: (check_id: string) =>
    request<DeploymentCheck>(`/api/v1/deployments/${check_id}/rollback`, post({ confirm: true })),

  knowledgeStatus: () => request<KnowledgeStatus>("/api/v1/knowledge/status"),
  searchKnowledge: (q: string, kind?: string, project_id?: string) => {
    const params = new URLSearchParams({ q });
    if (kind) params.set("kind", kind);
    if (project_id) params.set("project_id", project_id);
    return request<KnowledgeHit[]>(`/api/v1/knowledge/search?${params}`);
  },
  reindex: (project_id: string, force = false) =>
    request<Record<string, unknown>>(`/api/v1/knowledge/projects/${project_id}/index`, post({ force })),
  artifacts: (workflow_id: string) => request<Artifact[]>(`/api/v1/workflows/${workflow_id}/artifacts`),

  audit: (workflow_id?: string) =>
    request<AuditEntry[]>(`/api/v1/audit/capabilities${workflow_id ? `?workflow_id=${workflow_id}` : ""}`),
};

// ------------------------------------------------------------------- L4 autonomy

export type RunStatus =
  | "RECEIVED"
  | "PLANNING"
  | "PLANNED"
  | "EXECUTING"
  | "VERIFYING"
  | "RETRYING"
  | "PAUSED_BY_GUARDRAIL"
  | "STOPPING"
  | "STOPPED"
  | "RESOLVED"
  | "CLOSED_NO_ACTION"
  | "ESCALATED";

export interface PlanSubtask {
  task_id: string;
  purpose: string;
  role: string;
  worker_role: string;
  model: string;
  expected_output: string;
  dependencies: string[];
  parallel_group: string | null;
}

export interface TaskPlan {
  schema_version: string;
  trace_id: string;
  workflow_id: string;
  objective: string;
  source_item: Record<string, unknown>;
  subtasks: PlanSubtask[];
  worker_count_requested: number;
  worker_count_max: number;
  parallelism_note: string;
  cost_estimate_usd: number;
  planned_models: { provider: string; model: string; version: string }[];
  implementer_model: string;
  reviewer_model: string;
  action_profile_id: string;
  policy_version: string;
  contract_version: string;
  planned_actions: string[];
  budgets: Record<string, number>;
  closure_checks: string[];
  created_at: string;
  status: string;
  validation_errors: string[];
  hash: string;
}

export interface AutonomyRun {
  run_id: string;
  trace_id: string;
  trigger: string;
  source_item: { repository: string; number: number; url: string; title: string; labels: string[]; state: string };
  status: RunStatus;
  decision: string | null;
  decision_reason: string | null;
  contract_version: string;
  action_profile_id: string;
  mode: "observe" | "autonomous";
  workflow_id: string | null;
  plan: TaskPlan | null;
  counters: {
    tokens: number;
    cost_usd: number;
    runtime_seconds: number;
    retries: number;
    failed_tasks: number;
    workers_spawned: number;
    max_parallel_observed: number;
    comments_posted: number;
  };
  limits: { max_workers: number; allowed_models: string[]; budgets: Record<string, number> };
  closure: { name: string; passed: boolean; detail: string }[];
  escalation: { condition: string; rule: string; summary: string; decision_needed: string; escalated_at: string } | null;
  stop: { actor: string; reason: string; requested_at: string; stopped_at: string | null; cancelled_tasks: string[] } | null;
  resumes: number;
  guardrail: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface RunEvent {
  seq: number;
  type: string;
  timestamp: string;
  actor: string;
  data: Record<string, unknown>;
}

export interface AutonomyStatus {
  mode: "observe" | "autonomous";
  contract_version: string;
  contract_hash: string;
  responsibility: string;
  source: { repository: string; project_repository_path: string; connector_id: string; sweep_interval_seconds: number };
  source_ready: boolean;
  webhook_secret_configured: boolean;
  active: Record<string, number>;
  open_alerts: number;
  guardrails: {
    max_workers: number;
    allowed_models: string[];
    budgets: Record<string, number>;
    circuit_breakers: Record<string, number>;
  };
}

export interface AlertItem {
  alert_id: string;
  kind: string;
  severity: string;
  trace_id: string | null;
  summary: string;
  status: string;
  raised_at: string;
}

export interface ReviewRow {
  trace_id: string;
  source: string;
  status: string;
  decision: string | null;
  reason: string | null;
  mode: string;
  interventions: { kind: string; classification: string; condition?: string }[];
  autonomous_without_intervention: boolean;
  avoidable_dependencies: unknown[];
  closure_failed: string[];
}

export const autonomy = {
  status: () => request<AutonomyStatus>("/api/v1/autonomy/status"),
  runs: (status?: string) => request<AutonomyRun[]>(`/api/v1/autonomy/runs${status ? `?status=${status}` : ""}`),
  run: (trace: string) => request<AutonomyRun>(`/api/v1/autonomy/runs/${trace}`),
  events: (trace: string) => request<RunEvent[]>(`/api/v1/autonomy/runs/${trace}/events`),
  stop: (trace: string, reason: string) => request<AutonomyRun>(`/api/v1/autonomy/runs/${trace}/stop`, post({ reason })),
  resume: (trace: string) => request<AutonomyRun>(`/api/v1/autonomy/runs/${trace}/resume`, post()),
  rerun: (trace: string) => request<AutonomyRun>(`/api/v1/autonomy/runs/${trace}/rerun`, post()),
  intake: (number: number) => request<AutonomyRun>("/api/v1/autonomy/intake", post({ number })),
  sweep: () => request<Record<string, number>>("/api/v1/autonomy/sweep", post()),
  alerts: () => request<AlertItem[]>("/api/v1/autonomy/alerts"),
  ack: (id: string) => request<{ acknowledged: boolean }>(`/api/v1/autonomy/alerts/${id}/ack`, post()),
  review: (last = 5) => request<ReviewRow[]>(`/api/v1/autonomy/review?last=${last}`),
};
