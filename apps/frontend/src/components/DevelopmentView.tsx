import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type AgentRun, type Task, type Workflow } from "../lib/api";
import { DiffView } from "./DiffView";
import { Card } from "./SpecificationView";
import { TaskGraph } from "./TaskGraph";
import { TaskPanel } from "./TaskPanel";

/** Picks the most relevant task to show when the user has not chosen one. */
function defaultTask(tasks: Task[]): Task | undefined {
  return (
    tasks.find((t) => t.status === "FAILED") ??
    tasks.find((t) => t.status === "RUNNING") ??
    [...tasks].reverse().find((t) => t.result?.blocking) ??
    tasks.find((t) => t.kind === "integrate" && t.status === "COMPLETED") ??
    tasks[tasks.length - 1]
  );
}

export function DevelopmentView({ workflow, tasks, runs }: { workflow: Workflow; tasks: Task[]; runs: AgentRun[] }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showFullDiff, setShowFullDiff] = useState(false);
  const execution = workflow.execution;
  const fullDiff = useQuery({
    queryKey: ["workflow-diff", workflow.workflow_id, execution?.integration_commit],
    queryFn: () => api.workflowDiff(workflow.workflow_id),
    enabled: showFullDiff && !!execution?.integration_commit,
  });
  const selected = tasks.find((t) => t.task_id === selectedId) ?? defaultTask(tasks);
  const done = tasks.filter((t) => t.status === "COMPLETED").length;

  return (
    <div className="flex flex-col gap-4">
      <Card
        title={tasks.some((t) => t.kind === "decompose") ? "Development & verification" : "Verification"}
        aside={tasks.length > 0 && <span className="text-xs text-slate-500">{done}/{tasks.length} tasks completed</span>}
      >
        {execution && (
          <dl className="mb-4 grid gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
            <div>
              <dt className="text-xs text-slate-500">Base</dt>
              <dd className="font-mono text-xs">
                {execution.base_ref} @ {execution.base_commit.slice(0, 10)}
              </dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="text-xs text-slate-500">Integration branch</dt>
              <dd className="font-mono text-xs break-all">
                {execution.integration_branch
                  ? `${execution.integration_branch} @ ${execution.integration_commit?.slice(0, 10)}`
                  : "not integrated yet"}
              </dd>
            </div>
          </dl>
        )}
        {execution?.note && (
          <p className="mb-4 rounded-lg bg-emerald-50 p-3 text-sm text-emerald-900 dark:bg-emerald-950/50 dark:text-emerald-300">
            {execution.note}
          </p>
        )}
        {tasks.length > 0 ? (
          <TaskGraph tasks={tasks} selectedId={selected?.task_id ?? null} onSelect={setSelectedId} />
        ) : (
          <p className="text-sm text-slate-500">Waiting for the Developer agent to plan subtasks…</p>
        )}
        {execution?.integration_commit && (
          <div className="mt-4">
            <button
              onClick={() => setShowFullDiff((v) => !v)}
              className="rounded-lg border border-slate-300 px-3 py-1 text-xs hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
            >
              {showFullDiff ? "Hide full diff" : "Show full diff (base → integration)"}
            </button>
            {showFullDiff && (
              <div className="mt-3">
                {fullDiff.isPending ? <p className="text-sm text-slate-500">Loading diff…</p> : <DiffView diff={fullDiff.data ?? ""} />}
              </div>
            )}
          </div>
        )}
      </Card>
      {selected && <TaskPanel key={selected.task_id} task={selected} workflowId={workflow.workflow_id} runs={runs} />}
    </div>
  );
}
