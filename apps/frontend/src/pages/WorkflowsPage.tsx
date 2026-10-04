import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { StatusBadge } from "../components/StatusBadge";
import { api } from "../lib/api";

const EXAMPLES = [
  "Add a forgot-password flow.",
  "Review PR #142 for correctness and security.",
  "Run smoke tests against staging.",
  "Check this application against OWASP Top 10.",
];

export function WorkflowsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [request, setRequest] = useState("");
  const [repository, setRepository] = useState("");

  const workflows = useQuery({ queryKey: ["workflows"], queryFn: api.listWorkflows, refetchInterval: 5000 });
  const repositories = useQuery({ queryKey: ["repositories"], queryFn: api.listRepositories });

  const create = useMutation({
    mutationFn: () => api.createWorkflow({ request: request.trim(), repository_path: repository || null }),
    onSuccess: (wf) => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      navigate(`/workflows/${wf.workflow_id}`);
    },
  });

  return (
    <div className="flex flex-col gap-8">
      <section className="rounded-2xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
        <h1 className="text-xl font-semibold">New engineering request</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          ForgeFlow analyses the requirement first, asks only what it must, then routes the work to the
          capabilities it actually needs.
        </p>
        <form
          className="mt-4 flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (request.trim()) create.mutate();
          }}
        >
          <textarea
            rows={3}
            value={request}
            onChange={(e) => setRequest(e.target.value)}
            placeholder="What should be done?"
            maxLength={10000}
            className="w-full rounded-xl border border-slate-300 bg-white p-3 outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-500/30 dark:border-slate-700 dark:bg-slate-950"
          />
          <div className="flex flex-wrap gap-2">
            {EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                onClick={() => setRequest(example)}
                className="rounded-full border border-slate-200 px-3 py-1 text-xs text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
              >
                {example}
              </button>
            ))}
          </div>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <label className="flex items-center gap-2 text-sm">
              <span className="text-slate-500">Repository</span>
              <select
                value={repository}
                onChange={(e) => setRepository(e.target.value)}
                className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 dark:border-slate-700 dark:bg-slate-950"
              >
                <option value="">None</option>
                {repositories.data?.map((repo) => (
                  <option key={repo} value={repo}>
                    {repo}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="submit"
              disabled={!request.trim() || create.isPending}
              className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-40 dark:bg-white dark:text-slate-900 dark:hover:bg-slate-200"
            >
              {create.isPending ? "Starting…" : "Start workflow"}
            </button>
          </div>
          {create.isError && <p className="text-sm text-rose-600">{(create.error as Error).message}</p>}
        </form>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">Workflows</h2>
        {workflows.isError && <p className="text-sm text-rose-600">Could not reach the ForgeFlow API.</p>}
        {workflows.data?.length === 0 && <p className="text-sm text-slate-500">No workflows yet.</p>}
        <ul className="flex flex-col gap-2">
          {workflows.data?.map((wf) => (
            <li key={wf.workflow_id}>
              <Link
                to={`/workflows/${wf.workflow_id}`}
                className="flex flex-col gap-1 rounded-xl border border-slate-200 bg-white p-4 hover:border-slate-300 sm:flex-row sm:items-center sm:justify-between dark:border-slate-800 dark:bg-slate-900 dark:hover:border-slate-700"
              >
                <span className="min-w-0 truncate">{wf.request}</span>
                <span className="flex shrink-0 items-center gap-3 text-xs text-slate-500">
                  {wf.repository_path && <span className="font-mono">{wf.repository_path}</span>}
                  <StatusBadge status={wf.status} />
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
