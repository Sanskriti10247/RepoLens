"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { SystemDiagram } from "@/components/SystemDiagram";
import { Badge, Card, Empty, ErrorPanel, Loading, ReadOnlyNotice, When } from "@/components/ui";
import { ApiError, postJson } from "@/lib/api";
import { TERMINAL_ANALYSIS, label, repoSlug, shortSha } from "@/lib/format";
import type { Page, Repository } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { useReadOnly } from "@/lib/useReadOnly";

export default function RepositoriesPage() {
  const { data, error, loading, reload } = useApi<Page<Repository>>("/repositories?limit=100", {
    pollWhile: (page) =>
      page.items.some((r) => r.latest_analysis && !TERMINAL_ANALYSIS.has(r.latest_analysis.status)),
    pollMs: 3000,
  });
  return (
    <>
      <section className="home-head" aria-labelledby="home-title">
        <h1 id="home-title">Analyze a repository</h1>
        <p className="lede">
          Symbols, imports and calls as a graph, a health report, the impact of a change and the tests that cover it.
        </p>
        <RegisterForm onRegistered={reload} />
      </section>
      <Card title={data ? <>Repositories <span className="count-chip num">{data.page.total}</span></> : "Repositories"}>
        {loading && !data && <Loading what="Loading repositories" shape="table" rows={3} />}
        {error && <ErrorPanel error={error} />}
        {data && data.items.length === 0 && (
          <Empty>Nothing analyzed yet.</Empty>
        )}
        {data && data.items.length > 0 && (
          <table className="table repo-table">
            <thead>
              <tr>
                <th>Repository</th>
                <th>Latest analysis</th>
                <th className="hide-sm">Commit</th>
                <th className="hide-sm right">Added</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((repo) => {
                const latest = repo.latest_analysis;
                const running = latest && !TERMINAL_ANALYSIS.has(latest.status);
                return (
                  <tr key={repo.id}>
                    <td>
                      <Link href={`/repositories/${repo.id}`} title={repo.canonical_url} className="row-title row-link">
                        {repoSlug(repo.canonical_url)}
                      </Link>
                      <span className="row-sub">
                        {repo.host}
                        {repo.default_branch ? ` · ${repo.default_branch}` : ""}
                      </span>
                    </td>
                    <td>
                      {latest ? (
                        <Badge value={latest.status} text={running ? `${label(latest.status)}…` : undefined} />
                      ) : (
                        <span className="muted">Not yet</span>
                      )}
                      {latest && (
                        <span className="row-sub">
                          <When iso={latest.finished_at ?? latest.created_at} />
                        </span>
                      )}
                    </td>
                    <td className="hide-sm">
                      <span className="sha" title={latest?.commit_sha ?? undefined}>
                        {shortSha(latest?.commit_sha)}
                      </span>
                    </td>
                    <td className="hide-sm right">
                      <When iso={repo.created_at} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Card>
      <details className="home-diagram">
        <summary>How the pieces fit together</summary>
        <SystemDiagram />
      </details>
    </>
  );
}

function RegisterForm({ onRegistered }: { onRegistered: () => void }) {
  const router = useRouter();
  const readOnly = useReadOnly();
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("HEAD");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const repo = await postJson<Repository>("/repositories", { url: url.trim() });
      await postJson(`/repositories/${repo.id}/analyses`, { ref: ref.trim() || "HEAD" });
      onRegistered();
      router.push(`/repositories/${repo.id}`);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : null);
    } finally {
      setBusy(false);
    }
  }

  if (readOnly === true) {
    return (
      <div className="command">
        <ReadOnlyNotice what="registering and analyzing repositories" />
      </div>
    );
  }
  return (
    <div className="command">
      <form className="form-row" onSubmit={submit} aria-label="Analyze a repository">
        <label>
          Repository URL
          <input
            required
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://github.com/owner/name"
            aria-label="Repository URL"
            spellCheck={false}
            autoComplete="off"
          />
        </label>
        <label className="narrow">
          Branch, tag or commit
          <input value={ref} onChange={(e) => setRef(e.target.value)} aria-label="Revision" spellCheck={false} />
        </label>
        <button type="submit" disabled={busy || readOnly === null || !url.trim()}>
          {busy ? "Submitting…" : "Register and analyze"}
        </button>
      </form>
      {error && <ErrorPanel error={error} title="Could not register the repository" />}
    </div>
  );
}
