"use client";

import Link from "next/link";
import { useEffect, useId, useState, type ReactNode } from "react";

import type { ApiError } from "@/lib/api";
import { formatClock, formatRelative, formatTime, label, toneFor } from "@/lib/format";
import { useCountUp } from "@/lib/motion";

/** A status as a small square and plain text; colour is a hint, the word carries the meaning. */
export function Badge({ value, text }: { value: string | null | undefined; text?: string }) {
  return (
    <span className={`badge tone-${toneFor(value)}`}>
      <span className="badge-mark" aria-hidden="true" />
      {text ?? label(value)}
    </span>
  );
}

type SkeletonShape = "lines" | "table" | "stats" | "block";

/**
 * Work in flight, drawn as the shape of what will replace it (a table, a row of figures, a
 * chart) so the page does not jump when the data lands. The words are for screen readers.
 */
export function Loading({ what = "Loading", shape = "lines", rows = 3 }: { what?: string; shape?: SkeletonShape; rows?: number }) {
  return (
    <div className={`skeleton skeleton-${shape}`} role="status">
      <span className="sr-only">{what}…</span>
      <SkeletonShapes shape={shape} rows={rows} />
    </div>
  );
}

function SkeletonShapes({ shape, rows }: { shape: SkeletonShape; rows: number }) {
  if (shape === "block") return <span className="sk sk-block" aria-hidden="true" />;
  if (shape === "stats") {
    return (
      <span className="sk-stats" aria-hidden="true">
        {Array.from({ length: rows }, (_, i) => (
          <span key={i} className="sk" />
        ))}
      </span>
    );
  }
  if (shape === "table") {
    return (
      <>
        {Array.from({ length: rows }, (_, i) => (
          <span key={i} className="sk-row" aria-hidden="true">
            <span className="sk" style={{ width: `${88 - ((i * 17) % 40)}%` }} />
            <span className="sk" style={{ width: "60%" }} />
            <span className="sk" style={{ width: "40%" }} />
          </span>
        ))}
      </>
    );
  }
  return (
    <>
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} className="sk sk-line" aria-hidden="true" style={{ width: `${92 - ((i * 23) % 45)}%` }} />
      ))}
    </>
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

/** A section of a page: a numbered, ruled heading and its content. No box. */
export function Card({
  title,
  actions,
  children,
  id,
  className,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  /** Anchor for the page rail. */
  id?: string;
  className?: string;
}) {
  return (
    <section className={className ? `section ${className}` : "section"} id={id}>
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
      <div className="stat-value">{typeof value === "number" ? <Ticker value={value} /> : value}</div>
      <div className="stat-label">{name}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

/**
 * A number that counts up from zero when it first appears and glides to new values after.
 * Assistive technology reads the final value only.
 */
export function Ticker({ value, decimals = 0, suffix = "" }: { value: number; decimals?: number; suffix?: string }) {
  const shown = useCountUp(value);
  const format = (n: number) =>
    n.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) + suffix;
  return (
    <span className="ticker num">
      <span aria-hidden="true">{format(shown)}</span>
      <span className="sr-only">{format(value)}</span>
    </span>
  );
}

/** The title block of a top-level page: a small running head, the title and a lede. */
export function PageHead({ eyebrow, title, lede, children }: { eyebrow?: ReactNode; title: ReactNode; lede?: ReactNode; children?: ReactNode }) {
  return (
    <div className="page-head">
      {eyebrow && <p className="eyebrow">{eyebrow}</p>}
      <h1>{title}</h1>
      {lede && <p className="lede">{lede}</p>}
      {children}
    </div>
  );
}

/**
 * A term with a short explanation that opens on hover, keyboard focus or a tap. The
 * explanation is also the term's accessible description.
 */
export function Tip({ children, tip }: { children: ReactNode; tip: ReactNode }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  return (
    <span className={open ? "tip is-open" : "tip"}>
      <button
        type="button"
        className="tip-term"
        aria-describedby={id}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        onBlur={() => setOpen(false)}
        onKeyDown={(e) => e.key === "Escape" && setOpen(false)}
      >
        {children}
      </button>
      <span role="tooltip" id={id} className="tip-body">
        {tip}
      </span>
    </span>
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
