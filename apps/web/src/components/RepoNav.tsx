"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { Badge, When } from "@/components/ui";
import { TERMINAL_ANALYSIS, label, repoSlug, shortSha } from "@/lib/format";
import type { Repository } from "@/lib/types";
import { useApi } from "@/lib/useApi";

const TABS: [string, string][] = [
  ["Overview", ""],
  ["Report", "/insights"],
  ["Code explorer", "/explore"],
  ["Impact", "/impact"],
  ["Test runs", "/runs"],
  ["Ask", "/ask"],
];

/** The repository's masthead: which repository, at which commit, and its sections. */
export function RepoHeader({ id }: { id: string }) {
  const pathname = usePathname();
  const { data } = useApi<Repository>(`/repositories/${id}`, {
    pollWhile: (repo) => !!repo.latest_analysis && !TERMINAL_ANALYSIS.has(repo.latest_analysis.status),
    pollMs: 3000,
  });
  const base = `/repositories/${id}`;
  const slug = data ? repoSlug(data.canonical_url) : null;
  const [owner, name] = slug && slug.includes("/") && !slug.includes(":") ? slug.split("/") : [null, slug];
  const latest = data?.latest_analysis ?? null;
  return (
    <div className="repo-header">
      <p className="eyebrow">
        <Link href="/">Repositories</Link>
        {data && <span>{data.host}</span>}
      </p>
      <div className="repo-title">
        <h1>
          {!data && <span className="muted">Loading…</span>}
          {owner && (
            <>
              <span className="owner">{owner}</span>
              <span className="owner-sep">/</span>
            </>
          )}
          {name}
        </h1>
      </div>
      <div className="repo-meta">
        {data && (
          <a href={data.canonical_url} target="_blank" rel="noreferrer">
            {data.canonical_url.replace(/^https?:\/\//, "")} ↗
          </a>
        )}
        {data?.default_branch && <span>branch {data.default_branch}</span>}
        {latest && (
          <span>
            <Badge value={latest.status} text={TERMINAL_ANALYSIS.has(latest.status) ? undefined : `${label(latest.status)}…`} />{" "}
            {latest.commit_sha && (
              <span className="sha" title={latest.commit_sha}>
                {shortSha(latest.commit_sha).slice(0, 7)}
              </span>
            )}{" "}
            <When iso={latest.finished_at ?? latest.created_at} />
          </span>
        )}
      </div>
      <div className="repo-tabs">
        <nav className="tabs" aria-label="Repository">
          {TABS.map(([title, suffix]) => {
            const href = base + suffix;
            const active = suffix ? pathname.startsWith(href) : pathname === base;
            return (
              <Link
                key={title}
                href={href}
                className={active ? "tab active" : "tab"}
                aria-current={active ? "page" : undefined}
              >
                {title}
              </Link>
            );
          })}
        </nav>
      </div>
    </div>
  );
}
