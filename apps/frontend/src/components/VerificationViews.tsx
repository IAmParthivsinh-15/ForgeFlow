import type { CIReport, QAReport, ReviewReport, SecurityReport, Severity, TaskResult, Verdict } from "../lib/api";
import { artifactUrl } from "../lib/ext";

const SEVERITY_STYLE: Record<Severity, string> = {
  critical: "bg-rose-600 text-white",
  high: "bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300",
  medium: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-300",
  low: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
  info: "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
};

export const VERDICT_STYLE: Record<Verdict | "not_run", string> = {
  pass: "text-emerald-700 dark:text-emerald-400",
  fail: "text-rose-700 dark:text-rose-400",
  uncertain: "text-amber-700 dark:text-amber-400",
  not_run: "text-slate-500",
};

export function SeverityTag({ severity }: { severity: Severity }) {
  return <span className={`rounded px-1.5 py-0.5 text-[11px] font-semibold uppercase ${SEVERITY_STYLE[severity]}`}>{severity}</span>;
}

function Location({ file, line }: { file: string | null; line: number | null }) {
  if (!file) return null;
  return (
    <code className="break-all text-xs text-slate-500">
      {file}
      {line ? `:${line}` : ""}
    </code>
  );
}

export function VerdictLine({ result }: { result: TaskResult }) {
  if (!result.verdict) return null;
  return (
    <div className="flex flex-col gap-1">
      <p className={`text-sm font-medium ${VERDICT_STYLE[result.verdict]}`}>
        Verdict: {result.verdict}
        {result.blocking && " · blocking"}
      </p>
      {result.blocking_reasons.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-xs text-rose-700 dark:text-rose-300">
          {result.blocking_reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ReviewView({ report }: { report: ReviewReport }) {
  const decision = {
    approved: "text-emerald-700 dark:text-emerald-400",
    changes_requested: "text-amber-700 dark:text-amber-400",
    blocked: "text-rose-700 dark:text-rose-400",
  }[report.decision];
  return (
    <div className="flex flex-col gap-3 text-sm">
      <p>
        <span className={`font-semibold ${decision}`}>{report.decision.replace("_", " ")}</span> — {report.summary}
      </p>
      {report.requirements_alignment && <p className="text-slate-600 dark:text-slate-300">{report.requirements_alignment}</p>}
      {report.findings.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {report.findings.map((f, i) => (
            <li key={i} className="rounded-lg border border-slate-200 p-2 dark:border-slate-800">
              <div className="flex flex-wrap items-center gap-2">
                <SeverityTag severity={f.severity} />
                <span className="text-xs text-slate-500">{f.category.replace("_", " ")}</span>
                <Location file={f.file} line={f.line} />
              </div>
              <p className="mt-1">{f.message}</p>
              {f.suggestion && <p className="mt-1 text-xs text-slate-500">Suggestion: {f.suggestion}</p>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-slate-500">No findings.</p>
      )}
    </div>
  );
}

const OWASP_STYLE = {
  pass: "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
  fail: "border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-200",
  uncertain: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200",
  not_applicable: "border-slate-200 bg-slate-50 text-slate-500 dark:border-slate-800 dark:bg-slate-900",
};

export function SecurityView({ report }: { report: SecurityReport }) {
  return (
    <div className="flex flex-col gap-3 text-sm">
      <p>{report.summary}</p>
      <div>
        <h5 className="mb-1 text-xs font-medium text-slate-500">OWASP Top 10 ({report.owasp_edition})</h5>
        <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-5">
          {[...report.categories]
            .sort((a, b) => a.id.localeCompare(b.id))
            .map((c) => (
              <div key={c.id} title={c.notes} className={`rounded-lg border px-2 py-1 text-xs ${OWASP_STYLE[c.status]}`}>
                <div className="font-semibold">{c.id}</div>
                <div>{c.status.replace("_", " ")}</div>
              </div>
            ))}
        </div>
      </div>
      <div className="flex flex-wrap gap-2 text-xs">
        {report.scanners.map((s) => (
          <span
            key={s.tool}
            title={s.detail}
            className={`rounded-full border px-2 py-0.5 ${
              s.status === "completed"
                ? "border-emerald-300 text-emerald-800 dark:border-emerald-800 dark:text-emerald-300"
                : s.status === "skipped"
                  ? "border-slate-300 text-slate-500 dark:border-slate-700"
                  : "border-amber-300 text-amber-800 dark:border-amber-800 dark:text-amber-300"
            }`}
          >
            {s.tool}: {s.status} · {s.findings.length} finding(s)
          </span>
        ))}
      </div>
      {report.findings.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {report.findings.map((f, i) => (
            <li key={i} className="rounded-lg border border-slate-200 p-2 dark:border-slate-800">
              <div className="flex flex-wrap items-center gap-2">
                <SeverityTag severity={f.severity} />
                <span className="text-xs font-medium">{f.category}</span>
                <span className="text-xs text-slate-500">via {f.source}</span>
                <Location file={f.file} line={f.line} />
              </div>
              <p className="mt-1">{f.impact}</p>
              {f.evidence && <pre className="mt-1 overflow-auto rounded bg-slate-50 p-1.5 text-xs dark:bg-slate-950">{f.evidence}</pre>}
              <p className="mt-1 text-xs text-slate-500">Remediation: {f.remediation}</p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-slate-500">No findings.</p>
      )}
      {report.false_positives.length > 0 && (
        <p className="text-xs text-slate-500">Dismissed as false positives: {report.false_positives.join("; ")}</p>
      )}
    </div>
  );
}

const AC_STYLE = {
  PASS: "text-emerald-700 dark:text-emerald-400",
  FAIL: "text-rose-700 dark:text-rose-400",
  UNCERTAIN: "text-amber-700 dark:text-amber-400",
};

export function QAView({ report }: { report: QAReport }) {
  return (
    <div className="flex flex-col gap-3 text-sm">
      <p>{report.summary}</p>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-slate-500">
            <tr>
              <th className="py-1 pr-3 font-medium">Criterion</th>
              <th className="py-1 pr-3 font-medium">Result</th>
              <th className="py-1 font-medium">Evidence</th>
            </tr>
          </thead>
          <tbody>
            {report.criteria.map((c) => (
              <tr key={c.id} className="border-t border-slate-100 align-top dark:border-slate-800">
                <td className="py-1.5 pr-3 font-mono text-xs">{c.id}</td>
                <td className={`py-1.5 pr-3 font-semibold ${AC_STYLE[c.status]}`}>{c.status}</td>
                <td className="py-1.5 text-xs text-slate-600 dark:text-slate-300">
                  {c.evidence}
                  {c.checks.length > 0 && <div className="mt-0.5 font-mono text-slate-500">{c.checks.join(", ")}</div>}
                  {(c.artifacts ?? []).length > 0 && <Screenshots ids={c.artifacts ?? []} />}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(report.browser_url || report.browser_note) && (
        <p className="text-xs text-slate-500">
          Browser (Playwright MCP):{" "}
          {report.browser_url ? (
            <>
              served the commit at <code>{report.browser_url}</code> · {report.browser_actions ?? 0} browser action(s)
            </>
          ) : (
            <span className="text-amber-700 dark:text-amber-400">{report.browser_note}</span>
          )}
        </p>
      )}
      {report.downgraded.length > 0 && (
        <p className="text-xs text-amber-700 dark:text-amber-400">
          Downgraded to UNCERTAIN for lack of executed evidence: {report.downgraded.join(", ")}
        </p>
      )}
      {report.gaps.length > 0 && (
        <div>
          <h5 className="text-xs font-medium text-slate-500">Test gaps</h5>
          <ul className="list-disc pl-5 text-xs">
            {report.gaps.map((g, i) => (
              <li key={i}>{g}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/** Evidence screenshots captured through Playwright MCP (stored as artifacts). */
export function Screenshots({ ids }: { ids: string[] }) {
  return (
    <div className="mt-1.5 flex flex-wrap gap-2">
      {ids.map((id) => (
        <a key={id} href={artifactUrl(id)} target="_blank" rel="noreferrer" title={`Evidence ${id}`}>
          <img
            src={artifactUrl(id)}
            alt={`Screenshot evidence ${id}`}
            loading="lazy"
            className="h-20 w-32 rounded border border-slate-200 object-cover object-top dark:border-slate-700"
          />
        </a>
      ))}
    </div>
  );
}

export function CIView({ report }: { report: CIReport }) {
  const { build, analysis } = report;
  const ok = build.status === "SUCCESS";
  return (
    <div className="flex flex-col gap-3 text-sm">
      <p>
        <span className={`font-semibold ${ok ? "text-emerald-700 dark:text-emerald-400" : "text-rose-700 dark:text-rose-400"}`}>
          {build.status}
        </span>{" "}
        · {build.provider} {build.job} #{build.build_number} · {(build.duration_ms / 1000).toFixed(1)}s ·{" "}
        <code className="text-xs">{build.commit.slice(0, 10)}</code>
        {build.url && (
          <>
            {" "}
            ·{" "}
            <a href={build.url} target="_blank" rel="noreferrer" className="text-sky-700 underline dark:text-sky-400">
              open in Jenkins
            </a>
          </>
        )}
      </p>
      {build.stages.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {build.stages.map((s) => (
            <span
              key={s.name}
              className={`rounded-md border px-2 py-0.5 text-xs ${
                s.status === "SUCCESS"
                  ? "border-emerald-300 text-emerald-800 dark:border-emerald-800 dark:text-emerald-300"
                  : "border-rose-300 text-rose-800 dark:border-rose-800 dark:text-rose-300"
              }`}
            >
              {s.name}: {s.status.toLowerCase()}
            </span>
          ))}
        </div>
      )}
      {analysis && (
        <div className="rounded-lg bg-rose-50 p-3 text-sm dark:bg-rose-950/40">
          <p className="font-medium">
            Failed in {analysis.failing_stage}: {analysis.summary}
          </p>
          <p className="mt-1">Cause: {analysis.suspected_cause}</p>
          {analysis.recommended_fix && <p className="mt-1">Fix: {analysis.recommended_fix}</p>}
          {analysis.evidence.length > 0 && <pre className="mt-2 overflow-auto text-xs">{analysis.evidence.join("\n")}</pre>}
        </div>
      )}
      <details className="text-xs">
        <summary className="cursor-pointer text-slate-500">Console log (tail)</summary>
        <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-2 dark:bg-slate-950">{build.log_tail || "(empty)"}</pre>
      </details>
      <details className="text-xs">
        <summary className="cursor-pointer text-slate-500">Pipeline (generated from template)</summary>
        <pre className="mt-1 max-h-72 overflow-auto rounded bg-slate-50 p-2 dark:bg-slate-950">{report.pipeline}</pre>
      </details>
    </div>
  );
}

/** Stage-specific body for a verification task's result. */
export function VerificationBody({ result }: { result: TaskResult }) {
  return (
    <div className="flex flex-col gap-3">
      <VerdictLine result={result} />
      {result.review && <ReviewView report={result.review} />}
      {result.security && <SecurityView report={result.security} />}
      {result.qa && <QAView report={result.qa} />}
      {result.ci && <CIView report={result.ci} />}
      {result.change_analysis && result.change_analysis.domains.length > 0 && (
        <p className="text-xs text-slate-500">
          Change analysis: {result.change_analysis.domains.join(", ")} · risk {result.change_analysis.risk}
        </p>
      )}
    </div>
  );
}
