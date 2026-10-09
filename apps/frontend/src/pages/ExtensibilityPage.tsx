import { useSearchParams } from "react-router-dom";

import { ApprovalsTab, AuditTab } from "../components/ext/ApprovalsTab";
import { ConnectorsTab } from "../components/ext/ConnectorsTab";
import { McpTab } from "../components/ext/McpTab";
import { ProjectsTab } from "../components/ext/ProjectsTab";
import { SkillsTab } from "../components/ext/SkillsTab";

const TABS = {
  connectors: { label: "Connectors", component: ConnectorsTab },
  mcp: { label: "MCP servers", component: McpTab },
  skills: { label: "Skills", component: SkillsTab },
  projects: { label: "Projects", component: ProjectsTab },
  approvals: { label: "Approvals", component: ApprovalsTab },
  audit: { label: "Audit log", component: AuditTab },
} as const;

type Tab = keyof typeof TABS;

/** Extensibility (spec section 249): what agents may use, and what they did use. */
export function ExtensibilityPage() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) in TABS ? (params.get("tab") as Tab) : "connectors";
  const Active = TABS[tab].component;
  return (
    <div className="flex flex-col gap-4">
      <div>
        <div className="ff-label">Extensibility · gateway</div>
        <h1 className="mt-3 text-3xl font-semibold text-slate-100">Extensibility</h1>
        <p className="mt-2 text-base text-slate-500">
          Connect services, register MCP servers and manage skills. Agents only receive what a project enables and policy allows;
          every use is audited.
        </p>
      </div>
      <nav className="flex flex-wrap border-b border-slate-800">
        {(Object.keys(TABS) as Tab[]).map((key) => (
          <button
            key={key}
            onClick={() => setParams({ tab: key })}
            className={`ff-tab ${tab === key ? "ff-tab-active" : ""}`}
          >
            {TABS[key].label}
          </button>
        ))}
      </nav>
      <Active />
    </div>
  );
}
