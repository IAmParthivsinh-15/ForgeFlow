import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { AGENTS, ext } from "../../lib/ext";
import { BUTTON, ErrorText, Field, INPUT, Panel, Pill, PRIMARY } from "./shared";

/** Spec sections 212-222, 232-233, 250. */
export function SkillsTab() {
  const [scope, setScope] = useState<"all" | "mine" | "public">("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const skills = useQuery({ queryKey: ["skills", scope, query], queryFn: () => ext.skills(scope, query) });

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
      <div className="flex min-w-0 flex-col gap-4">
        <Panel
          title="Skills"
          aside={
            <div className="flex gap-1">
              {(["all", "mine", "public"] as const).map((s) => (
                <button key={s} onClick={() => setScope(s)} className={`${BUTTON} ${scope === s ? "ring-2 ring-sky-500" : ""}`}>
                  {s}
                </button>
              ))}
            </div>
          }
        >
          <input className={`${INPUT} mb-3`} placeholder="Search skills…" value={query} onChange={(e) => setQuery(e.target.value)} />
          {skills.data?.length === 0 && <p className="text-sm text-slate-500">No skills found.</p>}
          <ul className="flex flex-col gap-2">
            {skills.data?.map((s) => (
              <li key={s.skill_id}>
                <button
                  onClick={() => setSelected(s.skill_id)}
                  className={`w-full rounded-xl border p-3 text-left hover:border-slate-400 ${
                    selected === s.skill_id ? "border-sky-500" : "border-slate-200 dark:border-slate-800"
                  }`}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{s.name}</span>
                    <span className="text-xs text-slate-500">v{s.latest_version}</span>
                    <Pill value={s.visibility} />
                    {s.visibility === "public" && <Pill value={s.trust} />}
                  </div>
                  <p className="mt-0.5 text-xs text-slate-500">{s.description}</p>
                </button>
              </li>
            ))}
          </ul>
        </Panel>
        <CreateSkill onCreated={setSelected} />
      </div>
      <div className="min-w-0">{selected ? <SkillDetail skillId={selected} /> : <Panel title="Skill">Select a skill.</Panel>}</div>
    </div>
  );
}

function CreateSkill({ onCreated }: { onCreated: (id: string) => void }) {
  const qc = useQueryClient();
  const [form, setForm] = useState({
    name: "",
    slug: "",
    version: "1.0.0",
    description: "",
    when_to_use: "",
    tags: "",
    instructions: "",
    allowed_agents: [] as string[],
  });
  const done = (r: unknown) => {
    qc.invalidateQueries({ queryKey: ["skills"] });
    const id = (r as { skill?: { skill_id: string } }).skill?.skill_id;
    if (id) onCreated(id);
  };
  const create = useMutation({
    mutationFn: () =>
      ext.createSkill({
        metadata: {
          name: form.name,
          slug: form.slug,
          version: form.version,
          description: form.description,
          when_to_use: form.when_to_use,
          tags: form.tags.split(",").map((t) => t.trim()).filter(Boolean),
          allowed_agents: form.allowed_agents,
        },
        instructions: form.instructions,
      }),
    onSuccess: done,
  });
  const upload = useMutation({ mutationFn: (file: File) => ext.uploadSkill(file), onSuccess: done });

  return (
    <Panel
      title="Create skill"
      aside={
        <label className={`${BUTTON} cursor-pointer`}>
          Upload .zip
          <input
            type="file"
            accept=".zip"
            className="hidden"
            onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])}
          />
        </label>
      }
    >
      <form
        className="grid gap-3 sm:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Name">
          <input
            className={INPUT}
            value={form.name}
            onChange={(e) =>
              setForm({ ...form, name: e.target.value, slug: form.slug || e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") })
            }
          />
        </Field>
        <Field label="Slug">
          <input className={INPUT} value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} />
        </Field>
        <div className="sm:col-span-2">
          <Field label="Description">
            <input className={INPUT} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </Field>
        </div>
        <Field label="When to use">
          <input className={INPUT} value={form.when_to_use} onChange={(e) => setForm({ ...form, when_to_use: e.target.value })} />
        </Field>
        <Field label="Tags" hint="comma separated; used to match skills to tasks">
          <input className={INPUT} value={form.tags} onChange={(e) => setForm({ ...form, tags: e.target.value })} />
        </Field>
        <div className="sm:col-span-2">
          <Field label="Instructions (skill.md)" hint="Guidance for agents. It cannot grant tools or override safety rules.">
            <textarea rows={6} className={INPUT} value={form.instructions} onChange={(e) => setForm({ ...form, instructions: e.target.value })} />
          </Field>
        </div>
        <div className="sm:col-span-2">
          <Field label="Allowed agents" hint="None selected = all agents.">
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
        <div className="flex items-center gap-3 sm:col-span-2">
          <button className={PRIMARY} disabled={!form.name || !form.instructions || create.isPending}>
            Save private skill
          </button>
          <ErrorText error={create.error ?? upload.error} />
        </div>
      </form>
    </Panel>
  );
}

