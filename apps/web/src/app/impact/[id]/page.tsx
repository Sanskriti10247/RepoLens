"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { Fragment, useState } from "react";

import { Badge, Breadcrumbs, Card, Empty, ErrorPanel, KeyValues, Loading, Stat } from "@/components/ui";
import { ApiError, postJson } from "@/lib/api";
import { TERMINAL_CHANGE, formatTime, label, shortSha } from "@/lib/format";
import type {
  ChangeSet,
  ChangedFile,
  ChangedSymbol,
  ImpactItem,
  ImpactReport,
  Page,
  TestPlan,
} from "@/lib/types";
import { useApi } from "@/lib/useApi";

export default function ImpactPage() {
  const { id } = useParams<{ id: string }>();
  const change = useApi<ChangeSet>(`/impact/${id}`, {
    pollWhile: (c) => !TERMINAL_CHANGE.has(c.status),
  });
  if (change.error) return <ErrorPanel error={change.error} />;
  if (!change.data) return <Loading what="Loading impact analysis" />;
  const c = change.data;
  const done = c.status === "SUCCEEDED";
  return (
    <>
      <Breadcrumbs
        items={[
          ["Repository", `/repositories/${c.repository_id}`],
          ["Impact", `/repositories/${c.repository_id}/impact`],
          [`${c.base_ref} → ${c.target_ref}`, null],
        ]}
      />
      <h1>
        Impact of <span className="mono">{c.base_ref}</span> → <span className="mono">{c.target_ref}</span>{" "}
        <Badge value={c.status} />
      </h1>
      <Card>
        <KeyValues
          items={[
            ["Base commit", <span key="b" className="mono">{c.base_sha ?? "resolving…"}</span>],
            ["Target commit", <span key="t" className="mono">{c.target_sha ?? "resolving…"}</span>],
            ["Diff", c.diff_algorithm ?? "—"],
            ["Created", formatTime(c.created_at)],
            ["Job", c.job ? `${label(c.job.status)} (attempt ${c.job.attempts}/${c.job.max_attempts})` : "—"],
          ]}
        />
        {!TERMINAL_CHANGE.has(c.status) && (
          <p className="muted">Analyzing both revisions and mapping the change… this page updates automatically.</p>
        )}
        {c.error && <div className="error-panel">{c.error}</div>}
      </Card>
      {done && <Report changeId={id} />}
      {done && <ChangedFiles changeId={id} />}
      {done && <ChangedSymbols changeId={id} />}
      {done && <ImpactItems changeId={id} />}
      {done && <Plans changeId={id} />}
    </>
  );
}

