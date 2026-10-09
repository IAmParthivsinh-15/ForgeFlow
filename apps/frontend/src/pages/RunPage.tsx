import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ErrorText } from "../components/ext/shared";
import { autonomy } from "../lib/ext";
import { RunPill } from "./AutonomyPage";

const TABS = ["plan", "evidence", "closure", "escalation"] as const;
type Tab = (typeof TABS)[number];
const LIVE = ["RECEIVED", "PLANNING", "PLANNED", "EXECUTING", "VERIFYING", "RETRYING"];

/** One run's evidence portfolio (additional.md section 10) and its stop/resume controls. */
export function RunPage() {
  const { traceId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const tab: Tab = (TABS as readonly string[]).includes(params.get("tab") ?? "") ? (params.get("tab") as Tab) : "plan";
  const run = useQuery({ queryKey: ["run", traceId], queryFn: () => autonomy.run(traceId), refetchInterval: 3000 });
  const events = useQuery({ queryKey: ["run-events", traceId], queryFn: () => autonomy.events(traceId), refetchInterval: 3000 });
  const [reason, setReason] = useState("");
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["run", traceId] });
    qc.invalidateQueries({ queryKey: ["run-events", traceId] });
  };
  const stop = useMutation({ mutationFn: () => autonomy.stop(traceId, reason), onSuccess: refresh });
  const resume = useMutation({ mutationFn: () => autonomy.resume(traceId), onSuccess: refresh });
  const rerun = useMutation({ mutationFn: () => autonomy.rerun(traceId), onSuccess: (r) => navigate(`/autonomy/runs/${r.trace_id}`) });

  if (run.isPending) return <p className="font-mono text-sm text-slate-500">Loading…</p>;
  if (run.isError) return <p className="text-sm text-rose-400">{(run.error as Error).message}</p>;
  const r = run.data;
  const plan = r.plan;
  const c = r.counters;

  return (
    <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_18rem]">
      <div className="flex min-w-0 flex-col gap-6">
        <div>
          <div className="ff-label flex flex-wrap gap-x-2">
            <Link to="/autonomy" className="hover:text-slate-300">
              Run
            </Link>
            <span className="normal-case">· {r.trace_id}</span>
            <span>· {r.contract_version}</span>
            <span>· {r.action_profile_id}</span>
          </div>
          <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <h1 className="max-w-3xl text-3xl leading-tight font-semibold text-slate-100">{r.source_item.title}</h1>
            <div className="pt-1.5">
              <RunPill status={r.status} />
            </div>
          </div>
          <p className="mt-3 text-base text-slate-500">
            <a href={r.source_item.url} target="_blank" rel="noreferrer" className="hover:text-slate-300">
              {r.source_item.repository}#{r.source_item.number}
            </a>{" "}
            · {r.trigger} trigger · {r.mode} mode
            {r.decision && <> · decision <b className="text-slate-300">{r.decision}</b></>}
            {r.decision_reason && <> - {r.decision_reason}</>}
          </p>
        </div>

        <nav className="flex flex-wrap border-b border-slate-800">
          {TABS.map((t) => (
            <button key={t} onClick={() => setParams({ tab: t })} className={`ff-tab ${tab === t ? "ff-tab-active" : ""}`}>
              {t === "plan" ? "task_plan.json" : t.charAt(0).toUpperCase() + t.slice(1)}
            </button>
          ))}
        </nav>

        {tab === "plan" &&
          (plan ? (
            <div className="flex flex-col gap-4">
              <div className="ff-card p-4 text-sm">
                <div className="ff-label">Objective · {plan.status}</div>
                <p className="mt-2 text-slate-200">{plan.objective}</p>
                <p className="mt-2 font-mono text-[11px] text-slate-500">
                  workers {plan.worker_count_requested}/{plan.worker_count_max} · implementer {plan.implementer_model} · reviewer{" "}
                  {plan.reviewer_model} · est. ${plan.cost_estimate_usd} · {plan.hash.slice(0, 23)}…
                </p>
                <p className="mt-1 text-xs text-slate-500">{plan.parallelism_note}</p>
              </div>
              <div className="ff-card overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="ff-label">
                    <tr className="border-b border-slate-800">
                      <th className="px-4 py-2 font-normal">Step</th>
                      <th className="px-4 py-2 font-normal">Role</th>
                      <th className="px-4 py-2 font-normal">Worker</th>
                      <th className="px-4 py-2 font-normal">Model</th>
                      <th className="px-4 py-2 font-normal">Depends on</th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.subtasks.map((s) => (
                      <tr key={s.task_id} className="border-b border-slate-800/60 align-top last:border-0">
                        <td className="px-4 py-2">
                          <div className="font-mono text-[13px] text-slate-100">{s.task_id}</div>
                          <div className="text-xs text-slate-500">{s.purpose}</div>
                        </td>
                        <td className="px-4 py-2 font-mono text-xs text-slate-400">
                          {s.role}
                          {s.parallel_group && <div className="text-sky-400">∥ {s.parallel_group}</div>}
                        </td>
                        <td className="px-4 py-2 font-mono text-xs text-slate-400">{s.worker_role}</td>
                        <td className="px-4 py-2 font-mono text-xs text-slate-400">{s.model}</td>
                        <td className="px-4 py-2 font-mono text-xs text-slate-500">{s.dependencies.join(", ") || "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                <div className="ff-card p-4">
                  <div className="ff-label">Planned actions (all AUTO in the profile)</div>
                  <ul className="mt-2 font-mono text-xs text-slate-300">
                    {plan.planned_actions.map((a) => (
                      <li key={a}>{a}</li>
                    ))}
                  </ul>
                </div>
                <div className="ff-card p-4">
                  <div className="ff-label">Budgets · closure checks</div>
                  <p className="mt-2 font-mono text-xs text-slate-300">{JSON.stringify(plan.budgets)}</p>
                  <ul className="mt-2 font-mono text-xs text-slate-400">
                    {plan.closure_checks.map((x) => (
                      <li key={x}>✓ {x}</li>
                    ))}
                  </ul>
                </div>
              </div>
              <details className="ff-card p-4">
                <summary className="ff-label cursor-pointer">Raw task_plan.json</summary>
                <pre className="mt-3 max-h-96 overflow-auto font-mono text-[11px] text-slate-400">{JSON.stringify(plan, null, 2)}</pre>
              </details>
            </div>
          ) : (
            <p className="text-sm text-slate-500">No plan saved yet - workers cannot start before it is.</p>
          ))}

        {tab === "evidence" && (
          <ol className="ff-card divide-y divide-slate-800">
            {events.data?.map((e) => (
              <li key={e.seq} className="px-4 py-2.5">
                <div className="flex flex-wrap items-baseline gap-3 font-mono text-[11px]">
                  <span className="text-slate-600">{String(e.seq).padStart(3, "0")}</span>
                  <span className="text-sky-400">{new Date(e.timestamp).toLocaleTimeString()}</span>
                  <span className="text-slate-100">{e.type}</span>
                  <span className="text-slate-500">{e.actor}</span>
                </div>
                {Object.keys(e.data).length > 0 && (
                  <pre className="mt-1 max-h-40 overflow-auto font-mono text-[11px] whitespace-pre-wrap text-slate-500">
                    {JSON.stringify(e.data, null, 1).slice(0, 1500)}
                  </pre>
                )}
              </li>
            ))}
          </ol>
        )}

        {tab === "closure" && (
          <div className="flex flex-col gap-2">
            {r.closure.length === 0 && <p className="text-sm text-slate-500">Closure has not been verified yet.</p>}
            {r.closure.map((check) => (
              <div key={check.name} className="ff-card flex items-start justify-between gap-3 px-4 py-3">
                <div>
                  <div className="font-mono text-[13px] text-slate-100">{check.name}</div>
                  <div className="mt-1 text-xs text-slate-500">{check.detail}</div>
                </div>
                <span className={check.passed ? "ff-pill-green" : "ff-pill-red"}>{check.passed ? "passed" : "failed"}</span>
              </div>
            ))}
          </div>
        )}

        {tab === "escalation" &&
          (r.escalation ? (
            <div className="rounded-[6px] border border-amber-400/40 bg-amber-400/10 p-5 text-sm">
              <div className="ff-label text-amber-300">
                {r.escalation.condition} · {new Date(r.escalation.escalated_at).toLocaleString()}
              </div>
              <p className="mt-2 text-xs text-amber-200/80">{r.escalation.rule}</p>
              <p className="mt-3 text-slate-200">{r.escalation.summary}</p>
              <p className="mt-3 font-medium text-amber-200">Decision needed: {r.escalation.decision_needed}</p>
            </div>
          ) : (
            <p className="text-sm text-slate-500">This run has not escalated.</p>
          ))}
      </div>

      <aside className="flex flex-col gap-6 xl:border-l xl:border-slate-800 xl:pl-6">
        <section>
          <h2 className="ff-label mb-3">Counters</h2>
          <dl className="ff-card grid grid-cols-2 gap-x-3 gap-y-2 px-4 py-3 font-mono text-[11px]">
            {[
              ["workers ∥", `${c.max_parallel_observed}/${r.limits.max_workers}`],
              ["spawned", c.workers_spawned],
              ["retries", `${c.retries}/${r.limits.budgets.max_retries}`],
              ["failed", c.failed_tasks],
              ["tokens", c.tokens],
              ["cost", `$${c.cost_usd.toFixed(3)}`],
              ["runtime", `${Math.round(c.runtime_seconds)}s`],
              ["comments", c.comments_posted],
            ].map(([k, v]) => (
              <div key={k as string}>
                <dt className="text-slate-500">{k}</dt>
                <dd className="text-slate-100">{v}</dd>
              </div>
            ))}
          </dl>
        </section>
        {r.workflow_id && (
          <Link to={`/workflows/${r.workflow_id}`} className="ff-btn w-fit">
            Open workflow {r.workflow_id}
          </Link>
        )}
        <section>
          <h2 className="ff-label mb-3">Controls</h2>
          {LIVE.includes(r.status) && (
            <form
              className="flex flex-col gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                stop.mutate();
              }}
            >
              <input className="ff-input" placeholder="Reason (required)" value={reason} onChange={(e) => setReason(e.target.value)} />
              <button className="ff-btn-danger justify-center py-2 text-sm" disabled={reason.trim().length < 3 || stop.isPending}>
                ■ Emergency stop
              </button>
              <p className="font-mono text-[11px] text-slate-500">CLI: forgeflow run stop {r.trace_id} --reason "…"</p>
            </form>
          )}
          {["STOPPED", "PAUSED_BY_GUARDRAIL"].includes(r.status) && (
            <button className="ff-btn-primary" onClick={() => resume.mutate()} disabled={resume.isPending}>
              Resume (re-reads GitHub first)
            </button>
          )}
          {["STOPPED", "RESOLVED", "ESCALATED", "CLOSED_NO_ACTION"].includes(r.status) && (
            <button className="ff-btn mt-2" onClick={() => rerun.mutate()} disabled={rerun.isPending}>
              Start a new run for this issue
            </button>
          )}
          {r.stop && (
            <p className="mt-3 font-mono text-[11px] text-slate-500">
              stopped by {r.stop.actor}: {r.stop.reason} · {r.stop.cancelled_tasks.length} task(s) halted
            </p>
          )}
          <ErrorText error={stop.error ?? resume.error ?? rerun.error} />
        </section>
      </aside>
    </div>
  );
}
