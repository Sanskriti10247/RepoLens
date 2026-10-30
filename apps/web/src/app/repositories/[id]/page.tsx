"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { AnalysisPipeline } from "@/components/AnalysisPipeline";
import { HealthCard } from "@/components/HealthCard";
import { GraphCharts } from "@/components/GraphCharts";
import { ModuleArchitecture } from "@/components/ModuleGraph";
import { WithRail } from "@/components/SectionRail";
import {
  Badge,
  Card,
  Empty,
  ErrorPanel,
  FailureNote,
  KeyValues,
  Loading,
  ReadOnlyNotice,
  SandboxNotice,
  Stat,
  Stats,
  Tally,
  When,
} from "@/components/ui";
import { ApiError, postJson } from "@/lib/api";
import { TERMINAL_ANALYSIS, formatBytes, formatDuration, label, shortSha } from "@/lib/format";
import type { Analysis, AnalysisSummary, FilePage, Page, TestCase, TestRun, Warning } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { useCapabilities, useReadOnly } from "@/lib/useReadOnly";

const RAIL: [string, string][] = [
  ["analysis", "Analysis"],
  ["health", "Health"],
  ["architecture", "Architecture"],
  ["glance", "Graph at a glance"],
  ["languages", "Languages"],
  ["resolution", "Resolution"],
  ["completeness", "Indexing"],
  ["tests", "Tests"],
  ["warnings", "Warnings"],
  ["revisions", "Revisions"],
];

