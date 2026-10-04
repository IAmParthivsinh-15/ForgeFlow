import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type Question } from "../lib/api";

const CUSTOM = "CUSTOM";

/**
 * Spec section 174. The AI recommendation is pre-selected for convenience, but nothing is
 * submitted until the user presses Continue - the user remains authoritative.
 */
export function ClarificationDialog({
  workflowId,
  questions,
}: {
  workflowId: string;
  questions: Question[];
}) {
  const pending = questions.filter((q) => q.status === "awaiting_user");
  const question = pending[0];
  const position = questions.length - pending.length + 1;

  if (!question) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-950/50 p-4 backdrop-blur-sm sm:items-center">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="clarification-title"
        className="w-full max-w-lg rounded-2xl bg-white shadow-2xl ring-1 ring-slate-200 dark:bg-slate-900 dark:ring-slate-800"
      >
        <QuestionForm
          key={question.question_id}
          workflowId={workflowId}
          question={question}
          position={position}
          total={questions.length}
        />
      </div>
    </div>
  );
}

function QuestionForm({
  workflowId,
  question,
  position,
  total,
}: {
  workflowId: string;
  question: Question;
  position: number;
  total: number;
}) {
  const queryClient = useQueryClient();
  const recommended = question.options.find((o) => o.recommended);
  const [selected, setSelected] = useState<string>(recommended?.id ?? "");
  const [customText, setCustomText] = useState("");

  const submit = useMutation({
    mutationFn: () =>
      api.answerQuestion(question.question_id, {
        selected_option: selected,
        ...(selected === CUSTOM ? { custom_text: customText.trim() } : {}),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["workflow", workflowId] }),
  });

  const canSubmit = selected !== "" && (selected !== CUSTOM || customText.trim().length > 0);

  return (
    <form
      className="flex flex-col gap-5 p-6"
      onSubmit={(e) => {
        e.preventDefault();
        if (canSubmit) submit.mutate();
      }}
    >
      <header className="flex flex-col gap-1">
        <p className="text-xs font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-400">
          ForgeFlow needs a clarification{total > 1 ? ` · ${position} of ${total}` : ""}
        </p>
        <h2 id="clarification-title" className="text-lg font-semibold leading-snug">
          {question.question}
        </h2>
        {question.why_it_matters && (
          <p className="text-sm text-slate-500 dark:text-slate-400">{question.why_it_matters}</p>
        )}
      </header>

      <fieldset className="flex flex-col gap-2">
        <legend className="sr-only">Options</legend>
        {question.options.map((option) => {
          const isSelected = selected === option.id;
          return (
            <label
              key={option.id}
              className={`flex cursor-pointer gap-3 rounded-xl border p-3 transition ${
                isSelected
                  ? "border-sky-500 bg-sky-50 dark:border-sky-400 dark:bg-sky-950/40"
                  : "border-slate-200 hover:border-slate-300 dark:border-slate-700 dark:hover:border-slate-600"
              }`}
            >
              <input
                type="radio"
                name="option"
                value={option.id}
                checked={isSelected}
                onChange={() => setSelected(option.id)}
                className="mt-1 accent-sky-600"
              />
              <span className="flex min-w-0 flex-1 flex-col gap-1">
                <span className="flex flex-wrap items-center gap-2 font-medium">
                  {option.label}
                  {option.recommended && (
                    <span className="rounded-full bg-violet-100 px-2 py-0.5 text-xs font-medium text-violet-800 dark:bg-violet-950 dark:text-violet-300">
                      AI recommendation
                    </span>
                  )}
                </span>
                {option.description && (
                  <span className="text-sm text-slate-500 dark:text-slate-400">{option.description}</span>
                )}
                {option.id === CUSTOM && isSelected && (
                  <textarea
                    autoFocus
                    rows={3}
                    maxLength={2000}
                    value={customText}
                    onChange={(e) => setCustomText(e.target.value)}
                    placeholder="Describe what you want"
                    className="mt-1 w-full rounded-lg border border-slate-300 bg-white p-2 text-sm outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-500/30 dark:border-slate-700 dark:bg-slate-950"
                  />
                )}
              </span>
            </label>
          );
        })}
      </fieldset>

      {recommended?.reason && (
        <p className="rounded-lg bg-slate-100 p-3 text-sm text-slate-600 dark:bg-slate-800 dark:text-slate-300">
          <span className="font-medium">Why ForgeFlow recommends "{recommended.label}": </span>
          {recommended.reason}
        </p>
      )}

      {submit.isError && (
        <p className="text-sm text-rose-600 dark:text-rose-400">{(submit.error as Error).message}</p>
      )}

      <div className="flex justify-end">
        <button
          type="submit"
          disabled={!canSubmit || submit.isPending}
          className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40 dark:bg-white dark:text-slate-900 dark:hover:bg-slate-200"
        >
          {submit.isPending ? "Sending…" : "Continue"}
        </button>
      </div>
    </form>
  );
}
