"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, apiFetch } from "@/lib/api";

export interface ApiState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
}

interface Options<T> {
  /** Poll while this returns true (e.g. while a job is still running). */
  pollWhile?: (data: T) => boolean;
  pollMs?: number;
}

interface Result<T> {
  key: string | null;
  data: T | null;
  error: ApiError | null;
}

/** Fetch ``path`` (relative to /api); ``null`` skips the request. Stale data stays visible. */
export function useApi<T>(path: string | null, options: Options<T> = {}): ApiState<T> {
  const [tick, setTick] = useState(0);
  const [result, setResult] = useState<Result<T>>({ key: null, data: null, error: null });
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
    async function load() {
      try {
        const data = await apiFetch<T>(path as string);
        if (cancelled) return;
        setResult({ key, data, error: null });
        const { pollWhile, pollMs = 1500 } = optionsRef.current;
        if (pollWhile?.(data)) timer = setTimeout(load, pollMs);
      } catch (caught) {
        if (cancelled) return;
        const error =
          caught instanceof ApiError
            ? caught
            : new ApiError(0, "network_error", "Could not reach the API", null);
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
    reload,
  };
}
