import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { ErrorText } from "../components/ext/shared";
import { autonomy, type AutonomyRun, type RunStatus } from "../lib/ext";

export const RUN_PILL: Record<RunStatus, string> = {
  RECEIVED: "ff-pill-gray",
  PLANNING: "ff-pill-blue",
  PLANNED: "ff-pill-blue",
  EXECUTING: "ff-pill-blue",
  VERIFYING: "ff-pill-blue",
  RETRYING: "ff-pill-amber",
  PAUSED_BY_GUARDRAIL: "ff-pill-red",
  STOPPING: "ff-pill-red",
  STOPPED: "ff-pill-red",
  RESOLVED: "ff-pill-green",
  CLOSED_NO_ACTION: "ff-pill-gray",
  ESCALATED: "ff-pill-amber",
};

export function RunPill({ status }: { status: RunStatus }) {
  return <span className={RUN_PILL[status]}>{status.replace(/_/g, " ").toLowerCase()}</span>;
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="ff-card px-4 py-3.5">
      <div className="ff-label">{label}</div>
      <div className="mt-2 font-mono text-[15px] text-slate-100">{value}</div>
      {sub && <div className="mt-1 font-mono text-[11px] break-words text-slate-500">{sub}</div>}
    </div>
  );
}

function RunRow({ run }: { run: AutonomyRun }) {
  const passed = run.closure.filter((c) => c.passed).length;
  return (
    <Link
      to={`/autonomy/runs/${run.trace_id}`}
      className="ff-card flex flex-col gap-2 px-4 py-3 hover:border-slate-700 md:flex-row md:items-center md:justify-between"
    >
      <div className="min-w-0">
        <div className="ff-label normal-case">
          {run.trace_id} · {run.source_item.repository}#{run.source_item.number} · {run.trigger} · {run.mode}
        </div>
        <div className="mt-1 truncate text-[15px] font-medium text-slate-100">{run.source_item.title}</div>
        {(run.escalation || run.decision_reason) && (
          <div className="mt-1 truncate text-xs text-slate-500">
            {run.escalation ? `[${run.escalation.condition}] ${run.escalation.summary}` : run.decision_reason}
          </div>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-3 font-mono text-[11px] text-slate-500">
        <span>workers {run.counters.max_parallel_observed}</span>
        {run.closure.length > 0 && <span>closure {passed}/{run.closure.length}</span>}
        <RunPill status={run.status} />
      </div>
    </Link>
  );
}

/** L4 autonomy (additional.md): the work queue, its contract and what needs a human. */
export function AutonomyPage() {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["autonomy-status"], queryFn: autonomy.status, refetchInterval: 5000 });
  const runs = useQuery({ queryKey: ["autonomy-runs"], queryFn: () => autonomy.runs(), refetchInterval: 5000 });
  const alerts = useQuery({ queryKey: ["autonomy-alerts"], queryFn: autonomy.alerts, refetchInterval: 10000 });
  const review = useQuery({ queryKey: ["autonomy-review"], queryFn: () => autonomy.review(5), refetchInterval: 20000 });
  const [issue, setIssue] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["autonomy-runs"] });
  const intake = useMutation({ mutationFn: () => autonomy.intake(Number(issue)), onSuccess: refresh });
  const sweep = useMutation({ mutationFn: autonomy.sweep, onSuccess: refresh });
  const ack = useMutation({
    mutationFn: autonomy.ack,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["autonomy-alerts"] }),
  });
  const s = status.data;
  const triage = runs.data?.filter((r) => ["ESCALATED", "PAUSED_BY_GUARDRAIL", "STOPPED"].includes(r.status)) ?? [];

  return (
    <div className="flex flex-col gap-8">
      <div>
        <div className="ff-label">
          Autonomy · {s?.contract_version ?? "contract"} · {s?.mode ?? "…"} mode
        </div>
        <h1 className="mt-3 text-3xl font-semibold text-slate-100">Autonomous work queue</h1>
        {s && <p className="mt-2 max-w-4xl text-base text-slate-500">{s.responsibility}</p>}
        <ErrorText error={status.error} />
      </div>

      {s && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Stat
            label="Source"
            value={s.source.repository || "not configured"}
            sub={`${s.source_ready ? "service identity ready" : "set source.connector_id"} · webhook secret ${s.webhook_secret_configured ? "set" : "missing"} · sweep ${s.source.sweep_interval_seconds}s`}
          />
          <Stat
            label="Guardrails"
            value={`≤ ${s.guardrails.max_workers} workers`}
            sub={`tokens ${s.guardrails.budgets.max_tokens} · $${s.guardrails.budgets.max_cost_usd} · ${s.guardrails.budgets.max_runtime_seconds}s · ${s.guardrails.budgets.max_retries} retries`}
          />
          <Stat label="Models" value={`${s.guardrails.allowed_models.length} allowed`} sub={s.guardrails.allowed_models.join(", ")} />
          <Stat
            label="Queue"
            value={`${Object.values(s.active).reduce((a, b) => a + b, 0)} active`}
            sub={`${s.open_alerts} open alerts · ${Object.entries(s.active).map(([k, v]) => `${k.toLowerCase()} ${v}`).join(", ") || "idle"}`}
          />
        </div>
      )}

      <div className="flex flex-wrap items-end gap-2">
        <form
          className="flex items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            intake.mutate();
          }}
        >
          <label className="flex flex-col gap-1">
            <span className="ff-label">Trigger issue #</span>
            <input className="ff-input w-28 font-mono" value={issue} onChange={(e) => setIssue(e.target.value.replace(/\D/g, ""))} />
          </label>
          <button className="ff-btn" disabled={!issue || intake.isPending}>
            Intake
          </button>
        </form>
        <button className="ff-btn" onClick={() => sweep.mutate()} disabled={sweep.isPending}>
          Run sweep now
        </button>
        <ErrorText error={intake.error ?? sweep.error} />
        {sweep.data && <span className="font-mono text-xs text-slate-500">{JSON.stringify(sweep.data)}</span>}
      </div>

      {(triage.length > 0 || (alerts.data?.length ?? 0) > 0) && (
        <section>
          <h2 className="ff-label mb-3">Triage · needs a human</h2>
          <div className="flex flex-col gap-2">
            {triage.map((r) => (
              <Link
                key={r.trace_id}
                to={`/autonomy/runs/${r.trace_id}`}
                className="rounded-[6px] border border-amber-400/40 bg-amber-400/10 px-4 py-3 text-sm hover:border-amber-400/70"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <RunPill status={r.status} />
                  <span className="font-mono text-[11px] text-amber-300">{r.escalation?.condition ?? r.guardrail ?? r.stop?.reason}</span>
                  <span className="font-mono text-[11px] text-slate-500">{r.trace_id}</span>
                </div>
                <div className="mt-1 text-slate-200">{r.escalation?.summary ?? r.decision_reason}</div>
                {r.escalation?.decision_needed && (
                  <div className="mt-1 text-xs text-amber-200">Decision needed: {r.escalation.decision_needed}</div>
                )}
              </Link>
            ))}
            {alerts.data?.map((a) => (
              <div key={a.alert_id} className="ff-card flex items-start justify-between gap-3 px-4 py-2.5">
                <div className="min-w-0 text-sm">
                  <span className={a.severity === "critical" ? "ff-pill-red" : "ff-pill-amber"}>{a.kind}</span>{" "}
                  <span className="text-slate-300">{a.summary}</span>
                  <div className="font-mono text-[11px] text-slate-500">{new Date(a.raised_at).toLocaleString()}</div>
                </div>
                <button className="ff-btn" onClick={() => ack.mutate(a.alert_id)}>
                  Acknowledge
                </button>
              </div>
            ))}
          </div>
        </section>
      )}

      <section>
        <h2 className="ff-label mb-3">Runs</h2>
        {runs.data?.length === 0 && (
          <p className="text-sm text-slate-500">
            No runs yet. Label an issue <code>forgeflow</code> + <code>bug</code> in the configured repository, or trigger one above.
          </p>
        )}
        <div className="flex flex-col gap-2">
          {runs.data?.map((r) => (
            <RunRow key={r.trace_id} run={r} />
          ))}
        </div>
      </section>

      {(review.data?.length ?? 0) > 0 && (
        <section>
          <h2 className="ff-label mb-3">Last five runs · human dependency review</h2>
          <div className="ff-card divide-y divide-slate-800">
            {review.data?.map((row) => (
              <div key={row.trace_id} className="px-4 py-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[11px] text-slate-500">{row.trace_id}</span>
                  <span className="text-slate-200">{row.source}</span>
                  <span className={row.autonomous_without_intervention ? "ff-pill-green" : "ff-pill-gray"}>
                    {row.autonomous_without_intervention ? "no human needed" : row.status.toLowerCase()}
                  </span>
                  {row.avoidable_dependencies.length > 0 && <span className="ff-pill-red">avoidable dependency</span>}
                </div>
                {row.interventions.length > 0 && (
                  <ul className="mt-1 font-mono text-[11px] text-slate-500">
                    {row.interventions.map((i, n) => (
                      <li key={n}>
                        {i.kind}: {i.classification}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
