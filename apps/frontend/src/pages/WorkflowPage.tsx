import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ActivityLog, AgentRuns, describe } from "../components/ActivityLog";
import { ClarificationDialog } from "../components/ClarificationDialog";
import { DevelopmentView } from "../components/DevelopmentView";
import { ApprovalCard } from "../components/ext/ApprovalsTab";
import { A2APanel, DecisionPanel, ReportView } from "../components/ReportView";
import { RoutePlanView } from "../components/RoutePlanView";
import { Card, SpecificationView } from "../components/SpecificationView";
import { RUNNING, StatusBadge } from "../components/StatusBadge";
import { CIView, VerdictLine } from "../components/VerificationViews";
import { api, TERMINAL, type AgentRun, type Task, type WorkflowDetail } from "../lib/api";
import { ext } from "../lib/ext";
import { useWorkflowStream } from "../lib/useWorkflowStream";

const TABS = ["requirements", "task graph", "changes", "ci", "events"] as const;
type Tab = (typeof TABS)[number];

/** Status of one acceptance criterion from the latest QA round. */
function criterionPill(id: string, tasks: Task[]) {
  const qa = [...tasks].reverse().find((t) => t.kind === "qa" && t.result?.qa);
  const result = qa?.result?.qa?.criteria.find((c) => c.id === id);
  if (result?.status === "PASS") return <span className="ff-pill-green">verified</span>;
  if (result?.status === "FAIL") return <span className="ff-pill-red">failed</span>;
  if (result?.status === "UNCERTAIN") return <span className="ff-pill-amber">uncertain</span>;
  if (tasks.some((t) => t.kind === "qa" && (t.status === "RUNNING" || t.status === "DISPATCHED")))
    return <span className="ff-pill-blue">running</span>;
  return <span className="ff-pill-amber">pending</span>;
}

