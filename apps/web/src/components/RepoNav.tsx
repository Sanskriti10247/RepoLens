"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { Breadcrumbs } from "@/components/ui";
import { repoSlug } from "@/lib/format";
import type { Repository } from "@/lib/types";
import { useApi } from "@/lib/useApi";

const TABS: [string, string][] = [
  ["Overview", ""],
  ["Code explorer", "/explore"],
  ["Impact", "/impact"],
  ["Test runs", "/runs"],
  ["Ask", "/ask"],
];

export function RepoHeader({ id }: { id: string }) {
  const pathname = usePathname();
  const { data } = useApi<Repository>(`/repositories/${id}`);
  const base = `/repositories/${id}`;
  return (
    <div className="repo-header">
      <Breadcrumbs items={[["Repositories", "/"]]} />
      <h1>{data ? repoSlug(data.canonical_url) : "…"}</h1>
      <div className="repo-meta">
        {data && (
          <a href={data.canonical_url} target="_blank" rel="noreferrer">
            {data.canonical_url.replace(/^https?:\/\//, "")} ↗
          </a>
        )}
        {data?.default_branch && <span>default branch {data.default_branch}</span>}
      </div>
      <nav className="tabs" aria-label="Repository">
        {TABS.map(([name, suffix]) => {
          const href = base + suffix;
          const active = suffix ? pathname.startsWith(href) : pathname === base;
          return (
            <Link
              key={name}
              href={href}
              className={active ? "tab active" : "tab"}
              aria-current={active ? "page" : undefined}
            >
              {name}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
