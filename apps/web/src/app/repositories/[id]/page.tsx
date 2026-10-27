"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { Badge, Card, Empty, ErrorPanel, KeyValues, Loading, ReadOnlyNotice, Stat } from "@/components/ui";
import { ApiError, postJson } from "@/lib/api";
import { TERMINAL_ANALYSIS, formatBytes, formatDuration, formatTime, label, shortSha } from "@/lib/format";
import type { Analysis, AnalysisSummary, FilePage, Page, TestCase, TestRun, Warning } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { useReadOnly } from "@/lib/useReadOnly";

export default function RepositoryOverview() {
  const { id } = useParams<{ id: string }>();
  const analyses = useApi<Page<AnalysisSummary>>(`/repositories/${id}/analyses?limit=20`, {
    pollWhile: (page) => page.items.some((a) => !TERMINAL_ANALYSIS.has(a.status)),
  });
  const [selected, setSelected] = useState<string | null>(null);
  const current = selected ?? analyses.data?.items[0]?.id ?? null;

  return (
    <>
      <AnalyzeForm repoId={id} onQueued={analyses.reload} />
      {analyses.error && <ErrorPanel error={analyses.error} />}
      {analyses.loading && !analyses.data && <Loading what="Loading analyses" />}
      {analyses.data && analyses.data.items.length === 0 && (
        <Empty>This repository has not been analyzed yet.</Empty>
      )}
      {current && <AnalysisDetail analysisId={current} onChange={analyses.reload} />}
      {analyses.data && analyses.data.items.length > 1 && (
        <Card title="Analysis history">
          <table className="table">
            <thead>
              <tr>
                <th>Requested</th>
                <th>Status</th>
                <th>Commit</th>
                <th>Created</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {analyses.data.items.map((a) => (
                <tr key={a.id} className={a.id === current ? "selected" : undefined}>
                  <td className="mono">{a.requested_ref}</td>
                  <td>
                    <Badge value={a.status} />
                  </td>
                  <td className="mono">{shortSha(a.commit_sha)}</td>
                  <td>{formatTime(a.created_at)}</td>
                  <td>
                    <button className="link" onClick={() => setSelected(a.id)}>
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </>
  );
}

function AnalyzeForm({ repoId, onQueued }: { repoId: string; onQueued: () => void }) {
  const readOnly = useReadOnly();
  const [ref, setRef] = useState("HEAD");
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await postJson(`/repositories/${repoId}/analyses`, { ref: ref.trim() || "HEAD" });
      onQueued();
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
    } finally {
      setBusy(false);
    }
  }
  if (readOnly === true) return <ReadOnlyNotice what="analyzing new revisions" />;
  return (
    <form className="form-row compact" onSubmit={submit}>
      <label className="narrow">
        Analyze revision
        <input value={ref} onChange={(e) => setRef(e.target.value)} aria-label="Revision to analyze" />
      </label>
      <button type="submit" disabled={busy || readOnly === null}>
        {busy ? "Queueing…" : "Analyze"}
      </button>
      {error && <ErrorPanel error={error} title="Could not queue the analysis" />}
    </form>
  );
}

function AnalysisDetail({ analysisId, onChange }: { analysisId: string; onChange: () => void }) {
  const { data, error, loading } = useApi<Analysis>(`/analyses/${analysisId}`, {
    pollWhile: (a) => !TERMINAL_ANALYSIS.has(a.status),
  });
  if (error) return <ErrorPanel error={error} />;
  if (loading && !data) return <Loading what="Loading analysis" />;
  if (!data) return null;
  const done = data.status === "SUCCEEDED" || data.status === "PARTIAL" || data.status === "REUSED";
  const counts = (data.counts ?? {}) as Record<string, unknown>;
  const references = (counts.references_by_status ?? {}) as Record<string, number>;
  const edges = (counts.edges_by_type ?? {}) as Record<string, number>;
  return (
    <>
      <Card
        title={
          <>
            Analysis of <code className="inline-code">{data.requested_ref}</code>{" "}
            <Badge value={data.status} />
          </>
        }
      >
        <KeyValues
          items={[
            ["Commit", <span key="c" className="mono">{data.snapshot?.commit_sha ?? "not resolved yet"}</span>],
            ["Resolved ref", data.resolved_ref ? `${data.resolved_ref} (${label(data.resolved_ref_kind)})` : "—"],
            ["Committed", formatTime(data.snapshot?.committed_at)],
            ["Parser", <span key="p" className="mono small">{data.parser_version}</span>],
            ["Job", data.job ? <Badge key="j" value={data.job.status} text={`${label(data.job.status)} (attempt ${data.job.attempts}/${data.job.max_attempts})`} /> : "—"],
            ["Duration", formatDuration(Object.values(data.timings_ms ?? {}).reduce<number>((s, v) => s + (typeof v === "number" ? v : 0), 0) || null)],
          ]}
        />
        {data.reused_run_id && (
          <p className="muted small">Results reused from an identical earlier analysis of the same commit.</p>
        )}
        {data.error && <div className="error-panel">{data.error}</div>}
        {!done && !data.error && <p className="muted">Fetching, inventorying and parsing… this page updates automatically.</p>}
      </Card>
      {done && data.snapshot && (
        <>
          <div className="stats">
            <Stat label="Files" value={data.snapshot.file_count ?? "—"} hint={formatBytes(data.snapshot.total_bytes)} />
            <Stat label="Python files" value={String(counts.python_files ?? "—")} />
            <Stat label="Symbols" value={String(counts.symbols ?? "—")} />
            <Stat label="References" value={String(counts.references ?? "—")} />
            <Stat
              label="Tests discovered"
              value={String(counts.tests_discovered ?? "—")}
              hint="static discovery"
            />
          </div>
          <div className="grid-2">
            <Card title="Languages">
              <Languages inventory={data.snapshot.language_inventory ?? {}} />
            </Card>
            <Card title="Reference resolution">
              <Distribution values={references} />
              <p className="muted small">
                Unresolved and dynamic references are reported rather than guessed; they can hide real dependencies.
              </p>
            </Card>
          </div>
          <div className="grid-2">
            <IndexingCompleteness analysisId={data.id} />
            <Card title="Dependency edges">
              <Distribution values={edges} />
            </Card>
          </div>
          <TestsCard analysisId={data.id} onChange={onChange} />
          <WarningsCard analysisId={data.id} issues={data.snapshot.issue_counts} />
        </>
      )}
    </>
  );
}

function Languages({ inventory }: { inventory: Record<string, number> }) {
  const entries = Object.entries(inventory).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, n]) => sum + n, 0);
  if (!total) return <Empty>No recognised languages.</Empty>;
  return (
    <ul className="bars">
      {entries.map(([language, count]) => (
        <li key={language}>
          <span className="bar-label">{language}</span>
          <span className="bar">
            <span style={{ width: `${(100 * count) / total}%` }} />
          </span>
          <span className="bar-value">{count}</span>
        </li>
      ))}
    </ul>
  );
}