function SkillDetail({ skillId }: { skillId: string }) {
  const qc = useQueryClient();
  const detail = useQuery({ queryKey: ["skill", skillId], queryFn: () => ext.skill(skillId) });
  const projects = useQuery({ queryKey: ["projects"], queryFn: ext.projects });
  const [project, setProject] = useState<string>("");
  const [version, setVersion] = useState<string>("");
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["skill", skillId] });
    qc.invalidateQueries({ queryKey: ["skills"] });
  };
  const enable = useMutation({
    mutationFn: () => ext.enableSkill(skillId, { project_id: project || null, version: version || undefined }),
    onSuccess: refresh,
  });
  const disable = useMutation({ mutationFn: (p: string | null) => ext.disableSkill(skillId, p), onSuccess: refresh });
  const publish = useMutation({ mutationFn: () => ext.publishSkill(skillId), onSuccess: refresh });
  const fork = useMutation({ mutationFn: () => ext.forkSkill(skillId), onSuccess: refresh });
  if (!detail.data) return <Panel title="Skill">Loading…</Panel>;
  const { skill, versions, installations, owned } = detail.data;
  const latest = versions[0];

  return (
    <Panel title={skill.name} aside={<span className="text-xs text-slate-500">v{skill.latest_version}</span>}>
      <div className="flex flex-col gap-3 text-sm">
        <div className="flex flex-wrap gap-2">
          <Pill value={skill.visibility} />
          <Pill value={skill.status} />
          {skill.visibility === "public" && <Pill value={skill.trust} />}
          {skill.forked_from && <span className="text-xs text-slate-500">forked from {skill.forked_from}</span>}
        </div>
        <p>{skill.description}</p>
        {latest && (
          <>
            <p className="text-xs text-slate-500">
              Allowed agents: {latest.metadata.allowed_agents?.length ? latest.metadata.allowed_agents.join(", ") : "all"} · checksum{" "}
              <code>{latest.checksum.slice(0, 19)}…</code>
            </p>
            {latest.validation.warnings.length > 0 && (
              <ul className="list-disc pl-5 text-xs text-amber-700 dark:text-amber-400">
                {latest.validation.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            )}
            <details>
              <summary className="cursor-pointer text-xs text-slate-500">Instructions (v{latest.version})</summary>
              <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-2 text-xs dark:bg-slate-950">
                {latest.instructions}
              </pre>
            </details>
          </>
        )}

        <div className="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
          <h4 className="mb-2 text-xs font-medium text-slate-500">Enable</h4>
          <div className="flex flex-wrap items-center gap-2">
            <select className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-950" value={project} onChange={(e) => setProject(e.target.value)}>
              <option value="">All projects</option>
              {projects.data?.map((p) => (
                <option key={p.project_id} value={p.project_id}>
                  {p.name}
                </option>
              ))}
            </select>
            <select className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-950" value={version} onChange={(e) => setVersion(e.target.value)}>
              <option value="">latest (v{skill.latest_version})</option>
              {versions.map((v) => (
                <option key={v.version} value={v.version}>
                  pin v{v.version}
                </option>
              ))}
            </select>
            <button className={PRIMARY} onClick={() => enable.mutate()}>
              Enable
            </button>
          </div>
          {installations.length > 0 && (
            <ul className="mt-2 space-y-1 text-xs">
              {installations.map((i) => (
                <li key={i.installation_id} className="flex items-center gap-2">
                  <Pill value={i.enabled ? "active" : "disabled"} />
                  {i.project_id ?? "all projects"} · v{i.version}
                  {i.enabled && (
                    <button className={BUTTON} onClick={() => disable.mutate(i.project_id)}>
                      Disable
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex flex-wrap gap-2">
          {owned && skill.visibility === "private" && (
            <button className={BUTTON} onClick={() => publish.mutate()}>
              Publish (public)
            </button>
          )}
          <button className={BUTTON} onClick={() => fork.mutate()}>
            Fork to my skills
          </button>
        </div>
        <ErrorText error={enable.error ?? publish.error ?? fork.error ?? disable.error} />

        <div>
          <h4 className="text-xs font-medium text-slate-500">Versions (immutable)</h4>
          <ul className="text-xs">
            {versions.map((v) => (
              <li key={v.version}>
                v{v.version} · {new Date(v.created_at).toLocaleString()} · {Object.keys(v.files).length} reference file(s)
              </li>
            ))}
          </ul>
        </div>
      </div>
    </Panel>
  );
}
