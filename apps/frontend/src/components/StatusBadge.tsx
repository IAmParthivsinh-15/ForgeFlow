import type { WorkflowStatus } from "../lib/api";

const STYLES: Partial<Record<WorkflowStatus, string>> = {
  PLANNING: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
  AWAITING_CLARIFICATION: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-300",
  PLANNED: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  EXECUTING: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
  INTEGRATING: "bg-indigo-100 text-indigo-800 dark:bg-indigo-950 dark:text-indigo-300",
  PAUSED: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-300",
  COMPLETED: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  FAILED: "bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300",
  CANCELLED: "bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
};

const LABELS: Partial<Record<WorkflowStatus, string>> = {
  PLANNING: "Analyzing requirements",
  AWAITING_CLARIFICATION: "Needs your input",
  PLANNED: "Planned",
  EXECUTING: "Developing",
  INTEGRATING: "Integrating",
  PAUSED: "Paused",
};

export function StatusBadge({ status }: { status: WorkflowStatus }) {
  const style = STYLES[status] ?? "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${style}`}>
      {(status === "PLANNING" || status === "EXECUTING" || status === "INTEGRATING") && <span className="size-1.5 animate-pulse rounded-full bg-current" />}
      {LABELS[status] ?? status.replace(/_/g, " ").toLowerCase()}
    </span>
  );
}
