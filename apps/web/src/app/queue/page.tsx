"use client";

import { Badge, Card, ErrorPanel, KeyValues, Loading } from "@/components/ui";
import { formatDuration, label } from "@/lib/format";
import { useApi } from "@/lib/useApi";

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

export default function QueuePage() {
  const { data, error, loading } = useApi<QueueMetrics>("/metrics/queue", {
    pollWhile: () => true,
    pollMs: 5000,
  });
  return (
    <>
      <h1>Job queue</h1>
      {error && <ErrorPanel error={error} />}
      {loading && !data && <Loading />}
      {data && (
        <>
          <Card>
            <KeyValues
              items={[
                ["Active leases", data.active_leases],
                ["Expired leases (awaiting recovery)", data.expired_leases],
              ]}
            />
          </Card>
          {data.kinds.map((k) => (
            <Card key={k.kind} title={label(k.kind)}>
              <ul className="chips">
                {Object.entries(k.by_status).map(([status, n]) => (
                  <li key={status}>
                    <Badge value={status} /> <strong>{n}</strong>
                  </li>
                ))}
              </ul>
              <KeyValues
                items={[
                  ["Completed (24 h)", k.completed_last_24h],
                  ["Queue wait p50 / p95", `${formatDuration(k.median_queue_wait_ms)} / ${formatDuration(k.p95_queue_wait_ms)}`],
                  ["Run time p50 / p95", `${formatDuration(k.median_run_ms)} / ${formatDuration(k.p95_run_ms)}`],
                  ["Retried jobs (24 h)", k.retried_jobs],
                  ["Failure categories (24 h)", Object.entries(k.failure_categories).map(([c, n]) => `${c}: ${n}`).join(", ") || "none"],
                ]}
              />
            </Card>
          ))}
        </>
      )}
    </>
  );
}