function Distribution({ values }: { values: Record<string, number> }) {
  const entries = Object.entries(values);
  if (!entries.length) return <Empty>None recorded.</Empty>;
  return (
    <ul className="chips">
      {entries.map(([key, value]) => (
        <li key={key}>
          <Badge value={key} /> <strong>{value}</strong>
        </li>
      ))}
    </ul>
  );
}

function IndexingCompleteness({ analysisId }: { analysisId: string }) {
  const { data, error } = useApi<FilePage>(`/analyses/${analysisId}/files?limit=1`);
  const incomplete = useApi<FilePage>(`/analyses/${analysisId}/files?status=PARSED_WITH_ERRORS&limit=20`);
  return (
    <Card title="Indexing completeness">
      {error && <ErrorPanel error={error} />}
      {data && <Distribution values={data.status_counts} />}
      {incomplete.data && incomplete.data.items.length > 0 && (
        <>
          <p className="small">Files parsed with syntax errors (symbols near the errors may be missing):</p>
          <ul className="plain small mono">
            {incomplete.data.items.map((f) => (
              <li key={f.id}>
                {f.path} ({f.error_count} errors)
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}

function TestsCard({ analysisId, onChange }: { analysisId: string; onChange: () => void }) {
  const tests = useApi<Page<TestCase>>(`/analyses/${analysisId}/tests?limit=100`);
  const readOnly = useReadOnly();
  const [run, setRun] = useState<TestRun | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const counts: Record<string, number> = {};
  for (const t of tests.data?.items ?? []) counts[t.collection_status] = (counts[t.collection_status] ?? 0) + 1;
  async function collect() {
    setError(null);
    try {
      setRun(await postJson<TestRun>(`/analyses/${analysisId}/collect`));
      onChange();
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
    }
  }
  return (
    <Card
      title="Tests"
      actions={
        readOnly === true ? undefined : (
          <button className="secondary" onClick={collect} disabled={!!run || readOnly === null}>
            Confirm with sandboxed pytest collection
          </button>
        )
      }
    >
      {tests.data && tests.data.page.total === 0 && (
        <Empty>No pytest-style tests were found (test_*.py / *_test.py with test functions).</Empty>
      )}
      {tests.data && tests.data.page.total > 0 && (
        <>
          <p>
            Framework: <strong>pytest</strong> · {tests.data.page.total} tests discovered statically.
          </p>
          <Distribution values={counts} />
          <p className="muted small">
            Static discovery does not run repository code. Collection runs <code className="inline-code">pytest --collect-only</code> inside the sandbox.
          </p>
        </>
      )}
      {run && (
        <p>
          Collection queued: <Link href={`/test-runs/${run.id}`}>view run</Link>
        </p>
      )}
      {error && <ErrorPanel error={error} />}
    </Card>
  );
}

function WarningsCard({ analysisId, issues }: { analysisId: string; issues: Record<string, number> }) {
  const { data } = useApi<Page<Warning>>(`/analyses/${analysisId}/warnings?limit=50`);
  const hasIssues = Object.keys(issues).length > 0;
  if (!data || (data.page.total === 0 && !hasIssues)) return null;
  return (
    <Card title={`Warnings (${data.page.total})`}>
      {hasIssues && (
        <>
          <p className="small">Snapshot findings:</p>
          <Distribution values={issues} />
        </>
      )}
      <table className="table small">
        <tbody>
          {data.items.map((w) => (
            <tr key={w.id}>
              <td>
                <Badge value={w.severity === "ERROR" ? "FAILED" : "PARTIAL"} text={w.code} />
              </td>
              <td className="mono">
                {w.path ?? ""}
                {w.start_line ? `:${w.start_line}` : ""}
              </td>
              <td>{w.message}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}
