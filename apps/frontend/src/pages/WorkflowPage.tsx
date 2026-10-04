import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { ActivityLog, AgentRuns } from "../components/ActivityLog";
import { ClarificationDialog } from "../components/ClarificationDialog";
import { DevelopmentView } from "../components/DevelopmentView";
import { A2APanel, DecisionPanel, ReportView } from "../components/ReportView";
import { RoutePlanView } from "../components/RoutePlanView";
import { Card, SpecificationView } from "../components/SpecificationView";
import { StatusBadge } from "../components/StatusBadge";
import { api, TERMINAL } from "../lib/api";
import { useWorkflowStream } from "../lib/useWorkflowStream";

export function WorkflowPage() {
  const { workflowId = "" } = useParams();
  useWorkflowStream(workflowId);

  const detail = useQuery({ queryKey: ["workflow", workflowId], queryFn: () => api.getWorkflow(workflowId) });
  const events = useQuery({ queryKey: ["events", workflowId], queryFn: () => api.listEvents(workflowId) });
  const cancel = useMutation({ mutationFn: () => api.cancelWorkflow(workflowId), onSuccess: () => detail.refetch() });

  if (detail.isPending) return <p className="text-sm text-slate-500">Loading…</p>;
  if (detail.isError) return <p className="text-sm text-rose-600">{(detail.error as Error).message}</p>;

  const { workflow, specification, questions, agent_runs, tasks } = detail.data;
  const answered = questions.filter((q) => q.status === "answered");

  return (
    <div className="flex flex-col gap-6">
      <div>
        <Link to="/" className="text-sm text-slate-500 hover:underline">
          ← All workflows
        </Link>
        <div className="mt-2 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold break-words">{workflow.request}</h1>
            <p className="mt-1 font-mono text-xs text-slate-500">
              {workflow.workflow_id}
              {workflow.repository_path && ` · repo: ${workflow.repository_path}`}
              {workflow.intake && ` · intent: ${workflow.intake.intent}`}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <StatusBadge status={workflow.status} />
            {!TERMINAL.includes(workflow.status) && (
              <button
                onClick={() => cancel.mutate()}
                disabled={cancel.isPending}
                className="rounded-lg border border-slate-300 px-3 py-1 text-xs hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
              >
                Cancel
              </button>
            )}
          </div>
        </div>
      </div>

      {workflow.execution?.awaiting_decision && <DecisionPanel workflow={workflow} />}

      {workflow.error && !workflow.execution?.awaiting_decision && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800 dark:border-rose-900 dark:bg-rose-950/50 dark:text-rose-300">
          {workflow.error}
        </div>
      )}

      {workflow.status === "PLANNING" && !specification && (
        <Card title="Requirement analysis">
          <p className="text-sm text-slate-500">
            The Requirement Analyzer is inspecting the request{workflow.repository_path ? " and repository" : ""}…
          </p>
        </Card>
      )}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="flex min-w-0 flex-col gap-4">
          {workflow.report && <ReportView report={workflow.report} />}
          {workflow.route_plan && <RoutePlanView plan={workflow.route_plan} />}
          {(workflow.execution || tasks.length > 0) && (
            <DevelopmentView workflow={workflow} tasks={tasks} runs={agent_runs} />
          )}
          {specification && <SpecificationView spec={specification} />}
          {answered.length > 0 && (
            <Card title="Clarifications">
              <ul className="space-y-2 text-sm">
                {answered.map((q) => {
                  const option = q.options.find((o) => o.id === q.answer?.selected_option);
                  return (
                    <li key={q.question_id}>
                      <div className="text-slate-500">{q.question}</div>
                      <div className="font-medium">
                        {q.answer?.selected_option === "CUSTOM" ? q.answer.custom_text : option?.label}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </Card>
          )}
        </div>
        <div className="flex min-w-0 flex-col gap-4">
          <ActivityLog events={events.data ?? []} />
          <A2APanel workflowId={workflow.workflow_id} />
          <AgentRuns runs={agent_runs.filter((r) => !r.task_id)} />
        </div>
      </div>

      {workflow.status === "AWAITING_CLARIFICATION" && (
        <ClarificationDialog
          workflowId={workflow.workflow_id}
          questions={questions.filter((q) => q.round === workflow.clarification_round)}
        />
      )}
    </div>
  );
}
