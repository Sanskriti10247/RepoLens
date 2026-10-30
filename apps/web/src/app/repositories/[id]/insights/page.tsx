"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { Suspense, useEffect, type CSSProperties } from "react";

import { AnalysisPipeline } from "@/components/AnalysisPipeline";
import {
  CouplingScatter,
  FindingItem,
  Histogram,
  Meter,
  RankList,
  SEVERITIES,
  SEVERITY_TITLE,
  ScoreBar,
  ScoreDial,
  StackedBar,
  evidenceHref,
} from "@/components/Insights";
import { WithRail } from "@/components/SectionRail";
import { Card, Empty, ErrorPanel, Loading, Stat, Stats, Ticker, Tip } from "@/components/ui";
import { TERMINAL_ANALYSIS, formatBytes, label, shortSha } from "@/lib/format";
import type { Analysis, AnalysisSummary, Insights, Page } from "@/lib/types";
import { useApi } from "@/lib/useApi";

const RAIL: [string, string][] = [
  ["score", "Score"],
  ["findings", "Findings"],
  ["composition", "Composition"],
  ["complexity", "Complexity"],
  ["size", "Size"],
  ["docs-tests", "Docs and tests"],
  ["coupling", "Coupling"],
  ["dead-code", "Dead code"],
  ["references", "References"],
];

const FINISHED = new Set(["SUCCEEDED", "PARTIAL", "REUSED"]);

const METHOD_URL = "https://github.com/Sanskriti10247/RepoLens/blob/main/docs/insights.md";

const VERDICT: Record<string, string> = {
  A: "In very good shape.",
  B: "In good shape.",
  C: "Fair, with clear places to improve.",
  D: "Needs attention.",
  F: "In poor shape.",
};

export default function InsightsPage() {
  return (
    <Suspense fallback={<ReportSkeleton />}>
      <Report />
    </Suspense>
  );
}

function Report() {
  const { id } = useParams<{ id: string }>();
  const params = useSearchParams();
  const requested = params.get("analysis");
  const analyses = useApi<Page<AnalysisSummary>>(requested ? null : `/repositories/${id}/analyses?limit=20`, {
    pollWhile: (page) => page.items.length > 0 && !TERMINAL_ANALYSIS.has(page.items[0]!.status),
  });
  const items = analyses.data?.items ?? [];
  const latest = items[0] ?? null;
  // While a newer analysis runs, keep showing the last finished report rather than a wait.
  const running = latest && !TERMINAL_ANALYSIS.has(latest.status) ? latest : null;
  const finished = running ? items.find((a) => FINISHED.has(a.status)) ?? null : null;
  const analysisId = requested ?? finished?.id ?? latest?.id ?? null;
  if (analyses.error) return <ErrorPanel error={analyses.error} />;
  if (!requested && analyses.data && items.length === 0) {
    return <Empty>No analysis has been recorded for this repository yet, so there is nothing to report on.</Empty>;
  }
  if (!analysisId) return <ReportSkeleton />;
  return (
    <>
      {running && finished && (
        <p className="notice report-newer">
          A newer analysis of <span className="mono">{running.requested_ref}</span> is running. This report is for the
          previous one and will be replaced when it finishes.{" "}
          <Link href={`/repositories/${id}/insights?analysis=${running.id}`}>Watch it run</Link>
        </p>
      )}
      <ReportFor analysisId={analysisId} repoId={id} />
    </>
  );
}

function ReportFor({ analysisId, repoId }: { analysisId: string; repoId: string }) {
  const insights = useApi<Insights>(`/analyses/${analysisId}/insights`);
  const status = insights.error?.status;
  if (status === 409) return <Waiting analysisId={analysisId} onReady={insights.reload} />;
  if (status === 404) {
    return (
      <p className="notice">
        This API does not produce health reports yet. The rest of the analysis is available on the{" "}
        <Link href={`/repositories/${repoId}`}>overview</Link>.
      </p>
    );
  }
  if (insights.error) return <ErrorPanel error={insights.error} title="Could not load the health report" />;
  if (!insights.data) return <ReportSkeleton />;
  return <ReportView data={insights.data} repoId={repoId} />;
}

