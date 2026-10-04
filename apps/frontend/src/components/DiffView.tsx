/** Unified git diff with per-line colouring (spec section 65). */
export function DiffView({ diff }: { diff: string }) {
  if (!diff.trim()) return <p className="text-sm text-slate-500">No changes.</p>;
  return (
    <pre className="max-h-[28rem] overflow-auto rounded-lg border border-slate-200 bg-slate-50 p-3 font-mono text-xs leading-5 dark:border-slate-800 dark:bg-slate-950">
      {diff.split("\n").map((line, i) => (
        <div key={i} className={lineClass(line)}>
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

function lineClass(line: string): string {
  if (line.startsWith("diff --git")) return "mt-2 font-semibold text-slate-900 dark:text-slate-100";
  if (line.startsWith("+++") || line.startsWith("---")) return "text-slate-500";
  if (line.startsWith("@@")) return "text-violet-700 dark:text-violet-300";
  if (line.startsWith("+")) return "bg-emerald-100/70 text-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-200";
  if (line.startsWith("-")) return "bg-rose-100/70 text-rose-900 dark:bg-rose-950/60 dark:text-rose-200";
  return "text-slate-700 dark:text-slate-300";
}
