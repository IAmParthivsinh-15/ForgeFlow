import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { ext, type Approval } from "../../lib/ext";
import { BUTTON, ErrorText, Panel, Pill, PRIMARY } from "./shared";

/** "May we perform this action?" (spec sections 30, 236, 237). */
export function ApprovalCard({ approval }: { approval: Approval }) {
  const qc = useQueryClient();
  const decide = useMutation({
    mutationFn: (approve: boolean) => ext.decide(approval.approval_id, approve),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["approvals"] });
      if (approval.workflow_id) qc.invalidateQueries({ queryKey: ["workflow", approval.workflow_id] });
    },
  });
  return (
    <div className="rounded-xl border border-amber-300 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-950/40">
      <div className="flex flex-wrap items-center gap-2 text-xs text-amber-900 dark:text-amber-200">
        <span className="font-semibold uppercase tracking-wide">Approval needed</span>
        <span>· {approval.capability_id}</span>
        <span>· risk {approval.risk}</span>
        <span>· requested by {approval.agent}</span>
      </div>
      <p className="mt-1 text-sm font-medium text-amber-950 dark:text-amber-100">{approval.summary}</p>
      {Object.keys(approval.details).length > 0 && (
        <details className="mt-1 text-xs">
          <summary className="cursor-pointer text-amber-800 dark:text-amber-300">Details</summary>
          <pre className="mt-1 overflow-auto rounded bg-white/60 p-2 dark:bg-slate-950/60">{JSON.stringify(approval.details, null, 2)}</pre>
        </details>
      )}
      <p className="mt-1 text-[11px] text-amber-800 dark:text-amber-300">Expires {new Date(approval.expires_at).toLocaleString()}</p>
      <div className="mt-2 flex gap-2">
        <button className={PRIMARY} disabled={decide.isPending} onClick={() => decide.mutate(true)}>
          Approve
        </button>
        <button className={BUTTON} disabled={decide.isPending} onClick={() => decide.mutate(false)}>
          Reject
        </button>
      </div>
      <ErrorText error={decide.error} />
    </div>
  );
}

export function ApprovalsTab() {
  const approvals = useQuery({ queryKey: ["approvals"], queryFn: () => ext.approvals(), refetchInterval: 5000 });
  const pending = approvals.data?.filter((a) => a.status === "pending") ?? [];
  const history = approvals.data?.filter((a) => a.status !== "pending") ?? [];
  return (
    <div className="flex flex-col gap-4">
      <Panel title="Pending">
        {pending.length === 0 && <p className="text-sm text-slate-500">Nothing is waiting for you.</p>}
        <div className="flex flex-col gap-3">
          {pending.map((a) => (
            <ApprovalCard key={a.approval_id} approval={a} />
          ))}
        </div>
      </Panel>
      <Panel title="History">
        <ul className="flex flex-col gap-2 text-sm">
          {history.map((a) => (
            <li key={a.approval_id} className="flex flex-wrap items-center gap-2">
              <Pill value={a.status} />
              <span>{a.summary}</span>
              {a.workflow_id && (
                <Link className="text-xs text-sky-700 underline dark:text-sky-400" to={`/workflows/${a.workflow_id}`}>
                  {a.workflow_id}
                </Link>
              )}
              {a.note && <span className="text-xs text-slate-500">“{a.note}”</span>}
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}

export function AuditTab() {
  const audit = useQuery({ queryKey: ["audit"], queryFn: () => ext.audit(), refetchInterval: 10000 });
  return (
    <Panel title="Capability audit log">
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="py-1 pr-3 font-medium">Time</th>
              <th className="py-1 pr-3 font-medium">Agent</th>
              <th className="py-1 pr-3 font-medium">Capability</th>
              <th className="py-1 pr-3 font-medium">Action</th>
              <th className="py-1 pr-3 font-medium">Approval</th>
              <th className="py-1 font-medium">Result</th>
            </tr>
          </thead>
          <tbody>
            {audit.data?.map((a) => (
              <tr key={a.audit_id} className="border-t border-slate-100 align-top dark:border-slate-800">
                <td className="py-1 pr-3 whitespace-nowrap">{new Date(a.timestamp).toLocaleString()}</td>
                <td className="py-1 pr-3">{a.agent}</td>
                <td className="py-1 pr-3">
                  <code>{a.capability_id}</code>
                  {a.skill_version && ` @${a.skill_version}`}
                </td>
                <td className="py-1 pr-3 break-all">{a.action}</td>
                <td className="py-1 pr-3">{a.approval.replace(/_/g, " ")}</td>
                <td className="py-1">
                  <Pill value={a.result} /> {a.latency_ms} ms
                  {a.error && <div className="text-rose-600">{a.error}</div>}
                  {a.workflow_id && (
                    <Link className="block text-sky-700 underline dark:text-sky-400" to={`/workflows/${a.workflow_id}`}>
                      {a.workflow_id}
                    </Link>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {audit.data?.length === 0 && <p className="text-sm text-slate-500">No capability use recorded yet.</p>}
      </div>
    </Panel>
  );
}
