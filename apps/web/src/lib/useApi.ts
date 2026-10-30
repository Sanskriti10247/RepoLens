"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, apiFetch } from "@/lib/api";

export interface ApiState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  /** Error code of a temporary failure being retried quietly (e.g. "source_fetching"). */
  retrying: string | null;
  reload: () => void;
}

interface Options<T> {
  /** Poll while this returns true (e.g. while a job is still running). */
  pollWhile?: (data: T) => boolean;
  pollMs?: number;
}

// A free-tier API sleeps when idle and takes about a minute to wake; the proxy answers 502/504
// meanwhile. Reads retry quietly for a while instead of showing an error straight away.
const WAKE_STATUSES = new Set([0, 502, 503, 504]);
const WAKE_RETRY_MS = 5000;
const WAKE_RETRIES = 24;

interface Result<T> {
  key: string | null;
  data: T | null;
  error: ApiError | null;
}

/** Fetch ``path`` (relative to /api); ``null`` skips the request. Stale data stays visible. */
export function useApi<T>(path: string | null, options: Options<T> = {}): ApiState<T> {
  const [tick, setTick] = useState(0);
  const [result, setResult] = useState<Result<T>>({ key: null, data: null, error: null });
  const [retrying, setRetrying] = useState<{ key: string; code: string } | null>(null);
  const optionsRef = useRef(options);
  useEffect(() => {
    optionsRef.current = options;
  });
  const key = path === null ? null : `${path}#${tick}`;
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (key === null || path === null) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let retries = 0;
    async function load() {
      try {
        const data = await apiFetch<T>(path as string);
        if (cancelled) return;
        retries = 0;
        setRetrying(null);
        setResult({ key, data, error: null });
        const { pollWhile, pollMs = 1500 } = optionsRef.current;
        if (pollWhile?.(data)) timer = setTimeout(load, pollMs);
      } catch (caught) {
        if (cancelled) return;
        const error =
          caught instanceof ApiError
            ? caught
            : new ApiError(0, "network_error", "Could not reach the API", null);
        if (WAKE_STATUSES.has(error.status) && retries < WAKE_RETRIES) {
          retries += 1;
          setRetrying({ key: key as string, code: error.code });
          timer = setTimeout(load, WAKE_RETRY_MS);
          return;
        }
        setResult((previous) => ({ key, data: previous.data, error }));
      }
    }
    void load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [key, path]);

  // Keep showing the previous result while the *same* resource reloads (polling, reload),
  // but never show another resource's data under a new path.
  const resultPath = result.key === null ? null : result.key.slice(0, result.key.lastIndexOf("#"));
  const samePath = resultPath === path;
  return {
    data: samePath ? result.data : null,
    error: samePath ? result.error : null,
    loading: key !== null && result.key !== key,
    retrying: retrying !== null && retrying.key === key ? retrying.code : null,
    reload,
  };
}
