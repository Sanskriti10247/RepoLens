import Link from "next/link";
import type { ReactNode } from "react";

import type { ApiError } from "@/lib/api";
import { label, toneFor } from "@/lib/format";

export function Badge({ value, text }: { value: string | null | undefined; text?: string }) {
  return <span className={`badge tone-${toneFor(value)}`}>{text ?? label(value)}</span>;
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return (
    <p className="muted" role="status">
      {what}…
    </p>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorPanel({ error, title = "Request failed" }: { error: ApiError; title?: string }) {
  return (
    <div className="error-panel" role="alert">
      <strong>{title}</strong>
      <p>{error.message}</p>
      <p className="muted small">
        {error.code}
        {error.requestId ? ` · request ${error.requestId}` : ""}
      </p>
    </div>
  );
}

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
    <section className="card">
      {(title || actions) && (
        <header className="card-header">
          {title && <h2>{title}</h2>}
          {actions && <div className="card-actions">{actions}</div>}
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

export function Stat({ label: name, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
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
