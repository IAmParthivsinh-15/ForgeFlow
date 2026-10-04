// Types mirror forgeflow.schemas (Pydantic) - keep in sync with the backend.

export type WorkflowStatus =
  | "CREATED"
  | "PLANNING"
  | "AWAITING_CLARIFICATION"
  | "PLANNED"
  | "EXECUTING"
  | "INTEGRATING"
  | "REVIEWING"
  | "TESTING"
  | "CI"
  | "DEPLOYING"
  | "VERIFYING"
  | "COMPLETED"
  | "FAILED"
  | "PAUSED"
  | "CANCELLED";

export interface IntakeAssessment {
  intent: string;
  summary: string;
  is_engineering_request: boolean;
  repository_required: boolean;
  notes: string;
}

export interface RouteStage {
  stage_id: string;
  capability: string;
  agent: string;
  depends_on: string[];
  status: "planned" | "skipped";
  implemented: boolean;
  reason: string;
}

export interface RoutePlan {
  stages: RouteStage[];
  skipped: string[];
  rationale: string;
}

export interface Workflow {
  workflow_id: string;
  request: string;
  repository_path: string | null;
  status: WorkflowStatus;
  intake: IntakeAssessment | null;
  requirement_version: number;
  clarification_round: number;
  route_plan: RoutePlan | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface RequiredCapabilities {
  development: boolean;
  qa: boolean;
  code_review: boolean;
  security: boolean;
  ci: boolean;
}

export interface Specification {
  requirement_id: string;
  version: number;
  summary: string;
  goal: string;
  scope: { in_scope: string[]; out_of_scope: string[] };
  constraints: string[];
  assumptions: string[];
  checklist: { id: string; description: string; status: string }[];
  acceptance_criteria: { id: string; description: string; verification: string }[];
  required_capabilities: RequiredCapabilities;
  external_systems: string[];
  requires_human_approval: boolean;
  risk_level: "low" | "medium" | "high" | "critical";
  repository_observations: string[];
  clarifications: string[];
  status: "awaiting_clarification" | "finalized";
}

export interface QuestionOption {
  id: string;
  label: string;
  description: string;
  recommended: boolean;
  reason: string | null;
}

export interface Question {
  question_id: string;
  round: number;
  status: "awaiting_user" | "answered";
  question: string;
  why_it_matters: string;
  options: QuestionOption[];
  answer: { selected_option: string; custom_text: string | null } | null;
}

export interface ProviderAttempt {
  provider: string;
  model: string;
  status: "succeeded" | "failed";
  latency_ms: number;
  fallback_reason: string | null;
}

export interface AgentRun {
  run_id: string;
  agent_type: string;
  prompt_version: string;
  status: "completed" | "failed";
  attempts: ProviderAttempt[];
  tool_calls: { tool: string; args: Record<string, unknown> }[];
  error: string | null;
  started_at: string;
  completed_at: string;
}

export interface WorkflowDetail {
  workflow: Workflow;
  specification: Specification | null;
  questions: Question[];
  agent_runs: AgentRun[];
}

export interface WorkflowEvent {
  seq: number;
  event_id: string;
  event_type: string;
  timestamp: string;
  payload: Record<string, unknown>;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  listWorkflows: () => request<Workflow[]>("/api/v1/workflows"),
  getWorkflow: (id: string) => request<WorkflowDetail>(`/api/v1/workflows/${id}`),
  listEvents: (id: string) => request<WorkflowEvent[]>(`/api/v1/workflows/${id}/events`),
  listRepositories: () => request<string[]>("/api/v1/repositories"),
  createWorkflow: (body: { request: string; repository_path: string | null }) =>
    request<Workflow>("/api/v1/workflows", { method: "POST", body: JSON.stringify(body) }),
  cancelWorkflow: (id: string) =>
    request<Workflow>(`/api/v1/workflows/${id}/cancel`, { method: "POST" }),
  answerQuestion: (questionId: string, body: { selected_option: string; custom_text?: string }) =>
    request<{ question: Question; workflow: Workflow }>(`/api/v1/questions/${questionId}/answer`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
};

export const TERMINAL: WorkflowStatus[] = ["COMPLETED", "FAILED", "CANCELLED"];
