import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { AGENTS, ext, type Policy } from "../../lib/ext";
import { BUTTON, DANGER, ErrorText, Field, INPUT, Panel, Pill, PRIMARY } from "./shared";

const OPERATION_STYLE = {
  read: "text-emerald-700 dark:text-emerald-400",
  write: "text-amber-700 dark:text-amber-400",
  destructive: "text-rose-700 dark:text-rose-400",
};

/** Spec sections 207-211: register, discover, classify, control per tool, revoke. */
export function McpTab() {
  const qc = useQueryClient();
  const servers = useQuery({ queryKey: ["mcps"], queryFn: ext.mcps });
  const allowlist = useQuery({ queryKey: ["mcp-allowlist"], queryFn: ext.mcpAllowlist });
  const [form, setForm] = useState({
    name: "",
    transport: "stdio",
    stdio_server: "",
    url: "",
    token: "",
    approval_policy: "auto_read_ask_write",
    allowed_agents: [] as string[],
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["mcps"] });
  const create = useMutation({
    mutationFn: () =>
      ext.createMcp({
        name: form.name,
        transport: form.transport,
        approval_policy: form.approval_policy,
        allowed_agents: form.allowed_agents,
        ...(form.transport === "stdio"
          ? { stdio_server: form.stdio_server }
          : { url: form.url, token: form.token || undefined }),
      }),
    onSuccess: refresh,
  });
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "refresh-tools" | "disable" | "enable" | "revoke" | "delete" }) =>
      action === "delete" ? ext.deleteMcp(id) : ext.mcpAction(id, action),
    onSuccess: refresh,
  });
  const tool = useMutation({
    mutationFn: ({ id, name, body }: { id: string; name: string; body: { enabled?: boolean; policy?: Policy; clear_policy?: boolean } }) =>
      ext.updateTool(id, name, body),
    onSuccess: refresh,
  });

  return (
    <div className="flex flex-col gap-4">
      <PresetsPanel onEnabled={refresh} />
      <Panel title="Add MCP server">
        <form
          className="grid gap-3 md:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <Field label="Name">
            <input className={INPUT} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field label="Transport">
            <select className={INPUT} value={form.transport} onChange={(e) => setForm({ ...form, transport: e.target.value })}>
              <option value="stdio">stdio (allowlisted servers only)</option>
              <option value="streamable_http">Streamable HTTP</option>
              <option value="sse">SSE</option>
            </select>
          </Field>
          {form.transport === "stdio" ? (
            <Field label="Server" hint="Only servers in config/mcp_stdio_allowlist.yaml can be started.">
              <select className={INPUT} value={form.stdio_server} onChange={(e) => setForm({ ...form, stdio_server: e.target.value })}>
                <option value="">Choose…</option>
                {Object.entries(allowlist.data ?? {}).map(([key, description]) => (
                  <option key={key} value={key}>
                    {key} — {description}
                  </option>
                ))}
              </select>
            </Field>
          ) : (
            <>
              <Field label="URL" hint="Never put credentials in the URL.">
                <input className={INPUT} value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} placeholder="https://…/mcp" />
              </Field>
              <Field label="Token (optional)" hint="Sent as an Authorization: Bearer header; stored encrypted.">
                <input className={INPUT} type="password" value={form.token} onChange={(e) => setForm({ ...form, token: e.target.value })} />
              </Field>
            </>
          )}
          <Field label="Approval policy">
            <select className={INPUT} value={form.approval_policy} onChange={(e) => setForm({ ...form, approval_policy: e.target.value })}>
              <option value="auto_read_ask_write">Read tools auto, write tools ask (recommended)</option>
              <option value="ask_all">Ask for every call</option>
              <option value="auto_all">Auto (destructive still asks)</option>
            </select>
          </Field>
          <div className="md:col-span-2">
            <Field label="Agents allowed to use it" hint="None selected = any agent in projects that enable this server.">
              <div className="flex flex-wrap gap-3">
                {AGENTS.map((a) => (
                  <label key={a} className="flex items-center gap-1 text-xs">
                    <input
                      type="checkbox"
                      checked={form.allowed_agents.includes(a)}
                      onChange={(e) =>
                        setForm({
                          ...form,
                          allowed_agents: e.target.checked ? [...form.allowed_agents, a] : form.allowed_agents.filter((x) => x !== a),
                        })
                      }
                    />
                    {a.replace("_", " ")}
                  </label>
                ))}
              </div>
            </Field>
          </div>
          <div className="flex items-center gap-3 md:col-span-2">
            <button className={PRIMARY} disabled={!form.name || create.isPending}>
              {create.isPending ? "Connecting…" : "Register and discover tools"}
            </button>
            <ErrorText error={create.error} />
          </div>
        </form>
      </Panel>

      <Panel title="MCP servers">
        {servers.data?.length === 0 && <p className="text-sm text-slate-500">No MCP servers yet.</p>}
        <ul className="flex flex-col gap-3">
          {servers.data?.map(({ server: s, tools }) => (
            <li key={s.mcp_id} className="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{s.name}</span>
                  <Pill value={s.status} />
                  <span className="text-xs text-slate-500">
                    {s.transport} {s.stdio_server ?? s.url} · {tools.length} tool(s)
                    {s.allowed_agents.length > 0 && ` · agents: ${s.allowed_agents.join(", ")}`}
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  <button className={BUTTON} onClick={() => act.mutate({ id: s.mcp_id, action: "refresh-tools" })}>
                    Refresh tools
                  </button>
                  {s.status === "active" ? (
                    <button className={BUTTON} onClick={() => act.mutate({ id: s.mcp_id, action: "disable" })}>
                      Disable
                    </button>
                  ) : (
                    s.status !== "revoked" && (
                      <button className={BUTTON} onClick={() => act.mutate({ id: s.mcp_id, action: "enable" })}>
                        Enable
                      </button>
                    )
                  )}
                  {s.status !== "revoked" && (
                    <button className={DANGER} onClick={() => act.mutate({ id: s.mcp_id, action: "revoke" })}>
                      Revoke
                    </button>
                  )}
                  <button className={DANGER} onClick={() => act.mutate({ id: s.mcp_id, action: "delete" })}>
                    Delete
                  </button>
                </div>
              </div>
              {s.last_error && <p className="mt-1 text-xs text-rose-600">{s.last_error}</p>}
              {tools.length > 0 && (
                <div className="mt-2 overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="text-slate-500">
                      <tr>
                        <th className="py-1 pr-3 font-medium">Tool</th>
                        <th className="py-1 pr-3 font-medium">Operation</th>
                        <th className="py-1 pr-3 font-medium">Policy</th>
                        <th className="py-1 font-medium">Enabled</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tools.map((t) => (
                        <tr key={t.tool_id} className="border-t border-slate-100 dark:border-slate-800">
                          <td className="py-1 pr-3" title={t.description}>
                            <code>{t.name}</code>
                          </td>
                          <td className={`py-1 pr-3 font-medium ${OPERATION_STYLE[t.operation]}`}>{t.operation}</td>
                          <td className="py-1 pr-3">
                            <select
                              className="rounded border border-slate-300 bg-white px-1 py-0.5 dark:border-slate-700 dark:bg-slate-950"
                              value={t.policy_override ?? ""}
                              onChange={(e) =>
                                tool.mutate({
                                  id: s.mcp_id,
                                  name: t.name,
                                  body: e.target.value ? { policy: e.target.value as Policy } : { clear_policy: true },
                                })
                              }
                            >
                              <option value="">default</option>
                              <option value="auto">auto</option>
                              <option value="ask">ask</option>
                              <option value="deny">deny</option>
                            </select>
                          </td>
                          <td className="py-1">
                            <input
                              type="checkbox"
                              checked={t.enabled}
                              onChange={(e) => tool.mutate({ id: s.mcp_id, name: t.name, body: { enabled: e.target.checked } })}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </li>
          ))}
        </ul>
        <ErrorText error={act.error ?? tool.error} />
      </Panel>
    </div>
  );
}

/** Admin-reviewed presets (config/mcp_presets.yaml), e.g. Playwright for browser QA. */
function PresetsPanel({ onEnabled }: { onEnabled: () => void }) {
  const presets = useQuery({ queryKey: ["mcp-presets"], queryFn: ext.mcpPresets });
  const enable = useMutation({ mutationFn: (key: string) => ext.enablePreset(key, null), onSuccess: onEnabled });
  if (!presets.data?.length) return null;
  return (
    <Panel title="Presets">
      <ul className="flex flex-col gap-3">
        {presets.data.map((p) => (
          <li key={p.key} className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0 flex-1 text-sm">
              <div className="font-medium">{p.name}</div>
              <p className="text-xs text-slate-500">{p.description}</p>
              <p className="text-xs text-slate-500">
                {p.transport} {p.url && <code>{p.url}</code>} · agents: {p.allowed_agents.join(", ") || "all"}
                {p.skill && <> · skill: {p.skill}</>}
                {p.denied_tools.length > 0 && <> · disabled: {p.denied_tools.join(", ")}</>}
              </p>
            </div>
            <button className={BUTTON} onClick={() => enable.mutate(p.key)} disabled={enable.isPending}>
              Register
            </button>
          </li>
        ))}
      </ul>
      <p className="mt-2 text-xs text-slate-500">Enable it for a project under Projects to give the QA agent the tools and the skill.</p>
      <ErrorText error={enable.error} />
    </Panel>
  );
}
