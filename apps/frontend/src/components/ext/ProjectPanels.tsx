import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ext, type Project } from "../../lib/ext";
import { BUTTON, DANGER, ErrorText, Field, INPUT, Panel, Pill, PRIMARY } from "./shared";

/** Browser QA (spec sections 28, 156): the Playwright MCP preset for this project. */
export function BrowserPanel({ project, onChange }: { project: Project; onChange: () => void }) {
  const qc = useQueryClient();
  const presets = useQuery({ queryKey: ["mcp-presets"], queryFn: ext.mcpPresets });
  const mcps = useQuery({ queryKey: ["mcps"], queryFn: ext.mcps });
  const [origins, setOrigins] = useState(project.browser_allowed_origins.join(", "));
  useEffect(() => setOrigins(project.browser_allowed_origins.join(", ")), [project.browser_allowed_origins]);
  const playwright = presets.data?.find((p) => p.key === "playwright");
  const server = mcps.data?.find(({ server: s }) => s.preset === "playwright" && s.status !== "revoked")?.server;
  const enabled = !!server && project.enabled_mcp_ids.includes(server.mcp_id);
  const enable = useMutation({
    mutationFn: () => ext.enablePreset("playwright", project.project_id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["mcps"] });
      onChange();
    },
  });
  const saveOrigins = useMutation({
    mutationFn: () =>
      ext.updateProject(project.project_id, {
        browser_allowed_origins: origins.split(/[\s,]+/).filter(Boolean),
      }),
    onSuccess: onChange,
  });
  if (!playwright) return null;
  return (
    <Panel title="Browser QA (Playwright MCP)" aside={enabled ? <Pill value="active" /> : <Pill value="disabled" />}>
      <p className="text-sm text-slate-600 dark:text-slate-300">
        When an acceptance criterion is a <code>browser_test</code>, ForgeFlow serves the commit under test and the QA agent
        verifies it in Chromium through Playwright MCP, taking screenshots as evidence. The browser can only open the app
        under test; running code, evaluating JavaScript and uploading files are disabled
        {playwright.denied_tools.length > 0 && <> ({playwright.denied_tools.join(", ")})</>}.
      </p>
      <p className="mt-1 text-xs text-slate-500">
        Requires <code>docker compose --profile browser up -d</code>. The repository needs an <code>index.html</code> or a{" "}
        <code>preview:</code> entry in <code>forgeflow.yaml</code>.
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {!enabled && (
          <button className={PRIMARY} onClick={() => enable.mutate()} disabled={enable.isPending}>
            {enable.isPending ? "Enabling…" : "Enable browser QA for this project"}
          </button>
        )}
        {enabled && <span className="text-sm text-emerald-700 dark:text-emerald-400">Enabled for the QA agent with the Playwright skill.</span>}
        <ErrorText error={enable.error} />
      </div>
      {enabled && (
        <form
          className="mt-3 flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            saveOrigins.mutate();
          }}
        >
          <div className="min-w-64 flex-1">
            <Field label="Extra origins the browser may open" hint="Optional, e.g. a login page the app redirects to: https://auth.example.com">
              <input className={INPUT} value={origins} onChange={(e) => setOrigins(e.target.value)} />
            </Field>
          </div>
          <button className={BUTTON}>Save origins</button>
          <ErrorText error={saveOrigins.error} />
        </form>
      )}
    </Panel>
  );
}

const CHECK_STYLE = {
  healthy: "text-emerald-700 dark:text-emerald-400",
  unhealthy: "text-rose-700 dark:text-rose-400",
  unknown: "text-amber-700 dark:text-amber-400",
};

