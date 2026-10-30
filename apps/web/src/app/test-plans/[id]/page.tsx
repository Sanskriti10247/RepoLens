"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { Fragment, useState } from "react";

import { Badge, Breadcrumbs, Card, Empty, ErrorPanel, KeyValues, Loading, ReadOnlyNotice, SandboxNotice } from "@/components/ui";
import { ApiError, postJson } from "@/lib/api";
import { formatDuration, label } from "@/lib/format";
import type { ChangeSet, Page, PlanEvidence, TestPlan, TestPlanItem, TestRun } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import { useCapabilities } from "@/lib/useReadOnly";

export default function TestPlanPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const plan = useApi<TestPlan>(`/test-plans/${id}`);
  const change = useApi<ChangeSet>(plan.data ? `/impact/${plan.data.change_set_id}` : null);
  const items = useApi<Page<TestPlanItem>>(`/test-plans/${id}/items?limit=100`);
  const [coverage, setCoverage] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const { readOnly, sandbox } = useCapabilities();
  if (plan.error) return <ErrorPanel error={plan.error} />;
  if (!plan.data) return <Loading what="Loading test plan" shape="block" />;
  const p = plan.data;
  const selected = items.data?.items.filter((i) => i.selected) ?? [];
  const excluded = items.data?.items.filter((i) => !i.selected) ?? [];
  const sources = p.evidence_sources as Record<string, unknown>;
  const notCollected = (sources.excluded_not_collected as string[] | undefined) ?? [];

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const testRun = await postJson<TestRun>("/test-runs", { plan_id: id, coverage });
      router.push(`/test-runs/${testRun.id}`);
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {change.data && (
        <Breadcrumbs
          items={[
            ["Repository", `/repositories/${change.data.repository_id}`],
            ["Impact", `/impact/${p.change_set_id}`],
            ["Test plan", null],
          ]}
        />
      )}
      <div className="page-head">
        <h1>
          Test plan <Badge value={p.scope} text={`${label(p.scope)} scope`} />
        </h1>
        <p className="lede">
          Tests ranked by how directly they depend on the change. Select a row to see the evidence behind it.
        </p>
      </div>
      {p.fallback_reason && (
        <div className="warn-panel">
          <strong>Full-suite fallback.</strong> The impact analysis could not vouch for a targeted run:{" "}
          {p.fallback_reason}
        </div>
      )}
      {p.budget_limited && (
        <div className="warn-panel">
          <strong>Budget applied.</strong> Some candidate tests were excluded to fit the budget; they are listed
          below with the reason <em>budget</em>.
        </div>
      )}
      <Card
        title="Run"
        actions={
          readOnly === true || sandbox === false ? undefined : (
          <>
            <label className="checkbox">
              <input type="checkbox" checked={coverage} onChange={(e) => setCoverage(e.target.checked)} />
              Collect coverage
            </label>
            <button
              onClick={run}
              disabled={busy || readOnly === null || sandbox === null || (p.selected_tests === 0 && p.scope !== "FULL_SUITE")}
            >
              {busy ? "Starting…" : p.scope === "FULL_SUITE" ? "Run full suite in sandbox" : `Run ${p.selected_tests} selected tests`}
            </button>
          </>
          )
        }
      >
        {readOnly === true && <ReadOnlyNotice what="running tests" />}
        {readOnly === false && sandbox === false && (
          <SandboxNotice>
            Running tests needs the Docker sandbox, which this instance does not have. The selection below is still
            yours to run locally: <code className="inline-code">pytest</code> with the listed node ids.
          </SandboxNotice>
        )}
        <KeyValues
          items={[
            ["Selected", `${p.selected_tests} of ${p.total_tests} tests`],
            ["Policy", label(p.policy)],
            ["Estimated runtime", p.estimated_seconds !== null ? formatDuration(p.estimated_seconds * 1000) : "unknown (no measured durations for every selected test)"],
            [
              "Evidence used",
              ["static_graph", "collection", "coverage", "history"]
                .map((k) => `${label(k)}: ${sources[k] ? "yes" : "no"}`)
                .join(" · "),
            ],
          ]}
        />
        {error && <ErrorPanel error={error} title="Could not start the run" />}
      </Card>
      <Card title={<>Selected tests <span className="count-chip num">{selected.length}</span></>}>
        {items.loading && !items.data && <Loading what="Loading the selection" shape="table" />}
        {items.data && selected.length === 0 && <Empty>No test depends on the changed code.</Empty>}
        {selected.length > 0 && <PlanTable items={selected} />}
      </Card>
      <Card title={<>Not selected <span className="count-chip num">{excluded.length}</span></>}>
        {excluded.length === 0 ? (
          <p className="muted">Every test was selected.</p>
        ) : (
          <table className="table small">
            <tbody>
              {excluded.map((i) => (
                <tr key={i.id}>
                  <td className="mono">{i.node_id}</td>
                  <td className="nowrap">
                    <Badge value={i.exclusion_reason === "BUDGET" ? "PARTIAL" : "NONE"} text={label(i.exclusion_reason)} />
                  </td>
                  <td className="muted hide-sm">{i.evidence.length ? `strongest evidence: ${label(i.evidence[0]?.kind)}` : "no evidence linking it to the change"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {notCollected.length > 0 && (
          <p className="hint">Not collectable by pytest (excluded): {notCollected.join(", ")}</p>
        )}
      </Card>
      {change.data && (
        <p className="small back-link">
          <Link href={`/impact/${p.change_set_id}`}>← Back to the impact report</Link>
        </p>
      )}
    </>
  );
}

function PlanTable({ items }: { items: TestPlanItem[] }) {
  const [open, setOpen] = useState<number | null>(null);
  return (
    <table className="table small">
      <thead>
        <tr>
          <th className="right">#</th>
          <th>Test</th>
          <th className="right">Score</th>
          <th className="hide-sm">Strongest evidence</th>
          <th className="right hide-sm">Measured</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <Fragment key={item.id}>
            <tr
              className={open === item.id ? "clickable open" : "clickable"}
              onClick={() => setOpen(open === item.id ? null : item.id)}
              title="Show the evidence for this test"
            >
              <td className="mono right muted nowrap">{item.rank}</td>
              <td className="mono">
                <span className="chev" aria-hidden="true" />
                {item.node_id}
              </td>
              <td className="mono right">{item.score}</td>
              <td className="hide-sm">{item.evidence[0] && <EvidenceBadge evidence={item.evidence[0]} />}</td>
              <td className="mono right hide-sm">{item.estimated_seconds !== null ? formatDuration(item.estimated_seconds * 1000) : "—"}</td>
            </tr>
            {open === item.id && (
              <tr className="explain-row">
                <td colSpan={5}>
                  <ul className="plain">
                    {item.evidence.map((e, i) => (
                      <li key={i}>
                        <EvidenceBadge evidence={e} /> <EvidenceDetail evidence={e} />
                      </li>
                    ))}
                  </ul>
                </td>
              </tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </table>
  );
}

function EvidenceBadge({ evidence }: { evidence: PlanEvidence }) {
  const tone =
    evidence.kind === "STATIC_HEURISTIC" || evidence.kind === "MODULE_DEPENDENCY"
      ? "HEURISTIC"
      : evidence.kind === "FULL_SUITE_FALLBACK"
        ? "NONE"
        : "SUCCEEDED";
  return <Badge value={tone} text={`${label(evidence.kind)} · ${evidence.weight}`} />;
}

function EvidenceDetail({ evidence }: { evidence: PlanEvidence }) {
  if (evidence.path && evidence.path.length) {
    return (
      <span className="small mono">
        {[evidence.path[0]?.to, ...evidence.path.map((s) => s.from)].join(" ← ")}
      </span>
    );
  }
  if (evidence.lines) {
    return (
      <span className="small">
        executed changed lines {Object.entries(evidence.lines).map(([path, lines]) => `${path}:${lines.join(",")}`).join("; ")}
      </span>
    );
  }
  if (evidence.imports_modules) return <span className="small">imports {evidence.imports_modules.join(", ")}</span>;
  if (evidence.reason) return <span className="small">{evidence.reason}</span>;
  if (evidence.seed) return <span className="small">changed: {evidence.seed}</span>;
  return null;
}