function Report({ changeId }: { changeId: string }) {
  const { data, error } = useApi<ImpactReport>(`/impact/${changeId}/report`);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading what="Loading report" />;
  const counts = data.counts as Record<string, number>;
  const hidden = data.uncertainty.possible_hidden_callers ?? [];
  return (
    <>
      <div className="stats">
        <Stat label="Changed symbols" value={counts.changed_symbols ?? 0} />
        <Stat label="Impacted symbols" value={counts.impacted_symbols ?? 0} hint="non-test" />
        <Stat label="Impacted tests" value={counts.impacted_tests ?? 0} />
        <Stat label="Risk" value={<Badge value={data.risk_level} />} hint={`${data.risk_factors.reduce((s, f) => s + f.points, 0)} points`} />
        <Stat label="Completeness" value={<Badge value={data.completeness} />} />
        <Stat label="Recommended" value={<Badge value={data.recommended_scope} text={label(data.recommended_scope)} />} />
      </div>
      <div className="grid-2">
        <Card title="Recommended verification">
          <ul className="plain">
            {data.scope_reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          {data.completeness_reasons.length > 0 && (
            <>
              <p className="small">Why the analysis is not complete:</p>
              <ul className="plain small mono">
                {data.completeness_reasons.map((r, i) => (
                  <li key={i}>
                    {String(r.reason)} {r.path ? `· ${String(r.path)}` : ""} {r.detail ? `· ${String(r.detail)}` : ""}
                  </li>
                ))}
              </ul>
            </>
          )}
        </Card>
        <Card title="Risk factors">
          {data.risk_factors.length === 0 ? (
            <p className="muted small">No risk factors apply.</p>
          ) : (
            <table className="table small">
              <tbody>
                {data.risk_factors.map((f) => (
                  <tr key={f.factor}>
                    <td>{label(f.factor)}</td>
                    <td>+{f.points}</td>
                    <td>
                      {f.detail}
                      <div className="muted mono">{JSON.stringify(f.value)}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="muted small">{data.disclaimer}</p>
        </Card>
      </div>
      <Card title="Uncertainty">
        <KeyValues
          items={[
            ["Unresolved references in impacted code", data.uncertainty.unresolved_references_in_impacted_code ?? 0],
            ["Dynamic references in impacted code", data.uncertainty.dynamic_references_in_impacted_code ?? 0],
            ["Traversal limits", `depth ${String(data.limits.max_depth)}, ${String(data.limits.max_nodes)} nodes${data.limits.target_truncated ? " — truncated" : ""}`],
          ]}
        />
        {hidden.length > 0 && (
          <>
            <p className="small">Possible hidden callers (unresolved call sites that name a changed method):</p>
            <ul className="plain small mono">
              {hidden.map((h, i) => (
                <li key={i}>
                  {h.source}: {h.raw_text}
                </li>
              ))}
            </ul>
          </>
        )}
      </Card>
    </>
  );
}

function ChangedFiles({ changeId }: { changeId: string }) {
  const { data, error } = useApi<Page<ChangedFile>>(`/impact/${changeId}/files?limit=100`);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return null;
  return (
    <Card title={`Changed files (${data.page.total})`}>
      {data.items.length === 0 ? (
        <Empty>The two revisions have identical trees.</Empty>
      ) : (
        <table className="table small">
          <thead>
            <tr>
              <th>File</th>
              <th>Change</th>
              <th>Lines</th>
              <th>Mapped to symbols</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((f) => (
              <tr key={f.id}>
                <td className="mono">
                  {f.old_path && f.new_path && f.old_path !== f.new_path ? `${f.old_path} → ${f.new_path}` : f.new_path ?? f.old_path}
                </td>
                <td>{label(f.change_type)}</td>
                <td className="mono">
                  <span className="add">+{f.added_lines}</span> <span className="del">−{f.deleted_lines}</span>
                </td>
                <td>
                  <Badge value={f.mapping_status === "MAPPED" ? "SUCCEEDED" : f.mapping_status === "NON_PYTHON" && f.role === "DOC" ? "NONE" : "PARTIAL"} text={label(f.mapping_status)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

function ChangedSymbols({ changeId }: { changeId: string }) {
  const { data } = useApi<Page<ChangedSymbol>>(`/impact/${changeId}/changed-symbols?limit=100`);
  if (!data || data.items.length === 0) return null;
  return (
    <Card title={`Changed symbols (${data.page.total})`}>
      <table className="table small">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Kind</th>
            <th>Change</th>
            <th>Details</th>
          </tr>
        </thead>
        <tbody>
          {data.items.map((s) => (
            <tr key={s.id}>
              <td className="mono">{s.qualified_name}</td>
              <td>{label(s.symbol_kind)}</td>
              <td>
                <Badge value={s.change_kind === "REMOVED" ? "FAILED" : s.change_kind === "ADDED" ? "SUCCEEDED" : "PARTIAL"} text={label(s.change_kind)} />
              </td>
              <td className="small">
                {[
                  s.signature_changed && "signature",
                  s.body_changed && "body",
                  s.decorators_changed && "decorators",
                  s.bases_changed && "base classes",
                ]
                  .filter(Boolean)
                  .join(", ") || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

const STRENGTHS = ["", "CHANGED", "DIRECT", "TRANSITIVE", "IMPORT_ONLY", "HEURISTIC"];

function ImpactItems({ changeId }: { changeId: string }) {
  const [strength, setStrength] = useState("");
  const [testsOnly, setTestsOnly] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const query = new URLSearchParams({ limit: "100" });
  if (strength) query.set("strength", strength);
  if (testsOnly) query.set("is_test", "true");
  const { data, error } = useApi<Page<ImpactItem>>(`/impact/${changeId}/items?${query}`);
  return (
    <Card
      title={`Changed and impacted symbols${data ? ` (${data.page.total})` : ""}`}
      actions={
        <>
          <select value={strength} onChange={(e) => setStrength(e.target.value)} aria-label="Strength">
            {STRENGTHS.map((s) => (
              <option key={s} value={s}>
                {s ? label(s) : "All strengths"}
              </option>
            ))}
          </select>
          <label className="checkbox">
            <input type="checkbox" checked={testsOnly} onChange={(e) => setTestsOnly(e.target.checked)} /> Tests only
          </label>
        </>
      }
    >
      {error && <ErrorPanel error={error} />}
      {data && data.items.length === 0 && <Empty>Nothing matches.</Empty>}
      {data && data.items.length > 0 && (
        <table className="table small">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Strength</th>
              <th>Distance</th>
              <th>Because of</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((item) => (
              <Fragment key={item.id}>
                <tr className="clickable" onClick={() => setOpen(open === item.id ? null : item.id)}>
                  <td className="mono">
                    {item.qualified_name}
                    {item.is_test && <span className="tag">test</span>}
                  </td>
                  <td>
                    <Badge value={item.strength === "CHANGED" ? "RUNNING" : item.strength === "HEURISTIC" ? "HEURISTIC" : item.strength === "IMPORT_ONLY" ? "STATIC" : "SUCCEEDED"} text={label(item.strength)} />
                  </td>
                  <td>{item.depth}</td>
                  <td className="small">
                    <span className="mono">{item.seed_qualified_name}</span> · {label(item.seed_reason)}
                    {item.graph === "BASE" && <span className="muted"> (base revision)</span>}
                  </td>
                </tr>
                {open === item.id && (
                  <tr className="explain-row">
                    <td colSpan={4}>
                      {item.explanation.length === 0 ? (
                        <span className="muted">Changed directly.</span>
                      ) : (
                        <ol className="path">
                          {item.explanation.map((step, i) => (
                            <li key={i}>
                              <span className="mono">{step.from}</span> {label(step.edge_type).toLowerCase()}{" "}
                              <span className="mono">{step.to}</span>
                              {step.confidence !== "EXACT" && <span className="tag warn">heuristic</span>}
                            </li>
                          ))}
                        </ol>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

function Plans({ changeId }: { changeId: string }) {
  const router = useRouter();
  const plans = useApi<TestPlan[]>(`/impact/${changeId}/test-plans`);
  const [policy, setPolicy] = useState("CONSERVATIVE");
  const [maxTests, setMaxTests] = useState("");
  const [maxSeconds, setMaxSeconds] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  async function create(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const plan = await postJson<TestPlan>(`/impact/${changeId}/test-plan`, {
        policy,
        max_tests: maxTests ? Number(maxTests) : null,
        max_seconds: maxSeconds ? Number(maxSeconds) : null,
      });
      router.push(`/test-plans/${plan.id}`);
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
    }
  }
  return (
    <Card title="Test selection">
      <form className="form-row" onSubmit={create}>
        <label className="narrow">
          Policy
          <select value={policy} onChange={(e) => setPolicy(e.target.value)} aria-label="Selection policy">
            <option value="CONSERVATIVE">Conservative — run every candidate</option>
            <option value="BUDGETED">Budgeted — cap tests or time</option>
          </select>
        </label>
        {policy === "BUDGETED" && (
          <>
            <label className="narrow">
              Max tests
              <input type="number" min={1} value={maxTests} onChange={(e) => setMaxTests(e.target.value)} />
            </label>
            <label className="narrow">
              Max seconds
              <input type="number" min={1} value={maxSeconds} onChange={(e) => setMaxSeconds(e.target.value)} />
            </label>
          </>
        )}
        <button type="submit">Select tests</button>
      </form>
      {error && <ErrorPanel error={error} title="Could not create a test plan" />}
      {plans.data && plans.data.length > 0 && (
        <table className="table small">
          <thead>
            <tr>
              <th>Plan</th>
              <th>Policy</th>
              <th>Scope</th>
              <th>Selected</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {plans.data.map((p) => (
              <tr key={p.id}>
                <td>
                  <Link href={`/test-plans/${p.id}`} className="mono">
                    {shortSha(p.id)}
                  </Link>
                </td>
                <td>{label(p.policy)}</td>
                <td>
                  <Badge value={p.scope} />
                </td>
                <td>
                  {p.selected_tests} / {p.total_tests}
                </td>
                <td>{formatTime(p.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}
