"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { Badge, Card, Empty, ErrorPanel, Loading, ReadOnlyNotice, When } from "@/components/ui";
import { ApiError, postJson } from "@/lib/api";
import { TERMINAL_CHANGE, label, shortSha } from "@/lib/format";
import type { ChangeSet, Page, Repository } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { useReadOnly } from "@/lib/useReadOnly";

export default function ImpactListPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const repo = useApi<Repository>(`/repositories/${id}`);
  const list = useApi<Page<ChangeSet>>(`/repositories/${id}/impact?limit=50`, {
    pollWhile: (page) => page.items.some((c) => !TERMINAL_CHANGE.has(c.status)),
  });
  const readOnly = useReadOnly();
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
      <Card title="Compare two revisions">
        {readOnly === true ? (
          <ReadOnlyNotice what="analyzing new changes" />
        ) : (
        <>
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
          <button type="submit" disabled={busy || readOnly === null || !base.trim() || !target.trim()}>
            {busy ? "Queueing…" : "Analyze impact"}
            {!busy && (
              <span className="arrow" aria-hidden="true">
                →
              </span>
            )}
          </button>
        </form>
        <p className="hint">
          Branch names, tags or commit ids. Both revisions are analyzed first if needed; the diff is then mapped
          to symbols and followed outwards through the dependency graph.
        </p>
        {error && <ErrorPanel error={error} title="Could not start the impact analysis" />}
        </>
        )}
      </Card>
      <Card title={list.data ? <>Impact analyses <span className="count-chip num">{list.data.page.total}</span></> : "Impact analyses"}>
        {list.error && <ErrorPanel error={list.error} />}
        {list.loading && !list.data && <Loading what="Loading impact analyses" shape="table" />}
        {list.data && list.data.items.length === 0 && (
          <Empty>No changes compared yet. Pick two revisions above, for example the default branch and a feature branch.</Empty>
        )}
        {list.data && list.data.items.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>Change</th>
                <th>Status</th>
                <th className="right hide-sm">Files</th>
                <th className="right hide-sm">Symbols</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {list.data.items.map((c) => (
                <tr key={c.id}>
                  <td>
                    <Link href={`/impact/${c.id}`} className="row-title row-link mono">
                      {refName(c.base_ref)} → {refName(c.target_ref)}
                      <span className="arrow" aria-hidden="true">
                        →
                      </span>
                    </Link>
                    <span className="row-sub mono">
                      {shortSha(c.base_sha)} → {shortSha(c.target_sha)}
                    </span>
                  </td>
                  <td>
                    <Badge
                      value={c.status}
                      text={TERMINAL_CHANGE.has(c.status) ? undefined : `${label(c.status)}…`}
                    />
                  </td>
                  <td className="right mono hide-sm">{String(c.summary?.changed_files ?? "—")}</td>
                  <td className="right mono hide-sm">{String(c.summary?.changed_symbols ?? "—")}</td>
                  <td>
                    <When iso={c.created_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}

/** Full commit ids are unreadable in a list; branch and tag names are kept as typed. */
function refName(ref: string): string {
  return /^[0-9a-f]{20,}$/.test(ref) ? shortSha(ref) : ref;
}
