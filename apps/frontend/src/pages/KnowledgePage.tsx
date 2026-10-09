import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { BUTTON, ErrorText, INPUT, Panel, PRIMARY } from "../components/ext/shared";
import { ext } from "../lib/ext";

const KINDS = [
  ["", "Everything"],
  ["failures", "Failures"],
  ["builds", "CI builds"],
  ["tests", "Test runs"],
  ["reviews", "Review findings"],
  ["security", "Security findings"],
  ["code", "Code"],
  ["agent-events", "Agent runs"],
] as const;

/** Searchable engineering history (spec section 36): "have we seen this before?" */
export function KnowledgePage() {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["knowledge-status"], queryFn: ext.knowledgeStatus, refetchInterval: 15000 });
  const projects = useQuery({ queryKey: ["projects"], queryFn: ext.projects });
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("");
  const [project, setProject] = useState("");
  const [submitted, setSubmitted] = useState<{ q: string; kind: string; project: string } | null>(null);
  const results = useQuery({
    queryKey: ["knowledge", submitted],
    queryFn: () => ext.searchKnowledge(submitted!.q, submitted!.kind || undefined, submitted!.project || undefined),
    enabled: !!submitted?.q,
  });
  const reindex = useMutation({
    mutationFn: (id: string) => ext.reindex(id, true),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["knowledge-status"] }),
  });
  const s = status.data;

  return (
    <div className="flex flex-col gap-4">
      <div>
        <div className="ff-label">Knowledge · engineering history</div>
        <h1 className="mt-3 text-3xl font-semibold text-slate-100">Knowledge</h1>
        <p className="mt-2 max-w-4xl text-base text-slate-500">
          Past failures, CI builds, test runs, review and security findings, and the indexed code of each project. Agents
          search this too: repair and CI-diagnosis tasks receive similar earlier failures and how they were fixed.
        </p>
      </div>

      <Panel
        title="Index"
        aside={
          s && (
            <span className={`text-xs ${s.available ? "text-emerald-700 dark:text-emerald-400" : "text-amber-700 dark:text-amber-400"}`}>
              {!s.enabled ? "not configured" : s.available ? "Elasticsearch connected" : "Elasticsearch unavailable"}
              {s.embeddings ? ` · embeddings: ${s.embeddings}` : " · keyword search (no embeddings)"}
            </span>
          )
        }
      >
        {s && !s.available && (
          <p className="text-sm text-slate-600 dark:text-slate-300">
            Start it with <code>docker compose --profile search up -d</code>. ForgeFlow keeps working without it.
            {s.last_error && <span className="block text-xs text-slate-500">{s.last_error}</span>}
          </p>
        )}
        {s?.available && (
          <div className="flex flex-col gap-3 text-sm">
            <div className="flex flex-wrap gap-4 text-xs text-slate-500">
              {Object.entries(s.counts).map(([k, v]) => (
                <span key={k}>
                  {k}: <b className="text-slate-800 dark:text-slate-200">{v}</b>
                </span>
              ))}
            </div>
            <ul className="flex flex-col gap-1">
              {projects.data?.map((p) => {
                const indexed = s.repositories.find((r) => r.repository_id === p.project_id);
                return (
                  <li key={p.project_id} className="flex flex-wrap items-center justify-between gap-2">
                    <span>
                      {p.name}{" "}
                      <span className="text-xs text-slate-500">
                        {indexed
                          ? `· ${indexed.chunks} chunks from ${indexed.files} files at ${indexed.commit.slice(0, 10)}`
                          : "· not indexed yet (indexed automatically when a workflow starts)"}
                      </span>
                    </span>
                    <button className={BUTTON} onClick={() => reindex.mutate(p.project_id)} disabled={reindex.isPending}>
                      Re-index
                    </button>
                  </li>
                );
              })}
            </ul>
            <ErrorText error={reindex.error} />
          </div>
        )}
      </Panel>

      <Panel title="Search">
        <form
          className="flex flex-wrap gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setSubmitted({ q, kind, project });
          }}
        >
          <input
            className={`${INPUT} min-w-64 flex-1`}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="KeyError: 'exp' in test_refresh_token"
          />
          <select className={`${INPUT} w-auto`} value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
          <select className={`${INPUT} w-auto`} value={project} onChange={(e) => setProject(e.target.value)}>
            <option value="">All projects</option>
            {projects.data?.map((p) => (
              <option key={p.project_id} value={p.project_id}>
                {p.name}
              </option>
            ))}
          </select>
          <button className={PRIMARY} disabled={!q.trim()}>
            Search
          </button>
        </form>
        <ErrorText error={results.error} />
        {results.data && results.data.length === 0 && <p className="mt-3 text-sm text-slate-500">No matches.</p>}
        <ul className="mt-3 flex flex-col gap-3">
          {results.data?.map((h) => (
            <li key={`${h.kind}:${h.id}`} className="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
                <span className="rounded bg-slate-100 px-1.5 py-0.5 font-medium text-slate-700 dark:bg-slate-800 dark:text-slate-300">{h.kind}</span>
                {h.status && <span>{h.status}</span>}
                {h.severity && <span>{h.severity}</span>}
                {h.path && (
                  <code>
                    {h.path}
                    {h.start_line ? `:${h.start_line}` : ""}
                  </code>
                )}
                {h.workflow_id && <Link to={`/workflows/${h.workflow_id}`} className="underline">{h.workflow_id}</Link>}
                {h.timestamp && <span>{h.timestamp.slice(0, 10)}</span>}
              </div>
              <div className="mt-1 text-sm font-medium">{h.title}</div>
              <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap text-xs text-slate-600 dark:text-slate-300">{h.text}</pre>
              {h.resolution && <p className="mt-1 text-xs text-emerald-700 dark:text-emerald-400">Fixed by: {h.resolution}</p>}
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
