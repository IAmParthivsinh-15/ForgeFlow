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
  status: "planned" | "running" | "completed" | "failed" | "skipped";
  implemented: boolean;
  reason: string;
}

export interface RoutePlan {
  stages: RouteStage[];
  skipped: string[];
  rationale: string;
}

export interface ExecutionInfo {
  base_commit: string;
  base_ref: string;
  integration_branch: string | null;
  integration_commit: string | null;
  target_commit: string | null;
  verification_round: number;
  repair_attempts: number;
  extra_repairs_allowed: number;
  awaiting_decision: boolean;
  accepted_risks: string[];
  note: string | null;
  started_at: string;
  finished_at: string | null;
}

export type TaskStatus =
  | "PENDING"
  | "READY"
  | "DISPATCHED"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "BLOCKED"
  | "CANCELLED"
  | "RETRYING";

export interface CheckRun {
  kind: string;
  command: string;
  exit_code: number | null;
  passed: boolean;
  duration_ms: number;
  output: string;
  timed_out: boolean;
}

export type Severity = "info" | "low" | "medium" | "high" | "critical";
export type Verdict = "pass" | "fail" | "uncertain";

export interface ChangeAnalysis {
  files: string[];
  domains: string[];
  files_by_domain: Record<string, string[]>;
  risk: "low" | "medium" | "high";
  reviewers: string[];
  review_focus: string[];
}

export interface ReviewFinding {
  severity: Severity;
  category: string;
  file: string | null;
  line: number | null;
  message: string;
  suggestion: string;
}

export interface ReviewReport {
  decision: "approved" | "changes_requested" | "blocked";
  summary: string;
  findings: ReviewFinding[];
  requirements_alignment: string;
}

export interface SecurityFinding {
  category: string;
  severity: Severity;
  file: string | null;
  line: number | null;
  evidence: string;
  impact: string;
  remediation: string;
  source: string;
}

export interface ScanResult {
  tool: string;
  status: "completed" | "unavailable" | "error" | "skipped";
  findings: unknown[];
  detail: string;
  duration_ms: number;
}

export interface SecurityReport {
  owasp_edition: string;
  summary: string;
  categories: { id: string; status: "pass" | "fail" | "not_applicable" | "uncertain"; notes: string }[];
  findings: SecurityFinding[];
  scanners: ScanResult[];
  false_positives: string[];
}

export interface CriterionResult {
  id: string;
  status: "PASS" | "FAIL" | "UNCERTAIN";
  evidence: string;
  checks: string[];
}

export interface QAReport {
  summary: string;
  criteria: CriterionResult[];
  gaps: string[];
  downgraded: string[];
}

export interface CIReport {
  build: {
    provider: string;
    job: string;
    build_number: number | null;
    url: string | null;
    status: "SUCCESS" | "FAILURE" | "UNSTABLE" | "ABORTED" | "TIMEOUT";
    stages: { name: string; status: string; duration_ms: number }[];
    log_tail: string;
    duration_ms: number;
    commit: string;
  };
  pipeline: string;
  analysis: {
    failing_stage: string;
    summary: string;
    suspected_cause: string;
    evidence: string[];
    recommended_fix: string;
  } | null;
}

export interface FinalReport {
  outcome: "passed" | "passed_with_accepted_risks" | "failed" | "completed";
  summary: string;
  stages: { stage: string; verdict: Verdict | "not_run"; summary: string; round: number }[];
  acceptance_criteria: CriterionResult[];
  open_findings: string[];
  accepted_risks: string[];
  repair_rounds: number;
  files_changed: string[];
  branch: string | null;
  commit: string | null;
  ci_build_url: string | null;
  pr_title: string;
  pr_body: string;
  generated_at: string;
}

export interface A2AMessage {
  message_id: string;
  task_id: string | null;
  sender: string;
  receiver: string;
  message_type: string;
  request: string;
  context: string;
  status: "answered" | "timeout" | "error" | "refused";
  response: string | null;
  created_at: string;
}

export interface TaskResult {
  summary: string;
  files_changed: string[];
  commit: string | null;
  branch: string | null;
  checks: CheckRun[];
  risks: string[];
  next_actions: string[];
  merged_tasks: string[];
  conflicts_resolved: string[];
  verdict: Verdict | null;
  blocking: boolean;
  blocking_reasons: string[];
  change_analysis: ChangeAnalysis | null;
  review: ReviewReport | null;
  security: SecurityReport | null;
  qa: QAReport | null;
  ci: CIReport | null;
  a2a_messages: number;
}

export interface Task {
  task_id: string;
  key: string;
  title: string;
  kind: "decompose" | "implement" | "integrate" | "repair" | "review" | "security" | "qa" | "ci";
  agent_type: string;
  instructions: string;
  status: TaskStatus;
  dependencies: string[];
  file_scope: string[];
  acceptance_criteria: string[];
  round: number;
  attempt: number;
  max_attempts: number;
  retryable: boolean;
  wait_reason: string | null;
  workspace_id: string | null;
  result: TaskResult | null;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
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
  execution: ExecutionInfo | null;
  report: FinalReport | null;
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
  task_id: string | null;
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
  tasks: Task[];
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

async function text(path: string): Promise<string> {
  const response = await fetch(path);
  if (!response.ok) throw new ApiError(response.status, response.statusText);
  return response.text();
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
  taskDiff: (taskId: string) => text(`/api/v1/tasks/${encodeURIComponent(taskId)}/diff`),
  workflowDiff: (id: string) => text(`/api/v1/workflows/${id}/diff`),
  retryTask: (taskId: string) =>
    request<Task>(`/api/v1/tasks/${encodeURIComponent(taskId)}/retry`, { method: "POST" }),
  cancelTask: (taskId: string) =>
    request<Task>(`/api/v1/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" }),
  decide: (id: string, action: "accept" | "repair") =>
    request<Workflow>(`/api/v1/workflows/${id}/decision`, {
      method: "POST",
      body: JSON.stringify({ action }),
    }),
  listA2A: (id: string) => request<A2AMessage[]>(`/api/v1/workflows/${id}/a2a`),
  answerQuestion: (questionId: string, body: { selected_option: string; custom_text?: string }) =>
    request<{ question: Question; workflow: Workflow }>(`/api/v1/questions/${questionId}/answer`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
};

export const TERMINAL: WorkflowStatus[] = ["COMPLETED", "FAILED", "CANCELLED"];
