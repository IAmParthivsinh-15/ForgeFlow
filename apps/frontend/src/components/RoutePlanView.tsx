import type { RoutePlan, RouteStage } from "../lib/api";
import { Card } from "./SpecificationView";

const AGENT_LABEL: Record<string, string> = {
  developer: "Developer",
  code_review: "Code Review",
  security: "Security",
  qa: "QA",
  ci: "CI",
};

/** Groups stages into dependency layers so parallel stages render side by side. */
function layers(stages: RouteStage[]): RouteStage[][] {
  const level = new Map<string, number>();
  const result: RouteStage[][] = [];
  for (const stage of stages) {
    const l = Math.max(-1, ...stage.depends_on.map((d) => level.get(d) ?? -1)) + 1;
    level.set(stage.stage_id, l);
    (result[l] ??= []).push(stage);
  }
  return result;
}

export function RoutePlanView({ plan }: { plan: RoutePlan }) {
  return (
    <Card title="Execution route">
      <p className="mb-4 text-sm text-slate-600 dark:text-slate-300">{plan.rationale}</p>
      {plan.stages.length > 0 && (
        <div className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center">
          {layers(plan.stages).map((layer, i) => (
            <div key={i} className="flex items-center gap-2 sm:contents">
              {i > 0 && <span className="hidden text-slate-400 sm:block" aria-hidden>→</span>}
              <div className="flex flex-1 flex-col gap-2">
                {layer.map((stage) => (
                  <div
                    key={stage.stage_id}
                    title={stage.reason}
                    className="rounded-xl border border-slate-200 px-3 py-2 dark:border-slate-700"
                  >
                    <div className="text-sm font-medium">{AGENT_LABEL[stage.agent] ?? stage.agent}</div>
                    <div className="text-xs text-slate-500">
                      {stage.implemented ? "ready" : "planned · agent not yet implemented"}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
      {plan.skipped.length > 0 && (
        <p className="mt-4 text-xs text-slate-500">
          Not required: {plan.skipped.map((c) => c.replace("_", " ")).join(", ")}
        </p>
      )}
    </Card>
  );
}
