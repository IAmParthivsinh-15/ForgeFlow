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
