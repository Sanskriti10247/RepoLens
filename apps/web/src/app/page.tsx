"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Badge, Card, Empty, ErrorPanel, Loading, ReadOnlyNotice } from "@/components/ui";
import { ApiError, postJson } from "@/lib/api";
import { formatTime, shortSha } from "@/lib/format";
import type { Page, Repository } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { useReadOnly } from "@/lib/useReadOnly";

export default function RepositoriesPage() {
  const { data, error, loading, reload } = useApi<Page<Repository>>("/repositories?limit=100");
  return (
    <>
      <h1>Repositories</h1>
      <RegisterForm onRegistered={reload} />
      <Card title="Registered repositories">
        {loading && !data && <Loading />}
        {error && <ErrorPanel error={error} />}
        {data && data.items.length === 0 && (
          <Empty>No repositories yet. Register a public GitHub repository above.</Empty>
        )}
        {data && data.items.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>Repository</th>
                <th>Default branch</th>
                <th>Latest analysis</th>
                <th>Commit</th>
                <th>Registered</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((repo) => (
                <tr key={repo.id}>
                  <td>
                    <Link href={`/repositories/${repo.id}`}>{repo.canonical_url}</Link>
                  </td>
                  <td>{repo.default_branch ?? "—"}</td>
                  <td>
                    {repo.latest_analysis ? <Badge value={repo.latest_analysis.status} /> : "Never"}
                  </td>
                  <td className="mono">{shortSha(repo.latest_analysis?.commit_sha)}</td>
                  <td>{formatTime(repo.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
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
      <Card title="Analyze a repository">
        <ReadOnlyNotice what="registering and analyzing repositories" />
      </Card>
    );
  }
  return (
    <Card title="Analyze a repository">
      <form className="form-row" onSubmit={submit}>
        <label>
          Repository URL
          <input
            required
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://github.com/owner/name"
            aria-label="Repository URL"
          />
        </label>
        <label className="narrow">
          Branch, tag or commit
          <input value={ref} onChange={(e) => setRef(e.target.value)} aria-label="Revision" />
        </label>
        <button type="submit" disabled={busy || readOnly === null || !url.trim()}>
          {busy ? "Submitting…" : "Register and analyze"}
        </button>
      </form>
      <p className="muted small">
        Repositories are fetched into a bare mirror and parsed without executing any of their code.
      </p>
      {error && <ErrorPanel error={error} title="Could not register the repository" />}
    </Card>
  );
}
