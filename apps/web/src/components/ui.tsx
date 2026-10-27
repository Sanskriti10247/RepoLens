"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

import type { ApiError } from "@/lib/api";
import { formatClock, formatRelative, formatTime, label, toneFor } from "@/lib/format";

/** A status as a small square and plain text; colour is a hint, the word carries the meaning. */
export function Badge({ value, text }: { value: string | null | undefined; text?: string }) {
  return (
    <span className={`badge tone-${toneFor(value)}`}>
      <span className="badge-mark" aria-hidden="true" />
      {text ?? label(value)}
    </span>
  );
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return (
    <p className="loading" role="status">
      {what}…
    </p>
  );
}

/** Shown in place of an action that a read-only instance refuses. */
export function ReadOnlyNotice({ what }: { what: string }) {
  return (
    <p className="notice" role="note">
      This is a read-only demo of recorded analyses, so {what} is turned off.
    </p>
  );
}

/** Shown in place of an action that needs the Docker sandbox, on an instance without one. */
export function SandboxNotice({ children }: { children: ReactNode }) {
  return (
    <p className="notice" role="note">
      {children}
    </p>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function ErrorPanel({ error, title = "Request failed" }: { error: ApiError; title?: string }) {
  return (
    <div className="error-note" role="alert">
      <strong>{title}</strong>
      <p>{error.message}</p>
      <p className="error-meta">
        {error.code}
        {error.requestId ? ` · request ${error.requestId}` : ""}
      </p>
    </div>
  );
}

/** A plain-text failure recorded on a job, shown the same quiet way as a request error. */
export function FailureNote({ children }: { children: ReactNode }) {
  return <div className="error-note">{children}</div>;
}

/** A section of a page: a ruled heading and its content. No box. */
export function Card({
  title,
  actions,
  children,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="section">
      {(title || actions) && (
        <header className="section-head">
          {title && <h2>{title}</h2>}
          {actions && <div className="section-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function KeyValues({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Stats({ children }: { children: ReactNode }) {
  return <div className="stats">{children}</div>;
}

export function Stat({ label: name, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{typeof value === "number" ? value.toLocaleString("en-US") : value}</div>
      <div className="stat-label">{name}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

export function Breadcrumbs({ items }: { items: [string, string | null][] }) {
  return (
    <nav className="breadcrumbs" aria-label="Breadcrumb">
      {items.map(([text, href], index) => (
        <span key={`${text}-${index}`}>
          {index > 0 && <span className="sep">/</span>}
          {href ? <Link href={href}>{text}</Link> : <span>{text}</span>}
        </span>
      ))}
    </nav>
  );
}

export function Code({ children }: { children: ReactNode }) {
  return <code className="inline-code">{children}</code>;
}

/** A relative time with the exact local time on hover. */
export function When({ iso }: { iso: string | null | undefined }) {
  if (!iso) return <span className="muted">—</span>;
  return (
    <time dateTime={iso} title={formatTime(iso)} className="when">
      {formatRelative(iso)}
    </time>
  );
}

function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/**
 * A calm single line for work in flight: "Analysing… 0:42", with what is happening now and
 * a note that the page keeps itself up to date.
 */
export function Progress({
  doing,
  since,
  detail,
}: {
  doing: string;
  since: string | null | undefined;
  detail?: ReactNode;
}) {
  const now = useNow();
  const elapsed = since ? formatClock(now - new Date(since).getTime()) : null;
  return (
    <div className="progress" role="status">
      <span className="progress-mark" aria-hidden="true" />
      <span className="progress-doing">{doing}…</span>
      {elapsed && (
        <span className="progress-clock" aria-hidden="true">
          {elapsed}
        </span>
      )}
      {detail && <span className="progress-detail">{detail}</span>}
    </div>
  );
}

/** Counts per status as a dotted-leader list: "■ Resolved ........ 1204". */
export function Tally({ values }: { values: Record<string, number> }) {
  const entries = Object.entries(values);
  return (
    <ul className="tally">
      {entries.map(([key, count]) => (
        <li key={key}>
          <Badge value={key} />
          <span className="leader" aria-hidden="true" />
          <span className="count">{count}</span>
        </li>
      ))}
    </ul>
  );
}
