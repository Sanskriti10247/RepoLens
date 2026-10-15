"use client";

import { useEffect, useState } from "react";

import { ApiError, apiFetch, type Readiness } from "@/lib/api";

type State =
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "degraded"; reason: string }
  | { kind: "down"; reason: string };

export function ApiStatus({ pollMs = 15000 }: { pollMs?: number }) {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    async function check() {
      try {
        await apiFetch<Readiness>("/ready");
        if (!cancelled) setState({ kind: "ready" });
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 503) {
          setState({ kind: "degraded", reason: "API up, dependencies not ready" });
        } else {
          setState({ kind: "down", reason: "API unreachable" });
        }
      }
    }
    void check();
    const timer = setInterval(check, pollMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [pollMs]);

  const label = {
    loading: "Checking API…",
    ready: "API ready",
    degraded: state.kind === "degraded" ? state.reason : "",
    down: state.kind === "down" ? state.reason : "",
  }[state.kind];

  return (
    <span className={`status status-${state.kind}`} role="status" aria-live="polite">
      <span className="status-dot" aria-hidden="true" />
      {label}
    </span>
  );
}
