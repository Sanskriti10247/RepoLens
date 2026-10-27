"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

import { Badge, Card, Empty, ErrorPanel, Loading, SandboxNotice, When } from "@/components/ui";
import { formatDuration, label, shortSha } from "@/lib/format";
import type { Page, TestRun } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { useCapabilities } from "@/lib/useReadOnly";

export default function RunsPage() {
  const { id } = useParams<{ id: string }>();
  const { sandbox } = useCapabilities();
  const runs = useApi<Page<TestRun>>(`/test-runs?repository_id=${id}&limit=100`, {
    pollWhile: (page) => page.items.some((r) => r.status !== "COMPLETED"),
  });
  return (
    <Card title="Test runs">
      {sandbox === false && (
        <SandboxNotice>
          This instance has no Docker sandbox, so it cannot run tests. Runs recorded elsewhere still show up here.
        </SandboxNotice>
      )}
      {runs.error && <ErrorPanel error={runs.error} />}
      {runs.loading && !runs.data && <Loading what="Loading runs" />}
      {runs.data && runs.data.items.length === 0 && (
        <Empty>
          {sandbox === false
            ? "No test runs recorded for this repository."
            : "No test runs yet. Select tests from an impact analysis, or confirm the test list from the overview."}
        </Empty>
      )}
      {runs.data && runs.data.items.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Run</th>
              <th>Outcome</th>
              <th className="right">Passed</th>
              <th className="hide-sm">Commit</th>
              <th className="hide-sm right">Took</th>
              <th>When</th>
            </tr>
          </thead>
          <tbody>
            {runs.data.items.map((r) => (
              <tr key={r.id}>
                <td>
                  <Link href={`/test-runs/${r.id}`} className="row-title">
                    {label(r.mode)} run
                  </Link>
                  <span className="row-sub mono">{shortSha(r.id)}</span>
                </td>
                <td>{r.outcome ? <Badge value={r.outcome} /> : <Badge value={r.status} />}</td>
                <td className="right mono">
                  {r.counts ? `${r.counts.passed ?? 0} / ${r.counts.total ?? 0}` : "—"}
                </td>
                <td className="hide-sm">
                  <span className="sha" title={r.commit_sha}>
                    {shortSha(r.commit_sha)}
                  </span>
                </td>
                <td className="hide-sm right mono">{formatDuration(r.duration_ms)}</td>
                <td>
                  <When iso={r.created_at} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}
