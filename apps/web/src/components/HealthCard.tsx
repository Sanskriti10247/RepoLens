"use client";

import Link from "next/link";

import { SEVERITIES, SEVERITY_TITLE, ScoreBar, ScoreDial } from "@/components/Insights";
import { Card, Loading } from "@/components/ui";
import type { Insights } from "@/lib/types";
import { useApi } from "@/lib/useApi";

/**
 * The health score in brief on the overview, opening the full report. Stays out of the way
 * on an API that does not produce reports (404) or while the report is not ready.
 */
export function HealthCard({ analysisId, repoId }: { analysisId: string; repoId: string }) {
  const { data, error } = useApi<Insights>(`/analyses/${analysisId}/insights`);
  const href = `/repositories/${repoId}/insights?analysis=${analysisId}`;
  if (error) return null;
  return (
    <Card id="health" title="Health" className="health-card">
      {!data ? (
        <Loading what="Scoring the analysis" rows={3} />
      ) : (
        <Link href={href} className="health-link" aria-label={
            data.score.grade === null
              ? "Health report: not enough data for a grade. Open the full report"
              : `Health report: grade ${data.score.grade}, ${data.score.overall} of 100. Open the full report`
          }>
          <ScoreDial score={data.score.overall} grade={data.score.grade} size="small" />
          <span className="health-body">
            <span className="health-verdict">
              {data.score.grade === null ? (
                "Not enough data to grade this analysis."
              ) : (
                <>
                  Grade {data.score.grade}
                  <span className="muted">, {data.score.overall} of 100 across {data.score.components.length} measures.</span>
                </>
              )}
            </span>
            <span className="health-components">
              {data.score.components.map((c) => (
                <span key={c.key} className="health-component">
                  <span className="health-component-name">{c.label}</span>
                  <ScoreBar score={c.score} label={c.label} />
                  <span className={c.score === null ? "muted" : "num"}>{c.score ?? "n/a"}</span>
                </span>
              ))}
            </span>
            <span className="health-foot">
              <span className="health-findings">
                {SEVERITIES.filter((s) => data.findings.some((f) => f.severity === s)).map((s) => (
                  <span key={s} className={`sev-${s}`}>
                    <span className="finding-mark" aria-hidden="true" />
                    {data.findings.filter((f) => f.severity === s).length} {SEVERITY_TITLE[s].toLowerCase()}
                  </span>
                ))}
                {data.findings.length === 0 && <span>No findings</span>}
              </span>
              <span className="health-open">
                Read the report <span className="arrow" aria-hidden="true">→</span>
              </span>
            </span>
          </span>
        </Link>
      )}
    </Card>
  );
}
