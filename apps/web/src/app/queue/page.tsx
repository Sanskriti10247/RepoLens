"use client";

import { Card, ErrorPanel, KeyValues, Loading, Stat, Stats, Tally } from "@/components/ui";
import { formatDuration, label } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { useCapabilities } from "@/lib/useReadOnly";

interface KindMetrics {
  kind: string;
  by_status: Record<string, number>;
  completed_last_24h: number;
  median_queue_wait_ms: number | null;
  p95_queue_wait_ms: number | null;
  median_run_ms: number | null;
  p95_run_ms: number | null;
  retried_jobs: number;
  failure_categories: Record<string, number>;
}

interface QueueMetrics {
  kinds: KindMetrics[];
  active_leases: number;
  expired_leases: number;
}

const KIND_NOTES: Record<string, string> = {
  ANALYZE_REPOSITORY: "Fetching a revision and parsing it into symbols and references.",
  ANALYZE_CHANGE: "Diffing two revisions and following the change through the graph.",
  RUN_TESTS: "Running selected tests, or pytest collection, inside the sandbox.",
};

export default function QueuePage() {
  const { data, error, loading } = useApi<QueueMetrics>("/metrics/queue", {
    pollWhile: () => true,
    pollMs: 5000,
  });
  const { sandbox } = useCapabilities();
  return (
    <>
      <div className="page-head">
        <h1>Job queue</h1>
        <p className="lede">
          Work is queued in Postgres and picked up by a worker under a lease. If a worker dies, its lease expires
          and the job is retried. Figures refresh every few seconds.
        </p>
      </div>
      {error && <ErrorPanel error={error} />}
      {loading && !data && <Loading what="Loading queue figures" />}
      {data && (
        <>
          <Stats>
            <Stat label="Active leases" value={data.active_leases} hint="jobs being worked on now" />
            <Stat label="Expired leases" value={data.expired_leases} hint="awaiting recovery" />
            <Stat
              label="Done in 24 h"
              value={data.kinds.reduce((sum, k) => sum + k.completed_last_24h, 0)}
              hint="all job kinds"
            />
          </Stats>
          {data.kinds.map((k) => {
            const statuses = Object.keys(k.by_status).length > 0;
            return (
              <Card key={k.kind} title={label(k.kind)}>
                <p className="hint queue-note">
                  {KIND_NOTES[k.kind] ?? ""}
                  {k.kind === "RUN_TESTS" && sandbox === false && " This instance has no sandbox, so none will run here."}
                </p>
                <div className="columns">
                  <div>
                    <h3>By status</h3>
                    {statuses ? <Tally values={k.by_status} /> : <p className="muted">No jobs of this kind yet.</p>}
                  </div>
                  <KeyValues
                    items={[
                      ["Completed (24 h)", k.completed_last_24h],
                      ["Queue wait p50 / p95", <span key="q" className="mono">{`${formatDuration(k.median_queue_wait_ms)} / ${formatDuration(k.p95_queue_wait_ms)}`}</span>],
                      ["Run time p50 / p95", <span key="r" className="mono">{`${formatDuration(k.median_run_ms)} / ${formatDuration(k.p95_run_ms)}`}</span>],
                      ["Retried (24 h)", k.retried_jobs],
                      ["Failures (24 h)", Object.entries(k.failure_categories).map(([c, n]) => `${label(c)}: ${n}`).join(", ") || "none"],
                    ]}
                  />
                </div>
              </Card>
            );
          })}
        </>
      )}
    </>
  );
}
