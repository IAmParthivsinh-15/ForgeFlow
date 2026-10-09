import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { StatusBadge } from "../components/StatusBadge";
import { api, TERMINAL } from "../lib/api";

const EXAMPLES = [
  "Add a forgot-password flow.",
  "Review PR #142 for correctness and security.",
  "Run smoke tests against staging.",
  "Check this application against OWASP Top 10.",
];

function ago(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function WorkflowsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [request, setRequest] = useState("");
  const [repository, setRepository] = useState("");
  const textarea = useRef<HTMLTextAreaElement>(null);

  const workflows = useQuery({ queryKey: ["workflows"], queryFn: api.listWorkflows, refetchInterval: 5000 });
  const repositories = useQuery({ queryKey: ["repositories"], queryFn: api.listRepositories });

  useEffect(() => {
    if (params.get("new")) {
      textarea.current?.focus();
      textarea.current?.scrollIntoView({ block: "center" });
      setParams({}, { replace: true });
    }
  }, [params, setParams]);

  const create = useMutation({
    mutationFn: () => api.createWorkflow({ request: request.trim(), repository_path: repository || null }),
    onSuccess: (wf) => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      navigate(`/workflows/${wf.workflow_id}`);
    },
  });
  const active = workflows.data?.filter((w) => !TERMINAL.includes(w.status)) ?? [];
  const finished = workflows.data?.filter((w) => TERMINAL.includes(w.status)) ?? [];

  return (
    <div className="flex max-w-5xl flex-col gap-8">
      <div>
        <div className="ff-label">Workflows · {workflows.data?.length ?? 0} total · {active.length} active</div>
        <h1 className="mt-3 text-3xl font-semibold text-slate-100">New engineering request</h1>
        <p className="mt-2 max-w-3xl text-base text-slate-500">
          ForgeFlow analyses the requirement first, asks only what it must, then routes the work to the capabilities it
          actually needs.
        </p>
      </div>

      <form
        className="ff-card flex flex-col gap-3 p-5"
        onSubmit={(e) => {
          e.preventDefault();
          if (request.trim()) create.mutate();
        }}
      >
        <textarea
          ref={textarea}
          rows={3}
          value={request}
          onChange={(e) => setRequest(e.target.value)}
          placeholder="What should be done?"
          maxLength={10000}
          className="ff-input resize-y p-3 text-[15px]"
        />
        <div className="flex flex-wrap gap-2">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => setRequest(example)}
              className="ff-pill-gray px-2.5 py-1 text-[11px] hover:border-slate-500 hover:text-slate-200"
            >
              {example}
            </button>
          ))}
        </div>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <label className="flex items-center gap-2 text-sm">
            <span className="ff-label">Repository</span>
            <select value={repository} onChange={(e) => setRepository(e.target.value)} className="ff-input w-auto font-mono text-xs">
              <option value="">None</option>
              {repositories.data?.map((repo) => (
                <option key={repo} value={repo}>
                  {repo}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={!request.trim() || create.isPending} className="ff-btn-primary">
            {create.isPending ? "Starting…" : "Start workflow"}
          </button>
        </div>
        {create.isError && <p className="text-sm text-rose-400">{(create.error as Error).message}</p>}
      </form>

      {workflows.isError && <p className="text-sm text-rose-400">Could not reach the ForgeFlow API.</p>}
      {[
        ["Active", active],
        ["Finished", finished],
      ].map(([label, list]) =>
        (list as typeof active).length === 0 ? null : (
          <section key={label as string}>
            <h2 className="ff-label mb-3">{label as string}</h2>
            <ul className="flex flex-col gap-2">
              {(list as typeof active).map((wf) => (
                <li key={wf.workflow_id}>
                  <Link
                    to={`/workflows/${wf.workflow_id}`}
                    className="ff-card flex flex-col gap-2 px-4 py-3.5 hover:border-slate-700 sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div className="min-w-0">
                      <div className="ff-label normal-case">
                        {wf.workflow_id}
                        {wf.repository_path && ` · ${wf.repository_path}`} · {ago(wf.created_at)}
                      </div>
                      <div className="mt-1 truncate text-[15px] font-medium text-slate-100">{wf.request.split("\n")[0]}</div>
                    </div>
                    <StatusBadge status={wf.status} />
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        ),
      )}
      {workflows.data?.length === 0 && <p className="text-sm text-slate-500">No workflows yet.</p>}
    </div>
  );
}
