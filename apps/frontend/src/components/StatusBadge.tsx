import type { WorkflowStatus } from "../lib/api";

const PILL: Partial<Record<WorkflowStatus, string>> = {
  PLANNING: "ff-pill-blue",
  AWAITING_CLARIFICATION: "ff-pill-amber",
  PLANNED: "ff-pill-green",
  EXECUTING: "ff-pill-blue",
  INTEGRATING: "ff-pill-blue",
  PAUSED: "ff-pill-amber",
  REVIEWING: "ff-pill-blue",
  TESTING: "ff-pill-blue",
  CI: "ff-pill-blue",
  WAITING_FOR_APPROVAL: "ff-pill-amber",
  PUBLISHING: "ff-pill-blue",
  COMPLETED: "ff-pill-green",
  FAILED: "ff-pill-red",
  CANCELLED: "ff-pill-gray",
};

const LABELS: Partial<Record<WorkflowStatus, string>> = {
  PLANNING: "analyzing",
  AWAITING_CLARIFICATION: "needs input",
  PLANNED: "planned",
  EXECUTING: "developing",
  INTEGRATING: "integrating",
  PAUSED: "paused",
  REVIEWING: "reviewing",
  TESTING: "testing",
  CI: "running ci",
  WAITING_FOR_APPROVAL: "awaiting approval",
  PUBLISHING: "opening pr",
};

export const RUNNING: WorkflowStatus[] = ["PLANNING", "EXECUTING", "INTEGRATING", "REVIEWING", "TESTING", "CI", "PUBLISHING"];

/** Mono pill in the theme: blue = running, green = done, amber = waiting, red = failed. */
export function StatusBadge({ status }: { status: WorkflowStatus }) {
  return (
    <span className={`${PILL[status] ?? "ff-pill-gray"} gap-1.5`}>
      {RUNNING.includes(status) && <span className="size-1.5 animate-pulse rounded-full bg-current" />}
      {LABELS[status] ?? status.replace(/_/g, " ").toLowerCase()}
    </span>
  );
}
