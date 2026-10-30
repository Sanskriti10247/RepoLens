"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { WithRail } from "@/components/SectionRail";
import { Badge, Card, Empty, ErrorPanel, KeyValues, Loading, Progress, Stat, Stats } from "@/components/ui";
import { ApiError, postJson } from "@/lib/api";
import { formatDuration, formatTime, label, shortSha } from "@/lib/format";
import type { CoverageSummary, Page, RunLogs, TestResult, TestRun } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { useReadOnly } from "@/lib/useReadOnly";

const EXPLANATIONS: Record<string, string> = {
  PASSED: "pytest exited 0 and the report shows no failures or errors.",
  FAILED: "At least one test failed or errored.",
  TIMED_OUT: "The run exceeded its wall-clock limit and the container was killed.",
  CANCELLED: "The run was cancelled; no outcome was recorded for unfinished tests.",
  INFRASTRUCTURE_ERROR: "The sandbox did not produce a usable report. This is not a test result.",
  COLLECTION_ERROR: "pytest could not collect the requested tests (import error, usage error or no tests).",
  SECURITY_POLICY_BLOCKED: "The request violated the execution policy and was not run.",
  RESOURCE_LIMIT_EXCEEDED: "A memory, process or workspace limit was reached.",
};

const RAIL: [string, string][] = [
  ["details", "Details"],
  ["results", "Results"],
  ["coverage", "Coverage"],
  ["output", "Output"],
];

export default function TestRunPage() {
  const { id } = useParams<{ id: string }>();
  const run = useApi<TestRun>(`/test-runs/${id}`, {
    pollWhile: (r) => r.status !== "COMPLETED",
    pollMs: 1000,
  });
  const [error, setError] = useState<ApiError | null>(null);
  const readOnly = useReadOnly();
  if (run.error) return <ErrorPanel error={run.error} />;
  if (!run.data) return <Loading what="Loading test run" shape="block" />;
  const r = run.data;
  const completed = r.status === "COMPLETED";

  async function cancel() {
    setError(null);
    try {
      await postJson(`/test-runs/${id}/cancel`);
      run.reload();
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
    }
  }

  return (
    <>
      <p className="breadcrumbs">
        <Link href={`/repositories/${r.repository_id}`}>Repository</Link>
        <span className="sep">/</span>
        <Link href={`/repositories/${r.repository_id}/runs`}>Test runs</Link>
        {r.plan_id && (
          <>
            <span className="sep">/</span>
            <Link href={`/test-plans/${r.plan_id}`}>Plan</Link>
          </>
        )}
      </p>
      <div className="page-head page-head-wide">
        <h1>
          {label(r.mode)} run{" "}
          {completed ? <Badge value={r.outcome} /> : <Badge value={r.status} />}
        </h1>
      </div>
      <WithRail items={RAIL}>
      <Card
        id="details"
        title="Details"
        actions={
          !completed && readOnly !== true && (
            <button className="danger" onClick={cancel} disabled={readOnly === null}>
              Cancel run
            </button>
          )
        }
      >
        {completed && r.outcome && (
          <p>
            <strong>{label(r.outcome)}.</strong> {EXPLANATIONS[r.outcome]}{" "}
            {r.outcome_reason && <span className="muted">({r.outcome_reason})</span>}
          </p>
        )}
        {!completed && (
          <Progress
            doing={r.status === "QUEUED" ? "Waiting for a worker" : "Running in the sandbox"}
            since={r.status === "QUEUED" ? r.created_at : (r.started_at ?? r.created_at)}
            detail="this page refreshes by itself"
          />
        )}
        <KeyValues
          items={[
            ["Commit", <span key="c" className="sha" title={r.commit_sha}>{shortSha(r.commit_sha)}</span>],
            ["Tests requested", r.mode === "FULL_SUITE" ? "entire suite" : r.mode === "COLLECT" ? "collection only" : String(r.requested_node_ids.length)],
            ["Duration", formatDuration(r.duration_ms)],
            ["pytest exit code", r.pytest_exit_code ?? "—"],
            ["Container exit code", r.container_exit_code ?? "—"],
            ["Sandbox", `${r.image} · ${String(r.limits.memory_mb)} MB · ${String(r.limits.cpus)} CPU · ${String(r.limits.timeout_seconds)} s · network ${String(r.limits.network)}`],
            ["Cleanup", r.cleanup_ok === null ? "—" : r.cleanup_ok ? "container removed" : "container removal failed"],
            ["Job", r.job ? `${label(r.job.status)} · attempt ${r.job.attempts}/${r.job.max_attempts}${r.job.error_category ? ` · ${r.job.error_category}` : ""}` : "—"],
            ["Finished", formatTime(r.finished_at)],
          ]}
        />
        {error && <ErrorPanel error={error} title="Could not cancel" />}
      </Card>
      {completed && r.counts && (
        <Stats>
          <Stat label="Passed" value={r.counts.passed ?? 0} />
          <Stat label="Failed" value={r.counts.failed ?? 0} />
          <Stat label="Errors" value={r.counts.error ?? 0} />
          <Stat label="Skipped" value={r.counts.skipped ?? 0} />
        </Stats>
      )}
      {completed && <Results runId={id} />}
      {completed && r.coverage && <Coverage runId={id} />}
      {completed && <Logs runId={id} />}
      </WithRail>
    </>
  );
}

