import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ext } from "../../lib/ext";
import { BUTTON, DANGER, ErrorText, Field, INPUT, Panel, Pill, PRIMARY } from "./shared";

/** Spec sections 204-206, 254: connect, test, disable, revoke - credentials are write-only. */
export function ConnectorsTab() {
  const qc = useQueryClient();
  const connectors = useQuery({ queryKey: ["connectors"], queryFn: ext.connectors });
  const [name, setName] = useState("GitHub");
  const [token, setToken] = useState("");
  const [repos, setRepos] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["connectors"] });
  const create = useMutation({
    mutationFn: () =>
      ext.createConnector({
        name,
        token,
        repositories: repos.split(/[\s,]+/).filter(Boolean),
      }),
    onSuccess: () => {
      setToken("");
      refresh();
    },
  });
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "test" | "disable" | "enable" | "revoke" | "delete" }) =>
      action === "delete" ? ext.deleteConnector(id) : ext.connectorAction(id, action),
    onSuccess: refresh,
  });

  return (
    <div className="flex flex-col gap-4">
      <Panel title="Connect GitHub">
        <form
          className="grid gap-3 md:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <Field label="Name">
            <input className={INPUT} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Repositories" hint="owner/repo, comma separated. The connector can only touch these.">
            <input className={INPUT} value={repos} onChange={(e) => setRepos(e.target.value)} placeholder="octo/app" />
          </Field>
          <div className="md:col-span-2">
            <Field
              label="Fine-grained personal access token"
              hint={
                <>
                  GitHub → Settings → Developer settings → Fine-grained tokens. Repository access: only the
                  repositories above. Permissions: <b>Contents: Read and write</b>, <b>Pull requests: Read and write</b>{" "}
                  (Metadata: Read is added automatically). Stored encrypted; never shown again.
                </>
              }
            >
              <input
                className={INPUT}
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="github_pat_…"
              />
            </Field>
          </div>
          <div className="flex items-center gap-3 md:col-span-2">
            <button className={PRIMARY} disabled={!token || !repos || create.isPending}>
              {create.isPending ? "Connecting…" : "Connect and test"}
            </button>
            <ErrorText error={create.error} />
          </div>
        </form>
      </Panel>

      <ArgoCDForm onCreated={refresh} />

      <Panel title="Connectors">
        {connectors.data?.length === 0 && <p className="text-sm text-slate-500">No connectors yet.</p>}
        <ul className="flex flex-col gap-3">
          {connectors.data?.map(({ connector: c, has_credential }) => (
            <li key={c.connector_id} className="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{c.name}</span>
                  <Pill value={c.status} />
                  {c.account && <span className="text-xs text-slate-500">as {c.account}</span>}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {c.status !== "revoked" && (
                    <button className={BUTTON} onClick={() => act.mutate({ id: c.connector_id, action: "test" })}>
                      Test
                    </button>
                  )}
                  {c.status === "active" && (
                    <button className={BUTTON} onClick={() => act.mutate({ id: c.connector_id, action: "disable" })}>
                      Disable
                    </button>
                  )}
                  {c.status === "disabled" && (
                    <button className={BUTTON} onClick={() => act.mutate({ id: c.connector_id, action: "enable" })}>
                      Enable
                    </button>
                  )}
                  {c.status !== "revoked" && (
                    <button className={DANGER} onClick={() => act.mutate({ id: c.connector_id, action: "revoke" })}>
                      Revoke
                    </button>
                  )}
                  <button className={DANGER} onClick={() => act.mutate({ id: c.connector_id, action: "delete" })}>
                    Delete
                  </button>
                </div>
              </div>
              <p className="mt-1 text-xs text-slate-500">
                {c.type === "argocd" ? `Argo CD ${String(c.config.url ?? "")} · applications` : "Repositories"}:{" "}
                {c.repositories.join(", ")} · scopes: {c.scopes.join(", ") || "not tested"} · credential:{" "}
                {has_credential ? "stored (encrypted)" : "none"}
              </p>
              {c.last_error && <p className="mt-1 text-xs text-rose-600">{c.last_error}</p>}
            </li>
          ))}
        </ul>
        <ErrorText error={act.error} />
      </Panel>
    </div>
  );
}

/** Argo CD (spec sections 43, 157): deployment status, approved sync and rollback. */
function ArgoCDForm({ onCreated }: { onCreated: () => void }) {
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [apps, setApps] = useState("");
  const [verifyTls, setVerifyTls] = useState(true);
  const create = useMutation({
    mutationFn: () =>
      ext.createConnector({
        type: "argocd",
        name: "Argo CD",
        url,
        token,
        applications: apps.split(/[\s,]+/).filter(Boolean),
        verify_tls: verifyTls,
      }),
    onSuccess: () => {
      setToken("");
      onCreated();
    },
  });
  return (
    <Panel title="Connect Argo CD">
      <form
        className="grid gap-3 md:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Server URL">
          <input className={INPUT} value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://argocd.example.com" />
        </Field>
        <Field label="Applications" hint="Argo CD application names this connector may read, sync or roll back.">
          <input className={INPUT} value={apps} onChange={(e) => setApps(e.target.value)} placeholder="forgeflow" />
        </Field>
        <div className="md:col-span-2">
          <Field
            label="API token"
            hint={
              <>
                A project-role token limited to these applications:{" "}
                <code>argocd proj role create-token forgeflow forgeflow-connector</code>. ForgeFlow reads status
                automatically; sync and rollback always ask you first. Stored encrypted; never shown again.
              </>
            }
          >
            <input className={INPUT} type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />
          </Field>
        </div>
        <label className="flex items-center gap-2 text-sm md:col-span-2">
          <input type="checkbox" checked={verifyTls} onChange={(e) => setVerifyTls(e.target.checked)} />
          Verify TLS certificate (turn off only for a local Argo CD with a self-signed certificate)
        </label>
        <div className="flex items-center gap-3 md:col-span-2">
          <button className={PRIMARY} disabled={!url || !token || !apps || create.isPending}>
            {create.isPending ? "Connecting…" : "Connect and test"}
          </button>
          <ErrorText error={create.error} />
        </div>
      </form>
    </Panel>
  );
}
