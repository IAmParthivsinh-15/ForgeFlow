import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { AGENTS, ext } from "../../lib/ext";
import { BrowserPanel, DeploymentPanel } from "./ProjectPanels";
import { BUTTON, ErrorText, Field, INPUT, Panel, PRIMARY } from "./shared";

/** Spec section 241: capabilities are bound per project (repository). */
export function ProjectsTab() {
  const projects = useQuery({ queryKey: ["projects"], queryFn: ext.projects });
  const [selected, setSelected] = useState<string | null>(null);
  const current = selected ?? projects.data?.[0]?.project_id ?? null;

  return (
    <div className="grid gap-4 lg:grid-cols-[16rem_minmax(0,1fr)]">
      <Panel title="Projects">
        {projects.data?.length === 0 && <p className="text-sm text-slate-500">Add a repository under repos/.</p>}
        <ul className="flex flex-col gap-1">
          {projects.data?.map((p) => (
            <li key={p.project_id}>
              <button
                onClick={() => setSelected(p.project_id)}
                className={`w-full rounded-lg px-2 py-1.5 text-left text-sm ${current === p.project_id ? "bg-slate-100 font-medium dark:bg-slate-800" : "hover:bg-slate-50 dark:hover:bg-slate-800/50"}`}
              >
                {p.name}
                {p.github && <span className="ml-1 text-xs text-emerald-700 dark:text-emerald-400">· GitHub</span>}
              </button>
            </li>
          ))}
        </ul>
      </Panel>
      {current && <ProjectSettings projectId={current} />}
    </div>
  );
}