/** Deployment verification and approval-gated rollback (spec sections 43, 157). */
export function DeploymentPanel({ project, onChange }: { project: Project; onChange: () => void }) {
  const qc = useQueryClient();
  const connectors = useQuery({ queryKey: ["connectors"], queryFn: ext.connectors });
  const argo = connectors.data?.filter(({ connector: c }) => c.type === "argocd") ?? [];
  const checks = useQuery({
    queryKey: ["deployments", project.project_id],
    queryFn: () => ext.deployments(project.project_id),
    refetchInterval: (q) => (q.state.data?.some((c) => c.rollback === "requested") ? 3000 : false),
  });
  const [binding, setBinding] = useState({ connector_id: "", application: "", health_url: "", smoke_paths: "" });
  useEffect(() => {
    const d = project.deployment;
    setBinding({
      connector_id: d?.connector_id ?? argo[0]?.connector.connector_id ?? "",
      application: d?.application ?? argo[0]?.connector.repositories[0] ?? "",
      health_url: d?.health_url ?? "",
      smoke_paths: (d?.smoke_paths ?? []).join(", "),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.deployment, connectors.data]);
  const refreshChecks = () => qc.invalidateQueries({ queryKey: ["deployments", project.project_id] });
  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => ext.updateProject(project.project_id, body),
    onSuccess: onChange,
  });
  const verify = useMutation({ mutationFn: () => ext.verifyDeployment(project.project_id), onSuccess: refreshChecks });
  const rollback = useMutation({
    mutationFn: (id: string) => ext.rollback(id),
    onSuccess: () => {
      refreshChecks();
      qc.invalidateQueries({ queryKey: ["approvals"] });
    },
  });
  const selected = argo.find(({ connector: c }) => c.connector_id === binding.connector_id)?.connector;

  return (
    <Panel title="Deployment (Argo CD)">
      {argo.length === 0 ? (
        <p className="text-sm text-slate-500">Connect Argo CD under Connectors to verify deployments of this project.</p>
      ) : (
        <form
          className="grid gap-3 md:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate({
              deployment: {
                connector_id: binding.connector_id,
                application: binding.application,
                health_url: binding.health_url || null,
                smoke_paths: binding.smoke_paths.split(/[\s,]+/).filter(Boolean),
              },
            });
          }}
        >
          <Field label="Argo CD connector">
            <select className={INPUT} value={binding.connector_id} onChange={(e) => setBinding({ ...binding, connector_id: e.target.value })}>
              {argo.map(({ connector: c }) => (
                <option key={c.connector_id} value={c.connector_id}>
                  {c.name} ({c.status})
                </option>
              ))}
            </select>
          </Field>
          <Field label="Application">
            <select className={INPUT} value={binding.application} onChange={(e) => setBinding({ ...binding, application: e.target.value })}>
              {(selected?.repositories ?? []).map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Health URL" hint="Optional. GET must return 200.">
            <input className={INPUT} value={binding.health_url} onChange={(e) => setBinding({ ...binding, health_url: e.target.value })} placeholder="https://shop.example.com/healthz" />
          </Field>
          <Field label="Smoke paths" hint="Optional, on the health URL's host, e.g. / /cart">
            <input className={INPUT} value={binding.smoke_paths} onChange={(e) => setBinding({ ...binding, smoke_paths: e.target.value })} />
          </Field>
          <div className="flex flex-wrap items-center gap-2 md:col-span-2">
            <button className={PRIMARY} disabled={!binding.connector_id || !binding.application}>
              Save deployment binding
            </button>
            {project.deployment && (
              <>
                <button type="button" className={BUTTON} onClick={() => verify.mutate()} disabled={verify.isPending}>
                  {verify.isPending ? "Checking…" : "Verify deployment"}
                </button>
                <button type="button" className={BUTTON} onClick={() => save.mutate({ clear_deployment: true })}>
                  Remove binding
                </button>
              </>
            )}
            <ErrorText error={save.error ?? verify.error} />
          </div>
        </form>
      )}

      {(checks.data?.length ?? 0) > 0 && (
        <ul className="mt-4 flex flex-col gap-2">
          {checks.data?.slice(0, 5).map((c) => (
            <li key={c.check_id} className="rounded-xl border border-slate-200 p-3 text-sm dark:border-slate-800">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <span className={`font-semibold ${CHECK_STYLE[c.status]}`}>{c.status}</span>{" "}
                  <span className="text-slate-500">
                    · {c.application} · sync {c.sync_status ?? "?"} · health {c.health_status ?? "?"}
                    {c.revision && <> · {c.revision.slice(0, 10)}</>} · {new Date(c.created_at).toLocaleString()}
                  </span>
                </div>
                {c.rollback === "available" && (
                  <button className={DANGER} onClick={() => rollback.mutate(c.check_id)} disabled={rollback.isPending}>
                    Roll back to history #{c.rollback_to} (asks for approval)
                  </button>
                )}
                {c.rollback !== "available" && c.rollback !== "not_needed" && <Pill value={c.rollback} />}
              </div>
              <ul className="mt-1 text-xs">
                {c.probes.map((p) => (
                  <li key={p.name} className={p.passed ? "text-slate-500" : "text-rose-700 dark:text-rose-400"}>
                    {p.passed ? "✓" : "✗"} {p.name} · {p.detail}
                    {p.latency_ms > 0 && <> · {p.latency_ms} ms</>}
                  </li>
                ))}
              </ul>
              {c.rollback_detail && <p className="mt-1 text-xs text-slate-500">{c.rollback_detail}</p>}
            </li>
          ))}
        </ul>
      )}
      <ErrorText error={rollback.error} />
    </Panel>
  );
}
