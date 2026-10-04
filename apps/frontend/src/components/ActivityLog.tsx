import type { AgentRun, WorkflowEvent } from "../lib/api";
import { Card } from "./SpecificationView";

function describe(event: WorkflowEvent): string {
  const p = event.payload;
  switch (event.event_type) {
    case "workflow.status_changed":
      return `Status ${p.previous} → ${p.current}`;
    case "requirement.specification_created":
      return `Specification v${p.version} (${p.status})`;
    case "clarification.requested":
      return `Asked ${(p.question_ids as string[]).length} clarification question(s)`;
    case "clarification.answered":
      return `Answer received (${p.selected_option})`;
    case "requirement.analysis_requested":
      return `Requirement analysis requested (v${p.requirement_version})`;
    case "workflow.routed":
      return `Routed to: ${(p.stages as string[]).join(", ") || "no stages"}`;
    case "agent.run.completed":
    case "agent.run.failed":
      return `${p.agent_type} ${event.event_type.endsWith("failed") ? "failed" : "finished"} · ${(p.providers as string[]).join(", ")} · ${p.tool_calls} tool call(s)`;
    case "workflow.failed":
      return `Failed: ${p.error}`;
    case "workflow.execution_started":
      return `Development started from ${p.base_ref} @ ${String(p.base_commit).slice(0, 10)}`;
    case "workflow.execution_finished":
      return p.outcome === "not_started" ? `Development not started: ${p.reason}` : `Development finished → ${p.integration_branch}`;
    case "task.created":
      return `${p.key} created (${p.kind})`;
    case "task.dispatched":
      return `${p.key} dispatched${p.redispatch ? " again" : ""} (attempt ${p.attempt})`;
    case "task.started":
      return `${p.key} started`;
    case "task.completed":
      return `${p.key} completed${p.commit ? ` · ${String(p.commit).slice(0, 8)}` : ""}`;
    case "task.failed":
      return `${p.key} failed: ${p.error}`;
    case "task.blocked":
      return `${p.key} blocked`;
    case "task.retrying":
      return `${p.key} will be retried`;
    case "task.cancelled":
      return `${p.key} cancelled`;
    case "test.completed":
      return `${p.kind} ${p.passed ? "passed" : "failed"}: ${p.command}`;
    case "integration.conflict":
      return `Merge conflict integrating ${p.incoming}: ${(p.files as string[]).join(", ")}`;
    case "verification.round_started":
      return `Verification round ${p.round}: ${(p.stages as string[]).join(", ")}`;
    case "repair.requested":
      return `Repair round ${p.round} requested (attempt ${p.attempt})`;
    case "workflow.awaiting_decision":
      return "Repair limit reached; waiting for your decision";
    case "workflow.completed":
      return `Workflow completed: ${p.outcome}`;
    case "a2a.exchange":
      return `A2A ${p.sender} → ${p.receiver}: ${p.request} (${p.status})`;
    case "workspace.created":
      return `${p.key} worktree ready on ${p.branch}`;
    case "task.ready":
      return `${p.key} ready`;
    case "task.unblocked":
      return `${p.key} unblocked`;
    default:
      return event.event_type;
  }
}

export function ActivityLog({ events }: { events: WorkflowEvent[] }) {
  return (
    <Card title="Activity">
      <ol className="space-y-2 text-sm">
        {events.map((event) => (
          <li key={event.seq} className="flex gap-3">
            <time className="shrink-0 font-mono text-xs leading-5 text-slate-400">
              {new Date(event.timestamp).toLocaleTimeString()}
            </time>
            <span className="min-w-0 break-words">{describe(event)}</span>
          </li>
        ))}
      </ol>
    </Card>
  );
}

export function AgentRuns({ runs }: { runs: AgentRun[] }) {
  if (!runs.length) return null;
  return (
    <Card title="Agent runs">
      <ul className="space-y-3 text-sm">
        {runs.map((run) => (
          <li key={run.run_id} className="flex flex-col gap-1">
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="font-medium">{run.agent_type.replace("_", " ")}</span>
              <span className="text-xs text-slate-500">prompt {run.prompt_version}</span>
              <span className={`text-xs ${run.status === "failed" ? "text-rose-600" : "text-emerald-600"}`}>
                {run.status}
              </span>
            </div>
            {run.attempts.map((a, i) => (
              <div key={i} className="text-xs text-slate-500">
                {a.provider}:{a.model} · {a.status} · {a.latency_ms} ms
                {a.fallback_reason && <span className="text-rose-600"> · {a.fallback_reason}</span>}
              </div>
            ))}
            {run.tool_calls.length > 0 && (
              <div className="font-mono text-xs text-slate-500">
                {run.tool_calls.map((c) => c.tool).join(" → ")}
              </div>
            )}
            {run.error && <div className="text-xs text-rose-600">{run.error}</div>}
          </li>
        ))}
      </ul>
    </Card>
  );
}
