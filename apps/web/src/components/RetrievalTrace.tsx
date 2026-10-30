"use client";

import { useId, type CSSProperties, type ReactNode } from "react";

import type { ApiError } from "@/lib/api";
import { formatClock, label } from "@/lib/format";
import { useNow, useReducedMotion } from "@/lib/motion";
import type { Evidence, QuestionAnswer } from "@/lib/types";

/**
 * How a question was answered, step by step, from the fields the API returns.
 *
 * The API answers in one request and does not report intermediate progress, so while the
 * request is in flight the trace shows exactly that: one step, the request, with its clock.
 * Once the answer arrives the recorded steps (intent, matched symbols, evidence, answer
 * mode) are revealed one after another.
 */

// What the service retrieves for each intent (services/questions.py, QuestionService.answer).
const INTENT_PLAN: Record<string, string> = {
  DEFINITION: "definition and source, plus who refers to it",
  CALLERS: "definition, plus incoming calls and references",
  CALLEES: "definition, outgoing calls, and references it could not resolve",
  IMPACT: "definition, reverse dependencies, and unresolved references nearby",
  TESTS: "definition, reverse dependencies to tests, and recorded coverage",
  UNRESOLVED: "references the resolver could not settle",
  GENERAL: "the three best matches with their relationships both ways",
};

const CATEGORY_ORDER: Evidence["category"][] = [
  "SOURCE_FACT",
  "STATIC_RELATIONSHIP",
  "RUNTIME_OBSERVATION",
  "UNCERTAINTY",
];

export type TraceState =
  | { kind: "pending"; question: string; since: number }
  | { kind: "answered"; answer: QuestionAnswer }
  | { kind: "failed"; question: string; error: ApiError };

export function RetrievalTrace({ state }: { state: TraceState }) {
  const reduced = useReducedMotion();
  const heading = useId();
  return (
    <section className="trace" data-motion={reduced ? "reduced" : "full"} aria-labelledby={heading}>
      <h3 className="trace-head" id={heading}>
        How the answer was found
      </h3>
      {state.kind === "pending" && <Pending since={state.since} />}
      {state.kind === "failed" && (
        <ol className="trace-steps">
          <Step order={0} tone="bad" title="Request failed">
            The API answered {state.error.status || "nothing"} ({state.error.code}); no retrieval steps were reported.
          </Step>
        </ol>
      )}
      {state.kind === "answered" && <Answered answer={state.answer} />}
    </section>
  );
}

function Pending({ since }: { since: number }) {
  const now = useNow(250);
  return (
    <ol className="trace-steps">
      <Step order={0} tone="active" title="Asking the API" aside={<span className="num">{formatClock(now - since)}</span>}>
        Intent, symbol matching and evidence retrieval run together in this one request; the steps appear here when
        it returns.
      </Step>
    </ol>
  );
}

function Answered({ answer }: { answer: QuestionAnswer }) {
  const byCategory = new Map<string, number>();
  for (const e of answer.evidence) byCategory.set(e.category, (byCategory.get(e.category) ?? 0) + 1);
  const places = evidencePlaces(answer.evidence);
  const shownSymbols = answer.matched_symbols.slice(0, 10);
  return (
    <ol className="trace-steps">
      <Step order={0} tone="ok" title="Intent" aside={<span className="trace-chip">{label(answer.intent)}</span>}>
        {INTENT_PLAN[answer.intent] ? `Retrieves the ${INTENT_PLAN[answer.intent]}.` : "Classified from the wording."}
      </Step>
      <Step
        order={1}
        tone={shownSymbols.length ? "ok" : "warn"}
        title="Matched symbols"
        aside={<span className="num">{answer.matched_symbols.length}</span>}
      >
        {shownSymbols.length ? (
          <ul className="trace-chips">
            {shownSymbols.map((name, i) => (
              <li key={name} className={i === 0 ? "trace-chip is-primary" : "trace-chip"} style={{ "--i": i } as CSSProperties}>
                {name}
              </li>
            ))}
          </ul>
        ) : (
          "No symbol or path in the analysis matched the names in the question."
        )}
      </Step>
      <Step
        order={2}
        tone={answer.evidence.length ? "ok" : "warn"}
        title="Evidence"
        aside={<span className="num">{answer.evidence.length}</span>}
      >
        {answer.evidence.length === 0 ? (
          "Nothing was retrieved for these names."
        ) : (
          <>
            <ul className="trace-tally">
              {CATEGORY_ORDER.filter((c) => byCategory.has(c)).map((c) => (
                <li key={c}>
                  <span className={`trace-dot cat-${c.toLowerCase()}`} aria-hidden="true" />
                  {label(c)}
                  <span className="num muted">{byCategory.get(c)}</span>
                </li>
              ))}
            </ul>
            {places.length > 0 && (
              <ul className="trace-places">
                {places.slice(0, 8).map((p, i) => (
                  <li key={p.path} style={{ "--i": i } as CSSProperties}>
                    <span className="mono">{p.path}</span>
                    <span className="muted num">{p.lines}</span>
                  </li>
                ))}
                {places.length > 8 && <li className="muted">and {places.length - 8} more files</li>}
              </ul>
            )}
          </>
        )}
      </Step>
      <Step
        order={3}
        tone={answer.insufficient_evidence ? "warn" : "ok"}
        title="Answer"
        aside={<span className="trace-chip">{answer.mode === "model" ? "model" : "deterministic"}</span>}
      >
        {answer.mode === "model"
          ? "A language model phrased the answer from the same evidence; its citations were checked."
          : "Composed directly from the evidence, without a language model."}
        {answer.model_note && <span className="trace-note"> {answer.model_note}</span>}
        {answer.insufficient_evidence && <span className="trace-note"> Not enough evidence for a confident answer.</span>}
      </Step>
    </ol>
  );
}

/** Files the evidence points into, with the line ranges cited in each. */
export function evidencePlaces(evidence: Evidence[]): { path: string; lines: string }[] {
  const byPath = new Map<string, [number, number][]>();
  for (const e of evidence) {
    if (!e.path) continue;
    const ranges = byPath.get(e.path) ?? [];
    if (e.start_line) ranges.push([e.start_line, e.end_line ?? e.start_line]);
    byPath.set(e.path, ranges);
  }
  return [...byPath.entries()].map(([path, ranges]) => {
    const unique = [...new Map(ranges.map((r) => [`${r[0]}-${r[1]}`, r])).values()].sort((a, b) => a[0] - b[0]);
    const text = unique.slice(0, 4).map(([a, b]) => (a === b ? `${a}` : `${a}–${b}`));
    return { path, lines: unique.length ? `lines ${text.join(", ")}${unique.length > 4 ? ", …" : ""}` : "" };
  });
}

function Step({
  order,
  tone,
  title,
  aside,
  children,
}: {
  order: number;
  tone: "ok" | "warn" | "bad" | "active";
  title: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <li className={`trace-step tone-${tone}`} style={{ "--order": order } as CSSProperties}>
      <span className="trace-marker" aria-hidden="true" />
      <div className="trace-body">
        <p className="trace-title">
          <span>{title}</span>
          {aside && <span className="trace-aside">{aside}</span>}
        </p>
        <div className="trace-text">{children}</div>
      </div>
    </li>
  );
}
