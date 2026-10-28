"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useCapabilities } from "@/lib/useReadOnly";

const REPOSITORY_PAGES = ["/repositories", "/impact", "/test-plans", "/test-runs"];

export function SiteNav() {
  const pathname = usePathname() ?? "/";
  const inRepositories = pathname === "/" || REPOSITORY_PAGES.some((p) => pathname.startsWith(p));
  const items: [string, string, boolean][] = [
    ["Repositories", "/", inRepositories],
    ["Job queue", "/queue", pathname.startsWith("/queue")],
  ];
  return (
    <nav className="site-nav" aria-label="Primary">
      {items.map(([text, href, active]) => (
        <Link key={href} href={href} className={active ? "active" : undefined} aria-current={active ? "page" : undefined}>
          {text}
        </Link>
      ))}
      <a href="/api/docs" target="_blank" rel="noreferrer">
        API docs<span className="ext" aria-hidden="true">↗</span>
      </a>
    </nav>
  );
}

/** One line on what this instance can and cannot do, so missing buttons are not a mystery. */
export function InstanceNote() {
  const { readOnly, sandbox } = useCapabilities();
  if (readOnly === true) return <p className="instance-note">Read-only demo. Recorded analyses only.</p>;
  if (sandbox === false) return <p className="instance-note">No test sandbox on this instance. Analysis and impact work; test runs do not.</p>;
  return null;
}
