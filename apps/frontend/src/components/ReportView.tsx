import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type FinalReport, type Workflow } from "../lib/api";
import { Card } from "./SpecificationView";
import { VERDICT_STYLE } from "./VerificationViews";

const OUTCOME = {
  passed: { label: "Passed", style: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300" },
  passed_with_accepted_risks: {
    label: "Passed with accepted risks",
    style: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-300",
  },
  failed: { label: "Findings open", style: "bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300" },
  completed: { label: "Completed", style: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300" },
};

/** Final engineering report (spec sections 50, 58) with a ready-to-paste PR description. */
export function ReportView({ report }: { report: FinalReport }) {
  const [copied, setCopied] = useState<string | null>(null);
  const copy = async (label: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(label);
      setTimeout(() => setCopied(null), 1500);
    } catch {
      setCopied("Copy failed - select the text manually");
    }
  };
  const outcome = OUTCOME[report.outcome];
  return (
    <Card title="Final report" aside={<span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${outcome.style}`}>{outcome.label}</span>}>
      <p className="text-sm">{report.summary}</p>
      <ul className="mt-3 grid gap-1.5 text-sm sm:grid-cols-2">
        {report.stages.map((s) => (
          <li key={s.stage} className="flex items-baseline gap-2">
            <span className={`font-medium ${VERDICT_STYLE[s.verdict]}`}>{s.verdict}</span>
            <span>{s.stage}</span>
            {s.round > 1 && <span className="text-xs text-slate-500">round {s.round}</span>}
          </li>
        ))}
      </ul>
      {report.open_findings.length > 0 && (
        <div className="mt-3">
          <h4 className="text-xs font-medium text-slate-500">Open findings</h4>
          <ul className="list-disc pl-5 text-sm text-rose-700 dark:text-rose-300">
            {report.open_findings.map((f, i) => (
              <li key={i}>{f}</li>
            ))}
          </ul>
        </div>
      )}
      {report.accepted_risks.length > 0 && (
        <div className="mt-3">
          <h4 className="text-xs font-medium text-slate-500">Accepted risks</h4>
          <ul className="list-disc pl-5 text-sm text-amber-800 dark:text-amber-300">
            {report.accepted_risks.map((f, i) => (
              <li key={i}>{f}</li>
            ))}
          </ul>
        </div>
      )}
      <div className="mt-4 flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h4 className="text-sm font-medium">Pull request</h4>
          <div className="flex items-center gap-2">
            {copied && <span className="text-xs text-slate-500">{copied === "title" || copied === "body" ? "Copied" : copied}</span>}
            <button onClick={() => copy("title", report.pr_title)} className={BUTTON}>
              Copy title
            </button>
            <button onClick={() => copy("body", report.pr_body)} className={BUTTON}>
              Copy description
            </button>
          </div>
        </div>
        <p className="font-mono text-sm">{report.pr_title}</p>
        {report.pull_request_url && (
          <a
            href={report.pull_request_url}
            target="_blank"
            rel="noreferrer"
            className="w-fit rounded-lg bg-emerald-600 px-3 py-1 text-xs font-medium text-white hover:bg-emerald-700"
          >
            Open the pull request on GitHub
          </a>
        )}
        {report.branch && (
          <p className="text-xs text-slate-500">
            Branch <code>{report.branch}</code>
            {report.commit && (
              <>
                {" "}
                @ <code>{report.commit.slice(0, 10)}</code>
              </>
            )}{" "}
            {report.pull_request_url ? "" : " · No GitHub binding: create the PR from this branch, or bind the project under Extensibility → Projects."}
          </p>
        )}
        <details>
          <summary className="cursor-pointer text-xs text-slate-500">PR description (Markdown)</summary>
          <pre className="mt-1 max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-xs dark:bg-slate-950">{report.pr_body}</pre>
        </details>
      </div>
    </Card>
  );
}

/** Repair limit reached: the human decides (spec section 57, WAITING_FOR_HUMAN). */
export function DecisionPanel({ workflow }: { workflow: Workflow }) {
  const queryClient = useQueryClient();
  const decide = useMutation({
    mutationFn: (action: "accept" | "repair") => api.decide(workflow.workflow_id, action),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["workflow", workflow.workflow_id] }),
  });
  return (
    <section className="rounded-2xl border border-amber-300 bg-amber-50 p-5 dark:border-amber-800 dark:bg-amber-950/40">
      <h2 className="text-sm font-semibold text-amber-900 dark:text-amber-200">Your decision is needed</h2>
      <p className="mt-1 text-sm text-amber-900 dark:text-amber-200">{workflow.error}</p>
      <p className="mt-2 text-xs text-amber-800 dark:text-amber-300">
        ForgeFlow made {workflow.execution?.repair_attempts} automatic repair attempt(s). Accept to complete with these findings
        recorded as accepted risks, or allow one more repair round.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <button onClick={() => decide.mutate("repair")} disabled={decide.isPending} className={PRIMARY}>
          Try one more repair
        </button>
        <button onClick={() => decide.mutate("accept")} disabled={decide.isPending} className={BUTTON}>
          Accept risks and complete
        </button>
      </div>
      {decide.isError && <p className="mt-2 text-sm text-rose-600">{(decide.error as Error).message}</p>}
    </section>
  );
}

/** Recorded agent-to-agent exchanges (spec section 186). */
export function A2APanel({ workflowId }: { workflowId: string }) {
  const messages = useQuery({ queryKey: ["a2a", workflowId], queryFn: () => api.listA2A(workflowId) });
  if (!messages.data?.length) return null;
  return (
    <Card title="Agent-to-agent">
      <ul className="space-y-3 text-sm">
        {messages.data.map((m) => (
          <li key={m.message_id}>
            <div className="text-xs text-slate-500">
              {m.sender} → {m.receiver} · {m.status}
            </div>
            <div className="font-medium">{m.request}</div>
            {m.response && <div className="mt-0.5 text-slate-600 dark:text-slate-300">{m.response}</div>}
          </li>
        ))}
      </ul>
    </Card>
  );
}

const BUTTON =
  "rounded-lg border border-slate-300 bg-white px-3 py-1 text-xs hover:bg-slate-100 disabled:opacity-40 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800";
const PRIMARY =
  "rounded-lg bg-[var(--ff-accent)] px-3 py-1 text-xs font-medium text-white hover:bg-[var(--ff-accent-hover)] disabled:opacity-40 ";