function ProjectSettings({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const detail = useQuery({ queryKey: ["project", projectId], queryFn: () => ext.project(projectId) });
  const connectors = useQuery({ queryKey: ["connectors"], queryFn: ext.connectors });
  const mcps = useQuery({ queryKey: ["mcps"], queryFn: ext.mcps });
  const [agent, setAgent] = useState("developer_subagent");
  const manifest = useQuery({ queryKey: ["manifest", projectId, agent], queryFn: () => ext.manifest(projectId, agent) });
  const [binding, setBinding] = useState({ connector_id: "", repository: "", base_branch: "", auto_pull_request: true, draft: false });

  useEffect(() => {
    const p = detail.data?.project;
    if (!p) return;
    setBinding({
      connector_id:
        p.github?.connector_id ??
        connectors.data?.find(({ connector: c }) => c.type === "github")?.connector.connector_id ??
        "",
      repository: p.github?.repository ?? detail.data?.detected_github_repository ?? "",
      base_branch: p.github?.base_branch ?? "",
      auto_pull_request: p.github?.auto_pull_request ?? true,
      draft: p.github?.draft ?? false,
    });
  }, [detail.data, connectors.data]);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["project", projectId] });
    qc.invalidateQueries({ queryKey: ["projects"] });
    qc.invalidateQueries({ queryKey: ["manifest", projectId] });
  };
  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => ext.updateProject(projectId, body),
    onSuccess: refresh,
  });
  const project = detail.data?.project;
  if (!project) return <Panel title="Project">Loading…</Panel>;

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Panel title={`GitHub · ${project.name}`}>
        <form
          className="grid gap-3 md:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate({ github: { ...binding, base_branch: binding.base_branch || null } });
          }}
        >
          <Field label="Connector">
            <select className={INPUT} value={binding.connector_id} onChange={(e) => setBinding({ ...binding, connector_id: e.target.value })}>
              <option value="">Choose…</option>
              {connectors.data
                ?.filter(({ connector: c }) => c.type === "github")
                .map(({ connector: c }) => (
                <option key={c.connector_id} value={c.connector_id}>
                  {c.name} ({c.status})
                </option>
              ))}
            </select>
          </Field>
          <Field label="GitHub repository" hint={detail.data?.detected_github_repository ? `Detected from origin: ${detail.data.detected_github_repository}` : "owner/repo"}>
            <input className={INPUT} value={binding.repository} onChange={(e) => setBinding({ ...binding, repository: e.target.value })} />
          </Field>
          <Field label="Base branch" hint="Empty = the branch the workflow started from.">
            <input className={INPUT} value={binding.base_branch} onChange={(e) => setBinding({ ...binding, base_branch: e.target.value })} />
          </Field>
          <div className="flex flex-col gap-2 text-sm">
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={binding.auto_pull_request} onChange={(e) => setBinding({ ...binding, auto_pull_request: e.target.checked })} />
              Open a pull request when verification passes (asks for approval)
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={binding.draft} onChange={(e) => setBinding({ ...binding, draft: e.target.checked })} />
              Open as draft
            </label>
          </div>
          <div className="flex items-center gap-2 md:col-span-2">
            <button className={PRIMARY} disabled={!binding.connector_id || !binding.repository}>
              Save GitHub binding
            </button>
            {project.github && (
              <button type="button" className={BUTTON} onClick={() => save.mutate({ clear_github: true })}>
                Remove binding
              </button>
            )}
            <ErrorText error={save.error} />
          </div>
        </form>
      </Panel>

      <BrowserPanel project={project} onChange={refresh} />

      <DeploymentPanel project={project} onChange={refresh} />

      <Panel title="MCP servers in this project">
        {mcps.data?.length === 0 && <p className="text-sm text-slate-500">No MCP servers registered.</p>}
        <div className="flex flex-wrap gap-4">
          {mcps.data?.map(({ server: s }) => (
            <label key={s.mcp_id} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={project.enabled_mcp_ids.includes(s.mcp_id)}
                onChange={(e) =>
                  save.mutate({
                    enabled_mcp_ids: e.target.checked
                      ? [...project.enabled_mcp_ids, s.mcp_id]
                      : project.enabled_mcp_ids.filter((x) => x !== s.mcp_id),
                  })
                }
              />
              {s.name} <span className="text-xs text-slate-500">({s.status})</span>
            </label>
          ))}
        </div>
      </Panel>

      <Panel
        title="What an agent receives here"
        aside={
          <select className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-950" value={agent} onChange={(e) => setAgent(e.target.value)}>
            {AGENTS.map((a) => (
              <option key={a} value={a}>
                {a.replace("_", " ")}
              </option>
            ))}
          </select>
        }
      >
        {manifest.data && (
          <div className="grid gap-3 text-sm md:grid-cols-2">
            <div>
              <h4 className="text-xs font-medium text-slate-500">Skills</h4>
              <ul className="text-xs">
                {manifest.data.skills.map((s) => (
                  <li key={s.slug}>
                    {s.name} v{s.version} · {s.level}
                  </li>
                ))}
                {manifest.data.skills.length === 0 && <li className="text-slate-500">none</li>}
              </ul>
            </div>
            <div>
              <h4 className="text-xs font-medium text-slate-500">MCP and connector tools</h4>
              <ul className="text-xs">
                {[...manifest.data.mcp_tools, ...manifest.data.connector_capabilities].map((t) => (
                  <li key={t.name}>
                    <code>{t.name}</code> · {t.policy}
                  </li>
                ))}
              </ul>
            </div>
            <div className="md:col-span-2">
              <h4 className="text-xs font-medium text-slate-500">Native tools</h4>
              <p className="text-xs">{manifest.data.native_tools.join(", ") || "none"}</p>
            </div>
            {manifest.data.unavailable.length > 0 && (
              <div className="md:col-span-2">
                <h4 className="text-xs font-medium text-slate-500">Excluded</h4>
                <ul className="list-disc pl-5 text-xs text-amber-700 dark:text-amber-400">
                  {manifest.data.unavailable.map((u, i) => (
                    <li key={i}>{u}</li>
                  ))}
                </ul>
              </div>
            )}
            <p className="text-[11px] text-slate-500 md:col-span-2">Manifest {manifest.data.hash.slice(0, 23)}…</p>
          </div>
        )}
      </Panel>
    </div>
  );
}
