import type { Specification } from "../lib/api";

const RISK: Record<Specification["risk_level"], string> = {
  low: "text-emerald-700 dark:text-emerald-400",
  medium: "text-amber-700 dark:text-amber-400",
  high: "text-orange-700 dark:text-orange-400",
  critical: "text-rose-700 dark:text-rose-400",
};

export function Card({ title, children, aside }: { title: string; children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function List({ items, empty }: { items: string[]; empty: string }) {
  if (!items.length) return <p className="text-sm text-slate-400">{empty}</p>;
  return (
    <ul className="list-disc space-y-1 pl-5 text-sm">
      {items.map((item, i) => (
        <li key={i}>{item}</li>
      ))}
    </ul>
  );
}

export function SpecificationView({ spec }: { spec: Specification }) {
  return (
    <div className="flex flex-col gap-4">
      <Card
        title="Requirement specification"
        aside={
          <span className="text-xs text-slate-500">
            v{spec.version} · {spec.status === "finalized" ? "finalized" : "draft"} ·{" "}
            <span className={`font-medium ${RISK[spec.risk_level]}`}>{spec.risk_level} risk</span>
          </span>
        }
      >
        <p className="font-medium">{spec.summary}</p>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">{spec.goal}</p>
        {spec.requires_human_approval && (
          <p className="mt-3 rounded-lg bg-amber-50 p-2 text-sm text-amber-900 dark:bg-amber-950/50 dark:text-amber-300">
            Sensitive work: human approval will be required before execution.
          </p>
        )}
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <div>
            <h3 className="mb-1 text-sm font-medium">In scope</h3>
            <List items={spec.scope.in_scope} empty="-" />
          </div>
          <div>
            <h3 className="mb-1 text-sm font-medium">Out of scope</h3>
            <List items={spec.scope.out_of_scope} empty="-" />
          </div>
        </div>
      </Card>

      <Card title="Checklist">
        <ol className="space-y-2">
          {spec.checklist.map((item) => (
            <li key={item.id} className="flex gap-3 text-sm">
              <span className="shrink-0 font-mono text-xs text-slate-400">{item.id}</span>
              <span>{item.description}</span>
            </li>
          ))}
        </ol>
      </Card>

      <Card title="Acceptance criteria">
        <ol className="space-y-2">
          {spec.acceptance_criteria.map((ac) => (
            <li key={ac.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
              <span className="shrink-0 font-mono text-xs text-slate-400">{ac.id}</span>
              <span className="min-w-0 flex-1">{ac.description}</span>
              <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                {ac.verification}
              </span>
            </li>
          ))}
        </ol>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Assumptions">
          <List items={spec.assumptions} empty="None recorded." />
        </Card>
        <Card title="Constraints">
          <List items={spec.constraints} empty="None recorded." />
        </Card>
      </div>

      <Card title="Repository observations">
        <List items={spec.repository_observations} empty="No repository was inspected." />
      </Card>
    </div>
  );
}
