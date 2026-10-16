"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { Badge, Card, Empty, ErrorPanel, Loading } from "@/components/ui";
import { ApiError, postJson } from "@/lib/api";
import { TERMINAL_CHANGE, formatTime, shortSha } from "@/lib/format";
import type { ChangeSet, Page, Repository } from "@/lib/types";
import { useApi } from "@/lib/useApi";

export default function ImpactListPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const repo = useApi<Repository>(`/repositories/${id}`);
  const list = useApi<Page<ChangeSet>>(`/repositories/${id}/impact?limit=50`, {
    pollWhile: (page) => page.items.some((c) => !TERMINAL_CHANGE.has(c.status)),
  });
  const [base, setBase] = useState("");
  const [target, setTarget] = useState("HEAD");
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const change = await postJson<ChangeSet>(`/repositories/${id}/impact`, {
        base: base.trim(),
        target: target.trim(),
      });
      router.push(`/impact/${change.id}`);
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Card title="Analyze a change">
        <form className="form-row" onSubmit={submit}>
          <label className="narrow">
            Base revision
            <input
              required
              value={base}
              onChange={(e) => setBase(e.target.value)}
              placeholder={repo.data?.default_branch ?? "main"}
              aria-label="Base revision"
            />
          </label>
          <label className="narrow">
            Target revision
            <input required value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Target revision" />
          </label>
          <button type="submit" disabled={busy || !base.trim() || !target.trim()}>
            {busy ? "Queueing…" : "Analyze impact"}
          </button>
        </form>
        <p className="muted small">
          Branch names, tags or commit ids. Both revisions are analyzed first if needed, then the change is
          mapped to symbols and propagated through the dependency graph.
        </p>
        {error && <ErrorPanel error={error} title="Could not start the impact analysis" />}
      </Card>
      <Card title="Impact analyses">
        {list.error && <ErrorPanel error={list.error} />}
        {list.loading && !list.data && <Loading />}
        {list.data && list.data.items.length === 0 && <Empty>No impact analyses yet.</Empty>}
        {list.data && list.data.items.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>Change</th>
                <th>Status</th>
                <th>Files</th>
                <th>Symbols</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {list.data.items.map((c) => (
                <tr key={c.id}>
                  <td>
                    <Link href={`/impact/${c.id}`} className="mono">
                      {c.base_ref} ({shortSha(c.base_sha)}) → {c.target_ref} ({shortSha(c.target_sha)})
                    </Link>
                  </td>
                  <td>
                    <Badge value={c.status} />
                  </td>
                  <td>{String(c.summary?.changed_files ?? "—")}</td>
                  <td>{String(c.summary?.changed_symbols ?? "—")}</td>
                  <td>{formatTime(c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
