"use client";

import { useEffect, useState } from "react";

import { ApiError, apiFetch, type Readiness } from "@/lib/api";

type State =
  | { kind: "loading" }
  | { kind: "waking" }
  | { kind: "ready" }
  | { kind: "degraded"; reason: string }
  | { kind: "down"; reason: string };

/** ``wakeMs``: how long after page load a failure is shown as "waking" rather than "down". */
export function ApiStatus({ pollMs = 15000, wakeMs = 180_000 }: { pollMs?: number; wakeMs?: number }) {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const started = Date.now();
    let everReady = false;
    async function check() {
      let ready = false;
      try {
        await apiFetch<Readiness>("/ready");
        ready = everReady = true;
        if (!cancelled) setState({ kind: "ready" });
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 503) {
          setState({ kind: "degraded", reason: "API up, dependencies not ready" });
        } else if (!everReady && Date.now() - started < wakeMs) {
          // Most likely a sleeping free instance that is still starting up.
          setState({ kind: "waking" });
        } else {
          setState({ kind: "down", reason: "API unreachable" });
        }
      }
      if (!cancelled) timer = setTimeout(check, ready ? pollMs : 5000);
    }
    void check();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [pollMs, wakeMs]);

  const label = {
    loading: "Checking API…",
    waking: "Waking the API, about a minute",
    ready: "API ready",
    degraded: state.kind === "degraded" ? state.reason : "",
    down: state.kind === "down" ? state.reason : "",
  }[state.kind];

  return (
    <span className={`status status-${state.kind}`} role="status" aria-live="polite" title={label}>
      <span className="status-dot" aria-hidden="true" />
      <span className="status-text">{label}</span>
    </span>
  );
}