function AgentStatus({ runs, tasks }: { runs: AgentRun[]; tasks: Task[] }) {
  const running = tasks.filter((t) => t.status === "RUNNING" || t.status === "DISPATCHED");
  const latest = new Map<string, AgentRun>();
  for (const r of runs) latest.set(r.agent_type, r);
  const items = [
    ...running.map((t) => ({
      key: t.task_id,
      name: t.agent_type.replace(/_/g, "-"),
      detail: `${t.key} · ${t.status.toLowerCase()}`,
      live: true,
    })),
    ...[...latest.values()]
      .reverse()
      .slice(0, 5)
      .map((r) => ({
        key: r.run_id,
        name: r.agent_type.replace(/_/g, "-"),
        detail: `${r.status} · ${r.attempts.map((a) => `${a.provider}/${a.model}`).join(", ") || "no model"}`,
        live: false,
      })),
  ];
  return (
    <section>
      <h2 className="ff-label mb-3">Agent status</h2>
      {items.length === 0 && <p className="text-sm text-slate-500">No agent has run yet.</p>}
      <ul className="flex flex-col gap-3">
        {items.map((i) => (
          <li key={i.key} className="ff-card px-3.5 py-3">
            <div className="flex items-center gap-2 font-mono text-[13px] text-slate-100">
              <span className={`size-1.5 rounded-full ${i.live ? "animate-pulse bg-sky-400" : "bg-emerald-400"}`} />
              {i.name}
            </div>
            <div className="mt-1 font-mono text-[11px] leading-relaxed break-words text-slate-500">{i.detail}</div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function RecentActivity({ detail, events }: { detail: WorkflowDetail; events: ReturnType<typeof describeEvents> }) {
  return (
    <section>
      <h2 className="ff-label mb-3">Activity</h2>
      <ol className="flex flex-col gap-4">
        {events.slice(-8).reverse().map((e) => (
          <li key={e.seq}>
            <time className="font-mono text-[11px] text-sky-400">{e.time}</time>
            <div className="mt-0.5 text-[13px] text-slate-200/85">{e.text}</div>
          </li>
        ))}
        {events.length === 0 && <li className="text-sm text-slate-500">Waiting for {detail.workflow.status.toLowerCase()} events…</li>}
      </ol>
    </section>
  );
}

function describeEvents(events: Parameters<typeof describe>[0][]) {
  return events.map((e) => ({
    seq: e.seq,
    time: new Date(e.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    text: describe(e),
  }));
}

export function WorkflowPage() {
  const { workflowId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  useWorkflowStream(workflowId);
  const tab: Tab = (TABS as readonly string[]).includes(params.get("tab") ?? "") ? (params.get("tab") as Tab) : "requirements";
  const [showAll, setShowAll] = useState(false);

  const detail = useQuery({ queryKey: ["workflow", workflowId], queryFn: () => api.getWorkflow(workflowId) });
  const events = useQuery({ queryKey: ["events", workflowId], queryFn: () => api.listEvents(workflowId) });
  const approvals = useQuery({
    queryKey: ["approvals", workflowId],
    queryFn: () => ext.approvals("pending", workflowId),
    refetchInterval: 5000,
  });
  const cancel = useMutation({ mutationFn: () => api.cancelWorkflow(workflowId), onSuccess: () => detail.refetch() });

  if (detail.isPending) return <p className="font-mono text-sm text-slate-500">Loading…</p>;
  if (detail.isError) return <p className="text-sm text-rose-400">{(detail.error as Error).message}</p>;

  const { workflow, specification, questions, agent_runs, tasks } = detail.data;
  const answered = questions.filter((q) => q.status === "answered");
  const stages = workflow.route_plan?.stages ?? [];
  const current = stages.findIndex((s) => s.status === "running");
  const doneStages = stages.filter((s) => s.status === "completed").length;
  const stageNo = current >= 0 ? current + 1 : Math.min(doneStages + 1, Math.max(stages.length, 1));
  const [title, ...rest] = workflow.request.split("\n");
  const description = specification?.goal ?? rest.join(" ").trim();
  const ci = tasks.filter((t) => t.kind === "ci" && t.result?.ci);
  const integrate = [...tasks].reverse().find((t) => t.kind === "integrate" && t.result);
  const lastCi = ci[ci.length - 1];
  const timeline = describeEvents(events.data ?? []);

  return (
    <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_17rem]">
      <div className="flex min-w-0 flex-col gap-6">
        <div>
          <div className="ff-label flex flex-wrap items-center gap-x-2">
            <Link to="/" className="hover:text-slate-300">
              Workflow
            </Link>
            {stages.length > 0 && (
              <span>
                · stage {String(stageNo).padStart(2, "0")} of {String(stages.length).padStart(2, "0")}
              </span>
            )}
            <span className="normal-case">· {workflow.workflow_id}</span>
            {workflow.repository_path && <span className="normal-case">· {workflow.repository_path}</span>}
          </div>
          <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <h1 className="max-w-3xl text-3xl leading-tight font-semibold break-words text-slate-100">{title}</h1>
            <div className="flex shrink-0 items-center gap-2 pt-1.5">
              <StatusBadge status={workflow.status} />
              {!TERMINAL.includes(workflow.status) && (
                <button onClick={() => cancel.mutate()} disabled={cancel.isPending} className="ff-btn">
                  Cancel
                </button>
              )}
            </div>
          </div>
          {description && <p className="mt-3 max-w-3xl text-base text-slate-500">{description}</p>}
        </div>

        {approvals.data?.map((a) => <ApprovalCard key={a.approval_id} approval={a} />)}
        {workflow.execution?.awaiting_decision && <DecisionPanel workflow={workflow} />}
        {workflow.error && !workflow.execution?.awaiting_decision && (
          <div className="rounded-[6px] border border-rose-400/40 bg-rose-400/10 p-4 text-sm text-rose-300">{workflow.error}</div>
        )}

        <nav className="flex flex-wrap border-b border-slate-800">
          {TABS.map((t) => (
            <button
              key={t}
              onClick={() => setParams({ tab: t })}
              className={`ff-tab capitalize ${tab === t ? "ff-tab-active" : ""}`}
            >
              {t === "ci" ? "CI" : t.charAt(0).toUpperCase() + t.slice(1)}
            </button>
          ))}
        </nav>

        {tab === "requirements" && (
          <div className="flex flex-col gap-4">
            {workflow.status === "PLANNING" && !specification && (
              <Card title="Requirement analysis">
                <p className="text-sm text-slate-500">
                  The Requirement Analyzer is inspecting the request{workflow.repository_path ? " and repository" : ""}…
                </p>
              </Card>
            )}
            {specification?.acceptance_criteria.map((ac) => (
              <div key={ac.id} className="ff-card px-4 py-3.5">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="ff-label font-medium">
                      {ac.id} · {ac.verification.replace(/_/g, " ")}
                    </div>
                    <div className="mt-1.5 text-[15px] font-medium text-slate-100">{ac.description}</div>
                  </div>
                  {criterionPill(ac.id, tasks)}
                </div>
              </div>
            ))}
            {(integrate || lastCi) && (
              <div className="grid gap-3 sm:grid-cols-2">
                {integrate?.result && (
                  <div className="ff-card px-4 py-3.5">
                    <div className="ff-label">Worktree · {integrate.result.branch?.split("/").pop() ?? "integration"}</div>
                    <div className="mt-2 font-mono text-[13px] text-slate-100">{integrate.result.files_changed.length} files changed</div>
                    <div className="mt-1 font-mono text-[11px] text-slate-500">
                      {integrate.result.checks.filter((c) => c.passed).length}/{integrate.result.checks.length} checks pass
                    </div>
                  </div>
                )}
                {lastCi?.result?.ci && (
                  <div className="ff-card px-4 py-3.5">
                    <div className="ff-label">CI · build #{lastCi.result.ci.build.build_number}</div>
                    <div
                      className={`mt-2 font-mono text-[13px] ${lastCi.result.ci.build.status === "SUCCESS" ? "text-emerald-400" : "text-rose-400"}`}
                    >
                      ● {lastCi.result.ci.build.status.toLowerCase()}
                    </div>
                    <div className="mt-1 font-mono text-[11px] text-slate-500">
                      {lastCi.result.ci.build.stages.map((s) => s.name.toLowerCase()).join(" · ")}
                    </div>
                  </div>
                )}
              </div>
            )}
            {workflow.route_plan && <RoutePlanView plan={workflow.route_plan} />}
            {specification && <SpecificationView spec={specification} />}
            {answered.length > 0 && (
              <Card title="Clarifications">
                <ul className="space-y-2 text-sm">
                  {answered.map((q) => {
                    const option = q.options.find((o) => o.id === q.answer?.selected_option);
                    return (
                      <li key={q.question_id}>
                        <div className="text-slate-500">{q.question}</div>
                        <div className="font-medium">{q.answer?.selected_option === "CUSTOM" ? q.answer.custom_text : option?.label}</div>
                      </li>
                    );
                  })}
                </ul>
              </Card>
            )}
          </div>
        )}

        {tab === "task graph" &&
          (workflow.execution || tasks.length > 0 ? (
            <DevelopmentView workflow={workflow} tasks={tasks} runs={agent_runs} />
          ) : (
            <p className="text-sm text-slate-500">No tasks yet - the task graph appears once execution starts.</p>
          ))}

        {tab === "changes" &&
          (workflow.report ? (
            <ReportView report={workflow.report} />
          ) : (
            <Card title="Changes">
              <p className="text-sm text-slate-500">
                The final report, pull request text and diff appear here when verification finishes. Use the task graph for
                per-task diffs meanwhile.
              </p>
            </Card>
          ))}

        {tab === "ci" && (
          <div className="flex flex-col gap-4">
            {ci.length === 0 && <p className="text-sm text-slate-500">No CI build has run for this workflow.</p>}
            {ci.map((t) => (
              <Card key={t.task_id} title={`${t.key} · round ${t.round}`}>
                {t.result && <VerdictLine result={t.result} />}
                {t.result?.ci && <CIView report={t.result.ci} />}
              </Card>
            ))}
          </div>
        )}

        {tab === "events" && (
          <div className="flex flex-col gap-4">
            <ActivityLog events={showAll ? events.data ?? [] : (events.data ?? []).slice(-100)} />
            {(events.data?.length ?? 0) > 100 && !showAll && (
              <button className="ff-btn w-fit" onClick={() => setShowAll(true)}>
                Show all {events.data?.length} events
              </button>
            )}
            <A2APanel workflowId={workflow.workflow_id} />
            <AgentRuns runs={agent_runs} />
          </div>
        )}
      </div>

      <aside className="flex min-w-0 flex-col gap-8 xl:border-l xl:border-slate-800 xl:pl-6">
        {(RUNNING.includes(workflow.status) || agent_runs.length > 0) && <AgentStatus runs={agent_runs} tasks={tasks} />}
        <RecentActivity detail={detail.data} events={timeline} />
      </aside>

      {workflow.status === "AWAITING_CLARIFICATION" && (
        <ClarificationDialog
          workflowId={workflow.workflow_id}
          questions={questions.filter((q) => q.round === workflow.clarification_round)}
        />
      )}
    </div>
  );
}
