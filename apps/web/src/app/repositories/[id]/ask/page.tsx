"use client";

import { useParams } from "next/navigation";
import { useState } from "react";

import { Markdownish, inline } from "@/components/Markdownish";
import { Badge, Card, ErrorPanel, ReadOnlyNotice } from "@/components/ui";
import { ApiError, postJson } from "@/lib/api";
import { label, shortSha } from "@/lib/format";
import type { Evidence, QuestionAnswer } from "@/lib/types";
import { useReadOnly } from "@/lib/useReadOnly";

const EXAMPLES = [
  "Where is the main entry point implemented?",
  "Who calls this function?",
  "What could break if I change this class?",
  "Which tests cover this function?",
  "Which relationships are unresolved here?",
];

export default function AskPage() {
  const { id } = useParams<{ id: string }>();
  const [question, setQuestion] = useState("");
  const [useModel, setUseModel] = useState(true);
  const [answer, setAnswer] = useState<QuestionAnswer | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const readOnly = useReadOnly();

  async function ask(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setAnswer(
        await postJson<QuestionAnswer>(`/repositories/${id}/questions`, {
          question: question.trim(),
          use_model: useModel,
        }),
      );
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Card title="Ask about this repository">
        {readOnly === true ? (
          <ReadOnlyNotice what="asking questions" />
        ) : (
        <form onSubmit={ask} className="ask-form">
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. Who calls with_tax?  ·  What could break if I change Item.total?"
            rows={3}
            maxLength={1000}
            aria-label="Question"
          />
          <div className="form-row compact">
            <label className="checkbox">
              <input type="checkbox" checked={useModel} onChange={(e) => setUseModel(e.target.checked)} />
              Let the configured model explain the evidence (if one is enabled)
            </label>
            <button type="submit" disabled={busy || readOnly === null || !question.trim()}>
              {busy ? "Searching evidence…" : "Ask"}
            </button>
          </div>
        </form>
        )}
        <p className="muted small">
          Answers are assembled from the analysis: symbols, source, resolved relationships, recorded runs.
          Name a function, class, module or file for precise results. Examples: {EXAMPLES.join(" · ")}
        </p>
        {error && <ErrorPanel error={error} />}
      </Card>
      {answer && <AnswerView answer={answer} />}
    </>
  );
}

function AnswerView({ answer }: { answer: QuestionAnswer }) {
  return (
    <>
      <Card
        title={
          <>
            Answer <Badge value={answer.insufficient_evidence ? "PARTIAL" : "SUCCEEDED"} text={answer.insufficient_evidence ? "insufficient evidence" : label(answer.intent)} />
          </>
        }
      >
        {answer.model_explanation && (
          <div className="model-answer">
            <Markdownish text={answer.model_explanation} />
          </div>
        )}
        {answer.model_note && <p className="muted small">{answer.model_note}</p>}
        <Markdownish text={answer.answer_markdown} />
        <p className="muted small">
          Commit <span className="mono">{shortSha(answer.commit_sha)}</span> ·{" "}
          {answer.mode === "model" ? "model explanation + evidence" : "evidence only (no model)"}
        </p>
      </Card>
      {answer.evidence.length > 0 && (
        <Card title={`Evidence (${answer.evidence.length})`}>
          <ol className="evidence">
            {answer.evidence.map((e) => (
              <EvidenceItem key={e.id} evidence={e} />
            ))}
          </ol>
        </Card>
      )}
    </>
  );
}

function EvidenceItem({ evidence }: { evidence: Evidence }) {
  const [open, setOpen] = useState(false);
  const tone = {
    SOURCE_FACT: "SUCCEEDED",
    STATIC_RELATIONSHIP: "STATIC",
    RUNTIME_OBSERVATION: "RUNNING",
    UNCERTAINTY: "PARTIAL",
  }[evidence.category];
  return (
    <li id={evidence.id}>
      <span className="mono small">{evidence.id}</span> <Badge value={tone} text={label(evidence.category)} />{" "}
      {inline(evidence.statement)}
      {evidence.path && (
        <div className="muted small mono">
          {evidence.path}
          {evidence.start_line ? `:${evidence.start_line}-${evidence.end_line}` : ""} @ {shortSha(evidence.commit_sha)}{" "}
          {evidence.text && (
            <button className="link" onClick={() => setOpen(!open)}>
              {open ? "hide source" : "show source"}
            </button>
          )}
        </div>
      )}
      {open && evidence.text && <pre className="log">{evidence.text}</pre>}
    </li>
  );
}
