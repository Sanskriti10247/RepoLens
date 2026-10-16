"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

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
      <div className="repo-title">
        <Link href="/" className="muted">
          Repositories
        </Link>
        <span className="sep">/</span>
        <strong>{data ? `${data.owner}/${data.name.split("/").pop()}` : "…"}</strong>
      </div>
      <nav className="tabs" aria-label="Repository">
        {TABS.map(([name, suffix]) => {
          const href = base + suffix;
          const active = suffix ? pathname.startsWith(href) : pathname === base;
          return (
            <Link key={name} href={href} className={active ? "tab active" : "tab"}>
              {name}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
