"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

import { Badge, Card, Empty, ErrorPanel, Loading } from "@/components/ui";
import { formatDuration, formatTime, label, shortSha } from "@/lib/format";
import type { Page, TestRun } from "@/lib/types";
import { useApi } from "@/lib/useApi";

export default function RunsPage() {
  const { id } = useParams<{ id: string }>();
  const runs = useApi<Page<TestRun>>(`/test-runs?repository_id=${id}&limit=100`, {
    pollWhile: (page) => page.items.some((r) => r.status !== "COMPLETED"),
  });
  return (
    <Card title="Test runs">
      {runs.error && <ErrorPanel error={runs.error} />}
      {runs.loading && !runs.data && <Loading />}
      {runs.data && runs.data.items.length === 0 && (
        <Empty>No test runs yet. Create a test plan from an impact analysis, or confirm tests from the overview.</Empty>
      )}
      {runs.data && runs.data.items.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Run</th>
              <th>Mode</th>
              <th>Outcome</th>
              <th>Tests</th>
              <th>Commit</th>
              <th>Duration</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {runs.data.items.map((r) => (
              <tr key={r.id}>
                <td>
                  <Link href={`/test-runs/${r.id}`} className="mono">
                    {shortSha(r.id)}
                  </Link>
                </td>
                <td>{label(r.mode)}</td>
                <td>{r.outcome ? <Badge value={r.outcome} /> : <Badge value={r.status} />}</td>
                <td>{r.counts ? `${r.counts.passed ?? 0} passed / ${r.counts.total ?? 0}` : "—"}</td>
                <td className="mono">{shortSha(r.commit_sha)}</td>
                <td>{formatDuration(r.duration_ms)}</td>
                <td>{formatTime(r.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}