function Results({ runId }: { runId: string }) {
  const { data } = useApi<Page<TestResult>>(`/test-runs/${runId}/results?limit=100`);
  if (!data) return null;
  if (data.items.length === 0) return <Card id="results" title="Results"><Empty>No per-test results were reported.</Empty></Card>;
  const order: Record<string, number> = { FAILED: 0, ERROR: 1, SKIPPED: 2, PASSED: 3 };
  const sorted = [...data.items].sort((a, b) => (order[a.outcome] ?? 9) - (order[b.outcome] ?? 9));
  return (
    <Card id="results" title={<>Results <span className="count-chip num">{data.page.total}</span></>}>
      <table className="table small">
        <thead>
          <tr>
            <th>Outcome</th>
            <th>Test</th>
            <th className="right">Took</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((result) => (
            <tr key={result.id}>
              <td className="nowrap">
                <Badge value={result.outcome} />
              </td>
              <td className="mono">
                {result.node_id}
                {result.message && <span className="row-sub wrap">{result.message}</span>}
              </td>
              <td className="mono right nowrap">{formatDuration(result.duration_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function Coverage({ runId }: { runId: string }) {
  const { data } = useApi<CoverageSummary>(`/test-runs/${runId}/coverage`);
  if (!data) return null;
  return (
    <Card id="coverage" title="Coverage evidence">
      {data.files.length === 0 ? (
        <Empty>No coverage was recorded.</Empty>
      ) : (
        <table className="table small">
          <thead>
            <tr>
              <th>File</th>
              <th className="right">Lines executed</th>
              <th className="right">Tests</th>
            </tr>
          </thead>
          <tbody>
            {data.files.map((f) => (
              <tr key={f.path}>
                <td className="mono">{f.path}</td>
                <td className="mono right">{f.lines_covered}</td>
                <td className="mono right">{f.tests}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="hint">{data.note}</p>
    </Card>
  );
}

function Logs({ runId }: { runId: string }) {
  const { data } = useApi<RunLogs>(`/test-runs/${runId}/logs`);
  if (!data) return null;
  return (
    <Card id="output" title="Output">
      {data.output_truncated && <p className="warn-text small">Output exceeded its limit and was truncated.</p>}
      <h3>pytest output (tail)</h3>
      <pre className="log">{data.stdout_tail || "(empty)"}</pre>
      {data.stderr_tail && (
        <>
          <h3>stderr (tail)</h3>
          <pre className="log">{data.stderr_tail}</pre>
        </>
      )}
      <p className="hint">{data.note}</p>
    </Card>
  );
}
