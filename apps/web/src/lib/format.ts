export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 10) : "—";
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

export type Tone = "ok" | "warn" | "bad" | "active" | "neutral";

const TONES: Record<string, Tone> = {
  SUCCEEDED: "ok",
  PASSED: "ok",
  COMPLETE: "ok",
  LOW: "ok",
  TARGETED: "ok",
  NONE: "neutral",
  REUSED: "ok",
  COLLECTED: "ok",
  PARSED: "ok",
  RESOLVED: "ok",
  EXACT: "ok",
  PARTIAL: "warn",
  MEDIUM: "warn",
  BROAD: "warn",
  HEURISTIC: "warn",
  AMBIGUOUS: "warn",
  PARSED_WITH_ERRORS: "warn",
  SKIPPED: "neutral",
  EXTERNAL: "neutral",
  STATIC: "neutral",
  QUEUED: "active",
  CLAIMED: "active",
  RUNNING: "active",
  FAILED: "bad",
  HIGH: "bad",
  FULL_SUITE: "bad",
  UNKNOWN: "bad",
  TIMED_OUT: "bad",
  CANCELLED: "neutral",
  BLOCKED: "bad",
  INFRASTRUCTURE_ERROR: "bad",
  COLLECTION_ERROR: "bad",
  SECURITY_POLICY_BLOCKED: "bad",
  RESOURCE_LIMIT_EXCEEDED: "bad",
  UNRESOLVED: "bad",
  DYNAMIC: "warn",
  ERROR: "bad",
  NOT_COLLECTED: "warn",
};

export function toneFor(value: string | null | undefined): Tone {
  return (value && TONES[value]) || "neutral";
}

export function label(value: string | null | undefined): string {
  if (!value) return "—";
  return value.replaceAll("_", " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
}

export const TERMINAL_ANALYSIS = new Set(["SUCCEEDED", "PARTIAL", "REUSED", "FAILED", "CANCELLED"]);
export const TERMINAL_CHANGE = new Set(["SUCCEEDED", "FAILED", "CANCELLED"]);