/** The analysis is still running: show it, and fetch the report the moment it finishes. */
function Waiting({ analysisId, onReady }: { analysisId: string; onReady: () => void }) {
  const analysis = useApi<Analysis>(`/analyses/${analysisId}`, {
    pollWhile: (a) => !TERMINAL_ANALYSIS.has(a.status),
    pollMs: 1000,
  });
  const status = analysis.data?.status;
  const finished = !!status && TERMINAL_ANALYSIS.has(status);
  const failed = status === "FAILED" || status === "CANCELLED";
  useEffect(() => {
    if (finished && !failed) onReady();
  }, [finished, failed, onReady]);
  return (
    <Card title="Health report">
      {failed ? (
        <Empty>The analysis did not finish, so there is no report for it. Analyze the revision again from the overview.</Empty>
      ) : (
        <p className="lede waiting-lede">
          The report is written from the finished analysis. This one is still running; the report appears here as soon
          as it is done.
        </p>
      )}
      {analysis.data && <AnalysisPipeline analysis={analysis.data} />}
      {!analysis.data && !analysis.error && <Loading what="Loading the analysis" />}
      {analysis.error && <ErrorPanel error={analysis.error} />}
    </Card>
  );
}

/** The modules behind the scatter, with their numbers in plain sight. */
function CouplingTable({
  modules,
  cycleMembers,
  repoId,
}: {
  modules: Insights["coupling"]["modules"];
  cycleMembers: Set<string>;
  repoId: string;
}) {
  return (
    <table className="table small coupling-table">
      <thead>
        <tr>
          <th>Module</th>
          <th className="right" title="Modules that depend on it">
            In
          </th>
          <th className="right" title="Modules it depends on">
            Out
          </th>
          <th className="instability-head">Instability</th>
        </tr>
      </thead>
      <tbody>
        {modules.map((m) => (
          <tr key={m.module} className={cycleMembers.has(m.module) ? "in-cycle" : undefined}>
            <td>
              {m.symbol_id ? (
                <Link href={`/repositories/${repoId}/explore?symbol=${m.symbol_id}`} className="mono">
                  {m.module}
                </Link>
              ) : (
                <span className="mono">{m.module}</span>
              )}
              {cycleMembers.has(m.module) && <span className="tag cycle-tag">cycle</span>}
            </td>
            <td className="right mono">{m.fan_in}</td>
            <td className="right mono">{m.fan_out}</td>
            <td>
              {m.instability === null ? (
                <span className="muted">n/a</span>
              ) : (
                <span className="instability">
                  <span className="instability-track" aria-hidden="true">
                    <span style={{ left: `${m.instability * 100}%` }} />
                  </span>
                  <span className="mono">{m.instability.toFixed(2)}</span>
                </span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Shaped like the report: the rail, the dial beside its component rows, then findings. */
function ReportSkeleton() {
  return (
    <div className="with-rail report-skeleton">
      <div className="page-rail" aria-hidden="true">
        <span className="sk sk-line" style={{ width: "70%" }} />
        <span className="sk sk-line" style={{ width: "55%" }} />
        <span className="sk sk-line" style={{ width: "62%" }} />
      </div>
      <div className="rail-body">
        <div className="report-hero">
          <span className="sk sk-dial" aria-hidden="true" />
          <div className="report-summary">
            <span className="sk sk-title" aria-hidden="true" />
            <Loading what="Writing the health report" rows={5} />
          </div>
        </div>
        <Loading what="Loading findings" shape="table" rows={4} />
      </div>
    </div>
  );
}

function ReportView({ data, repoId }: { data: Insights; repoId: string }) {
  const { score, overview, complexity, size, documentation, testing, coupling, dead_code: dead, findings } = data;
  const bySeverity = SEVERITIES.map((s) => [s, findings.filter((f) => f.severity === s)] as const).filter(
    ([, list]) => list.length > 0,
  );
  const serious = findings.filter((f) => f.severity === "high" || f.severity === "medium").length;
  const symbol = (sid: number, name: string) => (
    <Link href={`/repositories/${repoId}/explore?symbol=${sid}`} className="mono" title={name}>
      {name}
    </Link>
  );
  const where = (path: string, line?: number | null) => (
    <span className="mono">
      {path}
      {line ? `:${line}` : ""}
    </span>
  );
  const cycleMembers = new Set(coupling.cycles.flat());
  const references = Object.entries(data.references).filter(
    ([key, value]) => key !== "resolution_rate" && typeof value === "number",
  ) as [string, number][];
  const count = (key: string) => references.find(([k]) => k === key)?.[1] ?? 0;
  const resolution = typeof data.references.resolution_rate === "number" ? data.references.resolution_rate : null;
  const totalReferences = references.reduce((sum, [, n]) => sum + n, 0);

  return (
    <WithRail items={RAIL}>
      <section className="report-hero section" id="score" aria-labelledby="score-title">
        <ScoreDial score={score.overall} grade={score.grade} />
        <div className="report-summary">
          <p className="eyebrow">
            Health report <span className="sha">at {shortSha(data.commit_sha).slice(0, 7)}</span>
          </p>
          <h2 id="score-title" className="report-verdict">
            {score.grade === null ? (
              <>Not enough data to grade.</>
            ) : (
              <>
                Grade {score.grade}. {VERDICT[score.grade] ?? ""}
              </>
            )}{" "}
            <span className="muted">
              {serious === 0
                ? "No serious findings."
                : `${serious} ${serious === 1 ? "finding is" : "findings are"} worth a look.`}
            </span>
          </h2>
          <ol className="components">
            {score.components.map((c, i) => (
              <li key={c.key} style={{ "--order": i } as CSSProperties}>
                <span className="component-name">{c.label}</span>
                <span className="component-weight num" title="Share of the overall score given to this component">
                  {c.score === null ? "—" : `${Math.round(c.weight * 100)}%`}
                </span>
                <ScoreBar score={c.score} label={c.label} />
                <span className={c.score === null ? "component-score muted" : "component-score num"}>
                  {c.score === null ? "n/a" : c.score}
                </span>
                <span className="component-detail">{c.detail}</span>
              </li>
            ))}
          </ol>
          <p className="hint">
            {score.method.replace(/;?\s*see docs\/insights\.md\.?$/, ".")}{" "}
            {!complexity.available && "Complexity was not measured for this analysis, so the other weights are scaled up. "}
            <a href={METHOD_URL} target="_blank" rel="noreferrer">
              How the score is worked out<span className="ext" aria-hidden="true">↗</span>
            </a>
          </p>
        </div>
      </section>

      <Card id="findings" title={<>Findings <span className="count-chip num">{findings.length}</span></>}>
        {findings.length === 0 ? (
          <Empty>No rule fired for this analysis. That is a good sign, not a guarantee.</Empty>
        ) : (
          <>
            <ul className="severity-strip" aria-label="Findings by severity">
              {SEVERITIES.map((s) => {
                const n = findings.filter((f) => f.severity === s).length;
                return (
                  <li key={s} className={`sev-${s}${n ? "" : " is-zero"}`}>
                    <span className="finding-mark" aria-hidden="true" />
                    <span className="num">{n}</span> {SEVERITY_TITLE[s].toLowerCase()}
                  </li>
                );
              })}
            </ul>
            {bySeverity.map(([severity, list]) => (
              <div key={severity} className="finding-group">
                <p className="finding-group-head label">
                  {SEVERITY_TITLE[severity]} <span className="num">· {list.length}</span>
                </p>
                <ol className="findings">
                  {list.map((f, i) => (
                    <FindingItem key={f.id} finding={f} repoId={repoId} order={i} />
                  ))}
                </ol>
              </div>
            ))}
          </>
        )}
      </Card>

      <Card id="composition" title="Composition">
        <Stats>
          <Stat label="Files" value={overview.files} hint={formatBytes(overview.bytes)} />
          <Stat label="Modules" value={overview.modules} />
          <Stat label="Functions" value={overview.functions} />
          <Stat label="Classes" value={overview.classes} />
          <Stat label="Methods" value={overview.methods} />
        </Stats>
        <div className="columns">
          <div>
            <h3>Languages, by files</h3>
            <StackedBar
              label="Files by language"
              unit="files"
              parts={[...overview.languages]
                .sort((a, b) => b.files - a.files)
                .map((l) => ({ key: l.language, label: l.language, value: l.files }))}
            />
          </div>
          <div>
            <h3>Files by role</h3>
            <StackedBar
              label="Files by role"
              unit="files"
              parts={Object.entries(overview.roles)
                .sort((a, b) => b[1] - a[1])
                .map(([role, n]) => ({ key: role, label: label(role), value: n }))}
            />
          </div>
        </div>
      </Card>

      <Card id="complexity" title="Complexity">
        {!complexity.available ? (
          <Empty>
            This analysis was parsed before RepoLens measured complexity. Analyze the revision again to see it.
          </Empty>
        ) : (
          <div className="split">
            <div>
              <Histogram buckets={complexity.distribution} unit="functions" />
              <dl className="figures">
                <div>
                  <dt>Mean</dt>
                  <dd className="num">{complexity.mean === null ? "—" : complexity.mean.toFixed(1)}</dd>
                </div>
                <div>
                  <dt>90th percentile</dt>
                  <dd className="num">{complexity.p90 ?? "—"}</dd>
                </div>
              </dl>
              <p className="hint">
                One plus the number of decision points (branches, loops, boolean operators) in a function. Above ten,
                a function is hard to test exhaustively.
              </p>
            </div>
            <div>
              <h3>Most complex</h3>
              <RankList
                rows={complexity.top}
                value={(r) => r.complexity}
                unit=""
                name={(r) => symbol(r.symbol_id, r.qualified_name)}
                sub={(r) => (
                  <>
                    {where(r.path, r.line)} · {r.lines} lines
                  </>
                )}
                empty="No functions measured."
              />
            </div>
          </div>
        )}
      </Card>

      <Card id="size" title="Size">
        <div className="columns">
          <div>
            <h3>Longest functions</h3>
            <RankList
              rows={size.longest_functions}
              value={(r) => r.lines}
              unit="lines"
              name={(r) => symbol(r.symbol_id, r.qualified_name)}
              sub={(r) => where(r.path, r.line)}
              empty="No functions recorded."
            />
          </div>
          <div>
            <h3>Largest files</h3>
            <RankList
              rows={size.largest_files}
              value={(r) => r.lines ?? r.bytes}
              unit={size.largest_files.some((r) => r.lines === undefined) ? "bytes" : "lines"}
              name={(r) => (
                <Link href={`/repositories/${repoId}/explore?file=${r.file_id}`} className="mono">
                  {r.path}
                </Link>
              )}
              sub={(r) =>
                `${formatBytes(r.bytes)} · ${r.symbols.toLocaleString("en-US")} ${r.symbols === 1 ? "symbol" : "symbols"}`
              }
              empty="No files recorded."
            />
          </div>
        </div>
      </Card>

      <Card id="docs-tests" title="Documentation and tests">
        <div className="columns">
          <div>
            <h3>Docstrings on public symbols</h3>
            <Meter
              label="Documentation coverage"
              value={documentation.coverage}
              caption={
                <>
                  <span className="num">{documentation.documented.toLocaleString("en-US")}</span> of{" "}
                  <span className="num">{documentation.public_symbols.toLocaleString("en-US")}</span> public symbols
                  have a docstring.
                </>
              }
            />
            <h3>Undocumented, most used first</h3>
            <RankList
              rows={documentation.undocumented}
              value={(r) => r.callers}
              unit={["caller", "callers"]}
              name={(r) => symbol(r.symbol_id, r.qualified_name)}
              sub={(r) => where(r.path, r.line)}
              empty="Every public symbol is documented."
            />
          </div>
          <div>
            <h3>Modules referenced by tests</h3>
            <Meter
              label="Source modules referenced by tests"
              value={testing.coverage}
              figure={
                <>
                  <Ticker value={testing.modules_with_tests} />
                  <span className="meter-of"> of {testing.modules_with_tests + testing.modules_without_tests}</span>
                </>
              }
              caption={
                <>
                  source modules are imported or referenced by test code ·{" "}
                  {testing.test_functions.toLocaleString("en-US")} test functions in {testing.test_files} files.
                </>
              }
            />
            <h3>Untested, most depended on first</h3>
            <RankList
              rows={testing.untested_modules}
              value={(r) => r.fan_in}
              unit={["dependent", "dependents"]}
              name={(r) => (
                <Link
                  href={evidenceHref(repoId, { path: r.path, symbol_id: r.symbol_id ?? null, file_id: r.file_id ?? null }) ?? "#"}
                  className="mono"
                >
                  {r.module}
                </Link>
              )}
              sub={(r) => `${r.path} · ${r.symbols} ${r.symbols === 1 ? "symbol" : "symbols"}`}
              empty="Every source module is reached by a test."
            />
            <p className="hint">Read from imports and references in test files. This is not line coverage.</p>
          </div>
        </div>
      </Card>

      <Card id="coupling" title="Coupling">
        {coupling.modules.length === 0 ? (
          <Empty>No dependencies between modules were recorded.</Empty>
        ) : (
          <div className="split split-wide">
            <div>
              <CouplingScatter
                links={coupling.cycles.flatMap((cycle) =>
                  cycle.map((m, i) => [m, cycle[(i + 1) % cycle.length]!] as [string, string]).slice(0, cycle.length === 2 ? 1 : undefined),
                )}
                points={coupling.modules.map((m) => ({
                  key: m.module,
                  label: m.module,
                  x: m.fan_in,
                  y: m.fan_out,
                  flagged: cycleMembers.has(m.module),
                  note:
                    (m.instability === null ? "instability n/a" : `instability ${m.instability.toFixed(2)}`) +
                    (cycleMembers.has(m.module) ? ", in an import cycle" : ""),
                }))}
              />
              <p className="scatter-key small muted">
                <span className="scatter-dot" aria-hidden="true" /> module
                <span className="scatter-dot is-flagged" aria-hidden="true" /> in an import cycle
                <span>
                  ·{" "}
                  <Tip tip="Fan-out ÷ (fan-in + fan-out). Near 0: much depends on it and it depends on little, so it is costly to change. Near 1: it depends on others and little depends on it.">
                    instability
                  </Tip>{" "}
                  = out / (in + out)
                </span>
              </p>
              <CouplingTable modules={coupling.modules} cycleMembers={cycleMembers} repoId={repoId} />
            </div>
            <div>
              <h3>Import cycles</h3>
              {coupling.cycles.length === 0 ? (
                <p className="small">No import cycles between modules.</p>
              ) : (
                <ul className="cycles">
                  {coupling.cycles.map((cycle, i) => (
                    <li key={i}>
                      {cycle.map((m, j) => (
                        <span key={m}>
                          {j > 0 && <span className="cycle-arrow" aria-hidden="true"> ⇄ </span>}
                          <span className="mono">{m}</span>
                        </span>
                      ))}
                    </li>
                  ))}
                </ul>
              )}
              <h3>Most called symbols</h3>
              <RankList
                rows={coupling.hubs}
                value={(r) => r.callers}
                unit={["caller", "callers"]}
                name={(r) => symbol(r.symbol_id, r.qualified_name)}
                sub={(r) => where(r.path, r.line)}
                empty="No resolved calls between symbols."
              />
            </div>
          </div>
        )}
      </Card>

      <Card id="dead-code" title={<>Dead code candidates <span className="count-chip num">{dead.count}</span></>}>
        <p className="caveat">{dead.note}</p>
        {dead.candidates.length === 0 ? (
          <Empty>Every function and class is referenced from somewhere in the graph.</Empty>
        ) : (
          <table className="table small">
            <thead>
              <tr>
                <th>Symbol</th>
                <th className="hide-sm">Kind</th>
                <th className="hide-sm">Where</th>
                <th className="right">Lines</th>
              </tr>
            </thead>
            <tbody>
              {dead.candidates.map((c) => (
                <tr key={c.symbol_id}>
                  <td>{symbol(c.symbol_id, c.qualified_name)}</td>
                  <td className="hide-sm">{label(c.kind)}</td>
                  <td className="hide-sm">{where(c.path, c.line)}</td>
                  <td className="right mono">{c.lines}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {dead.count > dead.candidates.length && (
          <p className="hint">
            Showing {dead.candidates.length} of {dead.count}.
          </p>
        )}
      </Card>

      <Card id="references" title="Reference resolution">
        <div className="columns">
          <Meter
            label="References resolved"
            value={resolution}
            caption={
              <>
                <span className="num">{count("resolved").toLocaleString("en-US")}</span> of{" "}
                <span className="num">{(totalReferences - count("external")).toLocaleString("en-US")}</span>{" "}
                references inside the repository resolve to a single definition.{" "}
                <Tip tip="Resolved ÷ (resolved + ambiguous + unresolved + dynamic). References to the standard library and third-party packages are external and left out of the rate.">
                  How this is counted
                </Tip>
              </>
            }
          />
          <div>
            <h3>By status</h3>
            <StackedBar
              label="References by status"
              unit="references"
              parts={references
                .sort((a, b) => b[1] - a[1])
                .map(([key, n]) => ({ key, label: label(key), value: n }))}
            />
            <p className="hint">Unresolved and dynamic references are reported, never guessed.</p>
          </div>
        </div>
      </Card>
    </WithRail>
  );
}
