"use client";

import { useEffect, useState } from "react";

import { apiFetch, type Health } from "@/lib/api";

let cached: Promise<boolean> | null = null;

function loadReadOnly(): Promise<boolean> {
  if (!cached) {
    const request = apiFetch<Health>("/health").then((health) => health.read_only === true);
    cached = request;
    // Retry on the next use if the API could not be reached.
    request.catch(() => {
      if (cached === request) cached = null;
    });
  }
  return cached;
}

/** For tests: forget the cached answer. */
export function resetReadOnlyCache(): void {
  cached = null;
}

/**
 * Whether the API refuses every mutation (REPOLENS_READ_ONLY). ``null`` until known, or if the
 * API could not be reached; callers treat that as "do not offer actions yet".
 */
export function useReadOnly(): boolean | null {
  const [readOnly, setReadOnly] = useState<boolean | null>(null);
  useEffect(() => {
    let cancelled = false;
    loadReadOnly().then(
      (value) => {
        if (!cancelled) setReadOnly(value);
      },
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, []);
  return readOnly;
}
