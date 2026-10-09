import type { ReactNode } from "react";

export const BUTTON = "ff-btn";
export const PRIMARY = "ff-btn-primary text-xs";
export const DANGER = "ff-btn-danger";
export const INPUT = "ff-input";

const PILL: Record<string, string> = {
  active: "ff-pill-green",
  published: "ff-pill-green",
  approved: "ff-pill-green",
  success: "ff-pill-green",
  done: "ff-pill-green",
  healthy: "ff-pill-green",
  pending: "ff-pill-amber",
  configured: "ff-pill-amber",
  requested: "ff-pill-blue",
  available: "ff-pill-amber",
  revoked: "ff-pill-red",
  rejected: "ff-pill-red",
  denied: "ff-pill-red",
  error: "ff-pill-red",
  failed: "ff-pill-red",
};

export function Pill({ value }: { value: string }) {
  return <span className={PILL[value] ?? "ff-pill-gray"}>{value.replace(/_/g, " ")}</span>;
}

export function Panel({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="ff-card p-5">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="ff-label">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="font-medium">{label}</span>
      {children}
      {hint && <span className="text-xs text-slate-500">{hint}</span>}
    </label>
  );
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  return <p className="text-sm text-rose-600 dark:text-rose-400">{(error as Error).message}</p>;
}
