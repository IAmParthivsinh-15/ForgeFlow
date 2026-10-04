import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type AgentRun, type Task } from "../lib/api";
import { DiffView } from "./DiffView";
import { TASK_STATUS_STYLE } from "./TaskGraph";

/** Task detail (spec section 64): what ran, where, what changed, and the evidence. */
export function TaskPanel({ task, workflowId, runs }: { task: Task; workflowId: string; runs: AgentRun[] }) {
  const queryClient = useQueryClient();
  const [showDiff, setShowDiff] = useState(false);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["workflow", workflowId] });
  const retry = useMutation({ mutationFn: () => api.retryTask(task.task_id), onSuccess: refresh });
  const cancel = useMutation({ mutationFn: () => api.cancelTask(task.task_id), onSuccess: refresh });
  const diff = useQuery({
    queryKey: ["task-diff", task.task_id, task.result?.commit],
    queryFn: () => api.taskDiff(task.task_id),
    enabled: showDiff && !!task.result?.commit,
  });
  const result = task.result;
  const taskRuns = runs.filter((r) => r.task_id === task.task_id);
  const open = !["COMPLETED", "CANCELLED"].includes(task.status);
  const error = (retry.error ?? cancel.error) as Error | null;

  return (
    <section className="flex min-w-0 flex-col gap-4 rounded-2xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-mono text-xs text-slate-500">{task.key} · {task.kind} · {task.agent_type.replace("_", " ")}</p>
          <h3 className="text-base font-semibold break-words">{task.title}</h3>
        </div>
        <div className="flex items-center gap-2">
          <span className={`rounded-full border px-2.5 py-0.5 text-xs font-medium ${TASK_STATUS_STYLE[task.status]}`}>
            {task.status.toLowerCase()} · attempt {task.attempt}/{task.max_attempts}
          </span>
          {task.status === "FAILED" && (
            <button onClick={() => retry.mutate()} disabled={retry.isPending} className={BUTTON}>
              Retry
            </button>
          )}
          {open && (
            <button onClick={() => cancel.mutate()} disabled={cancel.isPending} className={BUTTON}>
              Cancel
            </button>
          )}
        </div>
      </header>

      {error && <p className="text-sm text-rose-600">{error.message}</p>}
      {task.error && <p className="rounded-lg bg-rose-50 p-3 text-sm text-rose-800 dark:bg-rose-950/50 dark:text-rose-300">{task.error}</p>}
      {task.wait_reason && <p className="text-sm text-amber-700 dark:text-amber-400">{task.wait_reason}</p>}
      {task.instructions && <p className="text-sm text-slate-600 dark:text-slate-300">{task.instructions}</p>}

      <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
        {task.file_scope.length > 0 && <Item label="File scope" value={task.file_scope.join(", ")} mono />}
        {task.acceptance_criteria.length > 0 && <Item label="Acceptance criteria" value={task.acceptance_criteria.join(", ")} mono />}
        {result?.branch && <Item label="Branch" value={result.branch} mono />}
        {result?.commit && <Item label="Commit" value={result.commit.slice(0, 12)} mono />}
        {task.workspace_id && <Item label="Workspace" value={task.workspace_id} mono />}
        {result && result.merged_tasks.length > 0 && <Item label="Built on / merged" value={result.merged_tasks.join(", ")} mono />}
      </dl>

      {result?.summary && <p className="text-sm">{result.summary}</p>}

      {result && result.files_changed.length > 0 && (
        <div>
          <div className="mb-1 flex items-center justify-between">
            <h4 className="text-sm font-medium">Files changed ({result.files_changed.length})</h4>
            {result.commit && (
              <button onClick={() => setShowDiff((v) => !v)} className={BUTTON}>
                {showDiff ? "Hide diff" : "Show diff"}
              </button>
            )}
          </div>
          <ul className="space-y-0.5 font-mono text-xs text-slate-600 dark:text-slate-300">
            {result.files_changed.map((f) => (
              <li key={f} className="break-all">{f}</li>
            ))}
          </ul>
          {showDiff && <div className="mt-3">{diff.isPending ? <p className="text-sm text-slate-500">Loading diff…</p> : <DiffView diff={diff.data ?? ""} />}</div>}
        </div>
      )}

      {result && result.checks.length > 0 && (
        <div>
          <h4 className="mb-1 text-sm font-medium">Checks (executed by ForgeFlow)</h4>
          <ul className="space-y-1">
            {result.checks.map((c, i) => (
              <li key={i}>
                <details className="rounded-lg border border-slate-200 px-3 py-2 text-sm dark:border-slate-800">
                  <summary className="cursor-pointer">
                    <span className={c.passed ? "text-emerald-700 dark:text-emerald-400" : "text-rose-700 dark:text-rose-400"}>
                      {c.timed_out ? "timed out" : c.passed ? "passed" : "failed"}
                    </span>{" "}
                    · {c.kind} · <code className="text-xs">{c.command}</code> · {c.duration_ms} ms
                  </summary>
                  <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs text-slate-600 dark:text-slate-300">{c.output || "(no output)"}</pre>
                </details>
              </li>
            ))}
          </ul>
        </div>
      )}

      {result && result.risks.length > 0 && (
        <div>
          <h4 className="mb-1 text-sm font-medium">Risks</h4>
          <ul className="list-disc space-y-1 pl-5 text-sm text-amber-800 dark:text-amber-300">
            {result.risks.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      {result && result.conflicts_resolved.length > 0 && (
        <Item label="Merge conflicts resolved" value={result.conflicts_resolved.join(", ")} mono />
      )}

      {taskRuns.length > 0 && (
        <div className="text-xs text-slate-500">
          {taskRuns.map((r) => (
            <div key={r.run_id}>
              {r.agent_type} · prompt {r.prompt_version} · {r.attempts.map((a) => `${a.provider}:${a.model}`).join(" → ")}
              {r.tool_calls.length > 0 && ` · ${r.tool_calls.length} tool call(s)`}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

const BUTTON =
  "rounded-lg border border-slate-300 px-2.5 py-1 text-xs hover:bg-slate-100 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-800";

function Item({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className={`break-all ${mono ? "font-mono text-xs" : ""}`}>{value}</dd>
    </div>
  );
}