export default function RepositoryOverview() {
  const { id } = useParams<{ id: string }>();
  const analyses = useApi<Page<AnalysisSummary>>(`/repositories/${id}/analyses?limit=20`, {
    pollWhile: (page) => page.items.some((a) => !TERMINAL_ANALYSIS.has(a.status)),
  });
  const [selected, setSelected] = useState<string | null>(null);
  const current = selected ?? analyses.data?.items[0]?.id ?? null;

  return (
    <WithRail items={RAIL}>
      {analyses.error && <ErrorPanel error={analyses.error} />}
      {analyses.loading && !analyses.data && <Loading what="Loading analyses" shape="block" />}
      {analyses.data && analyses.data.items.length === 0 && (
        <Empty>No analysis has been recorded for this repository yet. Analyze a revision below to start.</Empty>
      )}
      {current && <AnalysisDetail analysisId={current} repoId={id} onChange={analyses.reload} />}
      <Card id="revisions" title="Revisions" actions={<AnalyzeForm repoId={id} onQueued={analyses.reload} />}>
        {analyses.data && analyses.data.items.length > 0 ? (
          <table className="table">
            <thead>
              <tr>
                <th>Requested</th>
                <th>Status</th>
                <th className="hide-sm">Commit</th>
                <th>When</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {analyses.data.items.map((a) => (
                <tr key={a.id} className={a.id === current ? "selected" : undefined}>
                  <td className="mono" title={a.requested_ref}>
                    {a.requested_ref.length > 24 ? shortSha(a.requested_ref) : a.requested_ref}
                  </td>
                  <td>
                    <Badge value={a.status} />
                  </td>
                  <td className="hide-sm">
                    <span className="sha" title={a.commit_sha ?? undefined}>
                      {shortSha(a.commit_sha)}
                    </span>
                  </td>
                  <td>
                    <When iso={a.created_at} />
                  </td>
                  <td className="right">
                    {a.id === current ? (
                      <span className="muted small">shown</span>
                    ) : (
                      <button className="link" onClick={() => setSelected(a.id)}>
                        View
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted small">Each analysis of a branch, tag or commit is listed here.</p>
        )}
      </Card>
    </WithRail>
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
    <>
      <form className="revision-form" onSubmit={submit}>
        <label>
          Analyze another revision
          <input
            value={ref}
            onChange={(e) => setRef(e.target.value)}
            aria-label="Revision to analyze"
            spellCheck={false}
          />
        </label>
        <button type="submit" className="secondary" disabled={busy || readOnly === null}>
          {busy ? "Queueing…" : "Analyze"}
        </button>
      </form>
      {error && <ErrorPanel error={error} title="Could not queue the analysis" />}
    </>
  );
}

function AnalysisDetail({
  analysisId,
  repoId,
  onChange,
}: {
  analysisId: string;
  repoId: string;
  onChange: () => void;
}) {
  const { data, error, loading } = useApi<Analysis>(`/analyses/${analysisId}`, {
    pollWhile: (a) => !TERMINAL_ANALYSIS.has(a.status),
    pollMs: 1000,
  });
  if (error) return <ErrorPanel error={error} />;
  if (loading && !data) return <Loading what="Loading analysis" shape="block" />;
  if (!data) return null;
  const done = data.status === "SUCCEEDED" || data.status === "PARTIAL" || data.status === "REUSED";
  const counts = (data.counts ?? {}) as Record<string, unknown>;
  const references = (counts.references_by_status ?? {}) as Record<string, number>;
  return (
    <>
      <Card
        id="analysis"
        title={
          <>
            Analysis of{" "}
            <span className="mono" title={data.requested_ref}>
              {data.requested_ref.length > 24 ? shortSha(data.requested_ref) : data.requested_ref}
            </span>{" "}
            <Badge value={data.status} />
          </>
        }
      >
        <KeyValues
          items={[
            [
              "Commit",
              <span key="c" className="sha" title={data.snapshot?.commit_sha}>
                {data.snapshot ? shortSha(data.snapshot.commit_sha) : "not resolved yet"}
              </span>,
            ],
            ["Resolved ref", data.resolved_ref ? `${data.resolved_ref} (${label(data.resolved_ref_kind)})` : "—"],
            ["Committed", <When key="w" iso={data.snapshot?.committed_at} />],
            ["Parser", <span key="p" className="mono small muted">{data.parser_version}</span>],
            ["Job", data.job ? <Badge key="j" value={data.job.status} text={`${label(data.job.status)} (attempt ${data.job.attempts}/${data.job.max_attempts})`} /> : "—"],
            ["Duration", formatDuration(Object.values(data.timings_ms ?? {}).reduce<number>((s, v) => s + (typeof v === "number" ? v : 0), 0) || null)],
          ]}
        />
        {data.reused_run_id && (
          <p className="hint">Results reused from an identical earlier analysis of the same commit.</p>
        )}
        {data.error && <FailureNote>{data.error}</FailureNote>}
        <AnalysisPipeline analysis={data} />
      </Card>
      {done && data.snapshot && (
        <>
          <Stats>
            <Stat label="Files" value={data.snapshot.file_count ?? "—"} hint={formatBytes(data.snapshot.total_bytes)} />
            <Stat label="Parsed files" value={num(counts.python_files)} />
            <Stat label="Symbols" value={num(counts.symbols)} />
            <Stat label="References" value={num(counts.references)} />
            <Stat
              label="Tests discovered"
              value={num(counts.tests_discovered)}
              hint="static discovery"
            />
          </Stats>
          <HealthCard analysisId={data.id} repoId={repoId} />
          <Card id="architecture" title="Architecture">
            <ModuleArchitecture analysisId={data.id} repoId={repoId} />
          </Card>
          <Card id="glance" title="Graph at a glance">
            <GraphCharts analysisId={data.id} repoId={repoId} />
          </Card>
          <div className="columns">
            <Card id="languages" title="Languages">
              <Languages inventory={data.snapshot.language_inventory ?? {}} />
            </Card>
            <Card id="resolution" title="Reference resolution">
              <Distribution values={references} />
              <p className="hint">
                Unresolved and dynamic references are reported rather than guessed; they can hide real dependencies.
              </p>
            </Card>
          </div>
          <IndexingCompleteness analysisId={data.id} />
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
  if (!total) return <Empty>No recognised source files.</Empty>;
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
  if (!entries.length) return <p className="muted">None recorded.</p>;
  return <Tally values={values} />;
}

function IndexingCompleteness({ analysisId }: { analysisId: string }) {
  const { data, error } = useApi<FilePage>(`/analyses/${analysisId}/files?limit=1`);
  const incomplete = useApi<FilePage>(`/analyses/${analysisId}/files?status=PARSED_WITH_ERRORS&limit=20`);
  return (
    <Card id="completeness" title="Indexing completeness">
      {error && <ErrorPanel error={error} />}
      {data && <Distribution values={data.status_counts} />}
      {incomplete.data && incomplete.data.items.length > 0 && (
        <>
          <h3>Parsed with syntax errors</h3>
          <p className="hint">Symbols near the errors may be missing.</p>
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
  const { readOnly, sandbox } = useCapabilities();
  const [run, setRun] = useState<TestRun | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const counts: Record<string, number> = {};
  for (const t of tests.data?.items ?? []) counts[t.collection_status] = (counts[t.collection_status] ?? 0) + 1;
  const hasTests = !!tests.data && tests.data.page.total > 0;
  const canCollect = readOnly === false && sandbox === true;
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
      id="tests"
      title="Tests"
      actions={
        readOnly === true || sandbox === false ? undefined : (
          <button className="secondary" onClick={collect} disabled={!!run || !canCollect}>
            Confirm with sandboxed pytest collection
          </button>
        )
      }
    >
      {tests.data && tests.data.page.total === 0 && (
        <Empty>No pytest-style tests were found (test_*.py or *_test.py files with test functions).</Empty>
      )}
      {hasTests && (
        <>
          <p>
            Framework: <strong>pytest</strong> · {tests.data?.page.total} tests discovered statically.
          </p>
          <Tally values={counts} />
          <p className="hint">
            Static discovery reads the files without running them.
            {sandbox !== false && (
              <>
                {" "}Collection runs <code className="inline-code">pytest --collect-only</code> inside the sandbox.
              </>
            )}
          </p>
          {readOnly === false && sandbox === false && (
            <SandboxNotice>
              Collection needs the Docker sandbox, which this instance does not have, so the static list above is
              what you get here.
            </SandboxNotice>
          )}
        </>
      )}
      {run && (
        <p>
          Collection queued. <Link href={`/test-runs/${run.id}`}>Follow the run</Link>
        </p>
      )}
      {error && <ErrorPanel error={error} title="Could not start collection" />}
    </Card>
  );
}

function WarningsCard({ analysisId, issues }: { analysisId: string; issues: Record<string, number> }) {
  const { data } = useApi<Page<Warning>>(`/analyses/${analysisId}/warnings?limit=50`);
  const hasIssues = Object.keys(issues).length > 0;
  if (!data || (data.page.total === 0 && !hasIssues)) return null;
  return (
    <Card id="warnings" title={<>Warnings <span className="count-chip num">{data.page.total}</span></>}>
      {hasIssues && (
        <>
          <h3>Snapshot findings</h3>
          <Distribution values={issues} />
        </>
      )}
      <table className="table small">
        <thead>
          <tr>
            <th>Code</th>
            <th>Where</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {data.items.map((w) => (
            <tr key={w.id}>
              <td className="nowrap">
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

function num(value: unknown): number | string {
  return typeof value === "number" ? value : "—";
}
