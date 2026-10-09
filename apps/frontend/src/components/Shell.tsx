import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";

import { api, TERMINAL } from "../lib/api";
import { autonomy, ext } from "../lib/ext";

type NavItem = { to: string; label: string; count?: number; match: (path: string, search: string) => boolean };

function SideLink({ item }: { item: NavItem }) {
  const location = useLocation();
  const active = item.match(location.pathname, location.search);
  return (
    <Link
      to={item.to}
      className={`flex items-center justify-between rounded-[4px] px-3 py-2 font-mono text-[13px] ${
        active ? "bg-sky-500/10 text-sky-400" : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-200"
      }`}
    >
      <span className="flex items-center gap-2.5">
        <span className={`size-1.5 rounded-full ${active ? "bg-sky-400" : "bg-slate-600"}`} />
        {item.label}
      </span>
      {item.count !== undefined && item.count > 0 && (
        <span className={`text-[11px] ${active ? "text-sky-400" : "text-slate-500"}`}>{item.count}</span>
      )}
    </Link>
  );
}

const ext_tab = (tab: string) => (path: string, search: string) =>
  path === "/extensibility" && (new URLSearchParams(search).get("tab") ?? "connectors") === tab;

/** Obsidian layout: fixed sidebar, top bar, content. */
export function Shell({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const workflows = useQuery({ queryKey: ["workflows"], queryFn: api.listWorkflows, refetchInterval: 10000 });
  const repositories = useQuery({ queryKey: ["repositories"], queryFn: api.listRepositories });
  const projects = useQuery({ queryKey: ["projects"], queryFn: ext.projects });
  const approvals = useQuery({ queryKey: ["approvals", "pending"], queryFn: () => ext.approvals("pending"), refetchInterval: 10000 });
  const skills = useQuery({ queryKey: ["skills", "all", ""], queryFn: () => ext.skills("all") });
  const mcps = useQuery({ queryKey: ["mcps"], queryFn: ext.mcps });
  const connectors = useQuery({ queryKey: ["connectors"], queryFn: ext.connectors });
  const status = useQuery({ queryKey: ["autonomy-status"], queryFn: autonomy.status, refetchInterval: 10000, retry: 0 });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: async () => (await fetch("/health")).json() as Promise<{ status: string }>,
    refetchInterval: 15000,
    retry: 0,
  });

  const active = workflows.data?.filter((w) => !TERMINAL.includes(w.status)).length ?? 0;
  const runsActive = Object.values(status.data?.active ?? {}).reduce((a, b) => a + b, 0);
  const firstProject = projects.data?.[0];
  const repoLabel = firstProject?.github?.repository ?? (repositories.data?.[0] ? `repos / ${repositories.data[0]}` : "no repository");
  const live = health.data?.status === "ok";

  const nav: NavItem[] = [
    { to: "/", label: "Workflows", count: active, match: (p) => p === "/" || p.startsWith("/workflows") },
    { to: "/autonomy", label: "Autonomy", count: runsActive, match: (p) => p.startsWith("/autonomy") },
    { to: "/knowledge", label: "Knowledge", match: (p) => p.startsWith("/knowledge") },
  ];
  const extNav: NavItem[] = [
    { to: "/extensibility?tab=connectors", label: "Connectors", count: connectors.data?.length, match: ext_tab("connectors") },
    { to: "/extensibility?tab=mcp", label: "MCP servers", count: mcps.data?.length, match: ext_tab("mcp") },
    { to: "/extensibility?tab=skills", label: "Skills", match: ext_tab("skills") },
    { to: "/extensibility?tab=projects", label: "Projects", count: projects.data?.length, match: ext_tab("projects") },
    { to: "/extensibility?tab=approvals", label: "Approvals", count: approvals.data?.length, match: ext_tab("approvals") },
    { to: "/extensibility?tab=audit", label: "Audit log", match: ext_tab("audit") },
  ];

  return (
    <div className="flex min-h-screen bg-slate-950">
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-slate-800 bg-slate-900 md:flex">
        <div className="px-5 pt-5 pb-4">
          <Link to="/" className="flex items-center gap-2 font-[family-name:var(--font-display)] text-[15px] font-medium text-slate-100">
            <span className="size-2 rounded-full bg-sky-500" /> ForgeFlow
          </Link>
        </div>
        <div className="mx-3 mb-4 rounded-[6px] border border-slate-800 bg-slate-950/60 px-3 py-2.5 font-mono text-[11px]">
          <div className="truncate text-slate-400">{repoLabel}</div>
          <div className="mt-1 flex items-center justify-between text-slate-200/80">
            <span>{(repositories.data?.length ?? 0) === 1 ? "1 repository" : `${repositories.data?.length ?? 0} repositories`}</span>
            <span className="text-sky-400/80">{(workflows.data?.length ?? 0)} wf</span>
          </div>
        </div>
        <nav className="flex flex-col gap-0.5 px-3">
          {nav.map((item) => (
            <SideLink key={item.to} item={item} />
          ))}
          <div className="ff-label mt-4 mb-1 px-3">Extensibility</div>
          {extNav.map((item) => (
            <SideLink key={item.to} item={item} />
          ))}
        </nav>
        <div className="mt-auto border-t border-slate-800 px-5 py-4 font-mono text-[11px]">
          <div className="ff-label mb-2">Runtime</div>
          <div className="flex justify-between py-0.5 text-slate-400">
            <span>Skills</span>
            <span className="text-slate-200/70">{skills.data?.length ?? 0}</span>
          </div>
          <div className="flex justify-between py-0.5 text-slate-400">
            <span>MCP servers</span>
            <span className="text-slate-200/70">{mcps.data?.length ?? 0}</span>
          </div>
          <div className="flex justify-between py-0.5 text-slate-400">
            <span>Connectors</span>
            <span className="text-slate-200/70">{connectors.data?.length ?? 0}</span>
          </div>
          <div className="mt-2 flex items-center justify-between">
            <span className="text-slate-500">{status.data ? `${status.data.mode} mode` : "api"}</span>
            <span className={live ? "text-emerald-400" : "text-amber-400"}>
              <span className={`mr-1.5 inline-block size-1.5 rounded-full ${live ? "bg-emerald-400" : "bg-amber-400"}`} />
              {live ? "live" : health.isPending ? "…" : "degraded"}
            </span>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-slate-800 bg-slate-950/80 px-4 py-3 backdrop-blur md:px-8">
          <div className="flex min-w-0 items-center gap-2">
            <Link to="/" className="font-[family-name:var(--font-display)] text-[15px] font-medium md:hidden">
              ForgeFlow
            </Link>
            <span className="ff-pill-blue rounded-[5px] px-2 py-1 uppercase">Local workspace</span>
            {status.data && (
              <span className={`${status.data.mode === "autonomous" ? "ff-pill-green" : "ff-pill-amber"} rounded-[5px] px-2 py-1 uppercase`}>
                {status.data.mode}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <nav className="flex gap-1 md:hidden">
              {[...nav, { to: "/extensibility", label: "Ext", match: () => false }].map((n) => (
                <NavLink key={n.to} to={n.to} className="px-2 font-mono text-xs text-slate-400">
                  {n.label}
                </NavLink>
              ))}
            </nav>
            <button className="ff-btn-primary" onClick={() => navigate("/?new=1")}>
              <span className="text-base leading-none">+</span> New workflow
            </button>
          </div>
        </header>
        <main className="mx-auto w-full max-w-[1400px] flex-1 px-4 py-6 md:px-8">{children}</main>
      </div>
    </div>
  );
}
