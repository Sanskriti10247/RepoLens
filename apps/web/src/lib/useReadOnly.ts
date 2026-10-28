"use client";

import { useEffect, useState } from "react";

import { apiFetch, type Health } from "@/lib/api";

let cached: Promise<Health> | null = null;

function loadHealth(): Promise<Health> {
  if (!cached) {
    const request = apiFetch<Health>("/health");
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

export interface Capabilities {
  /** The API refuses every mutation (REPOLENS_READ_ONLY). */
  readOnly: boolean | null;
  /**
   * Test runs and sandboxed collection are available. APIs that predate the field always had
   * a sandbox, so a missing value counts as available.
   */
  sandbox: boolean | null;
}

const UNKNOWN: Capabilities = { readOnly: null, sandbox: null };

/**
 * What this API instance allows. Each field is ``null`` until known, or if the API could not
 * be reached; callers treat that as "do not offer the action yet".
 */
export function useCapabilities(): Capabilities {
  const [capabilities, setCapabilities] = useState<Capabilities>(UNKNOWN);
  useEffect(() => {
    let cancelled = false;
    loadHealth().then(
      (health) => {
        if (cancelled) return;
        setCapabilities({ readOnly: health.read_only === true, sandbox: health.sandbox !== false });
      },
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, []);
  return capabilities;
}

/**
 * Whether the API refuses every mutation (REPOLENS_READ_ONLY). ``null`` until known, or if the
 * API could not be reached; callers treat that as "do not offer actions yet".
 */
export function useReadOnly(): boolean | null {
  return useCapabilities().readOnly;
}
