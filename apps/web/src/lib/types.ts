/** Response shapes of the RepoLens API (mirrors src/repolens/api/schemas.py). */

export interface PageMeta {
  total: number;
  limit: number;
  offset: number;
}

export interface Page<T> {
  items: T[];
  page: PageMeta;
}

export type AnalysisStatus =
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "PARTIAL"
  | "REUSED"
  | "FAILED"
  | "CANCELLED";

export interface Job {
  id: string;
  status: string;
  attempts: number;
  max_attempts: number;
  error_category: string | null;
  error_detail: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  cancel_requested_at: string | null;
}

export interface AnalysisSummary {
  id: string;
  status: AnalysisStatus;
  requested_ref: string;
  commit_sha: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface Repository {
  id: string;
  provider: string;
  host: string;
  owner: string;
  name: string;
  canonical_url: string;
  default_branch: string | null;
  created_at: string;
  latest_analysis: AnalysisSummary | null;
}

export interface Snapshot {
  id: string;
  commit_sha: string;
  tree_sha: string | null;
  committed_at: string | null;
  file_count: number | null;
  total_bytes: number | null;
  language_inventory: Record<string, number> | null;
  inventory_sha256: string | null;
  issue_counts: Record<string, number>;
  issues_truncated: boolean;
}

/** What the worker reported last for an analysis (services/progress.py). */
export interface AnalysisProgress {
  /** One of ``stages``, or ``"done"`` once the results are committed. */
  stage: string;
  stage_index: number;
  stage_count: number;
  stages: string[];
  files_done: number | null;
  files_total: number | null;
  current: string | null;
  detail: string | null;
  stage_started_at: string | null;
  durations_ms: Record<string, number>;
  updated_at: string;
}

export interface Analysis {
  id: string;
  repository_id: string;
  status: AnalysisStatus;
  requested_ref: string;
  resolved_ref: string | null;
  resolved_ref_kind: string | null;
  reused_run_id: string | null;
  results_run_id: string;
  parser_version: string;
  engine_version: string;
  config_fingerprint: string;
  counts: Record<string, unknown> | null;
  timings_ms: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  snapshot: Snapshot | null;
  job: Job | null;
  /** Absent on APIs that predate live progress. */
  progress?: AnalysisProgress | null;
}

export interface SymbolSummary {
  id: number;
  qualified_name: string;
  name: string;
  kind: string;
  visibility: string;
  path: string;
  file_id: number;
  start_line: number;
  start_col: number;
  end_line: number;
  end_col: number;
  signature: string | null;
  doc_summary: string | null;
  is_test: boolean;
  has_parse_errors: boolean;
}

export interface SymbolPage extends Page<SymbolSummary> {
  analysis_id: string;
  results_run_id: string;
  commit_sha: string;
}

export interface Neighbour {
  edge_type: string;
  confidence: string;
  provenance: string;
  reference_count: number;
  rules: string[];
  symbol: SymbolSummary;
}

export interface Reference {
  id: number;
  ref_kind: string;
  raw_text: string;
  start_line: number;
  end_line: number;
  resolution_status: string;
  resolver_rule: string | null;
  external_target: string | null;
  candidates: string[] | null;
  target_symbol_id: number | null;
  target_qualified_name: string | null;
}

export interface SymbolDetail {
  analysis_id: string;
  commit_sha: string;
  symbol: SymbolSummary;
  body_fingerprint: string | null;
  decorators: string[];
  parent: SymbolSummary | null;
  children: SymbolSummary[];
  callers: Neighbour[];
  callers_total: number;
  callees: Neighbour[];
  callees_total: number;
  dependents: Neighbour[];
  dependencies: Neighbour[];
  references: Reference[];
  reference_counts: Record<string, number>;
}

export interface SourceFile {
  file_id: number;
  path: string;
  language: string | null;
  commit_sha: string;
  blob_sha: string;
  content: string;
}

export interface FileEntry {
  id: number;
  path: string;
  language: string | null;
  role: string;
  size_bytes: number;
  status: string | null;
  module_name: string | null;
  symbol_count: number | null;
  error_count: number | null;
}

export interface FilePage extends Page<FileEntry> {
  status_counts: Record<string, number>;
}

export interface Warning {
  id: number;
  code: string;
  severity: string;
  message: string;
  path: string | null;
  start_line: number | null;
  end_line: number | null;
}

export interface Stats {
  analysis_id: string;
  symbols_by_kind: Record<string, number>;
  edges_by_type: Record<string, number>;
  references_by_status: Record<string, number>;
  top_fan_in: { symbol: SymbolSummary; count: number }[];
  top_fan_out: { symbol: SymbolSummary; count: number }[];
}

export interface ModuleNode {
  /** The id of the module's MODULE symbol (usable in the code explorer). */
  id: number;
  name: string;
  path: string;
}

export interface ModuleEdge {
  src_module_id: number;
  dst_module_id: number;
  edge_type: string;
  edges: number;
  references: number;
}

export interface ModuleGraph {
  analysis_id: string;
  modules: ModuleNode[];
  edges: ModuleEdge[];
  import_cycles: number[][];
}

export interface TestCase {
  id: number;
  node_id: string;
  path: string;
  qualified_name: string;
  symbol_id: number | null;
  collection_status: string;
}

export interface ChangeSet {
  id: string;
  repository_id: string;
  status: string;
  base_ref: string;
  target_ref: string;
  base_sha: string | null;
  target_sha: string | null;
  base_run_id: string | null;
  target_run_id: string | null;
  diff_algorithm: string | null;
  summary: Record<string, unknown> | null;
  timings_ms: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  job: Job | null;
}

export interface ChangedFile {
  id: number;
  change_type: string;
  old_path: string | null;
  new_path: string | null;
  language: string | null;
  role: string | null;
  mapping_status: string;
  similarity: number | null;
  added_lines: number;
  deleted_lines: number;
  old_ranges: number[][];
  new_ranges: number[][];
}

export interface ChangedSymbol {
  id: number;
  change_kind: string;
  symbol_kind: string;
  qualified_name: string;
  base_symbol_id: number | null;
  target_symbol_id: number | null;
  signature_changed: boolean;
  body_changed: boolean;
  decorators_changed: boolean;
  bases_changed: boolean;
  changed_lines: number;
}

export interface RiskFactor {
  factor: string;
  points: number;
  value: unknown;
  detail: string;
}

export interface ImpactReport {
  id: string;
  change_set_id: string;
  engine_version: string;
  completeness: "COMPLETE" | "PARTIAL" | "UNKNOWN";
  completeness_reasons: Record<string, unknown>[];
  risk_level: "LOW" | "MEDIUM" | "HIGH";
  risk_factors: RiskFactor[];
  recommended_scope: "NONE" | "TARGETED" | "BROAD" | "FULL_SUITE";
  scope_reasons: string[];
  counts: Record<string, unknown>;
  limits: Record<string, unknown>;
  uncertainty: {
    unresolved_references_in_impacted_code?: number;
    dynamic_references_in_impacted_code?: number;
    possible_hidden_callers?: { name: string; source: string; raw_text: string }[];
    names_too_common_to_match?: string[];
    dynamic_samples?: { source: string; raw_text: string }[];
  };
  created_at: string;
  disclaimer: string;
}

export interface ExplanationStep {
  from: string;
  to: string;
  edge_type: string;
  confidence: string;
}

export interface ImpactItem {
  id: number;
  target_symbol_id: number | null;
  base_symbol_id: number | null;
  qualified_name: string;
  symbol_kind: string;
  path: string | null;
  is_test: boolean;
  depth: number;
  strength: string;
  seed_qualified_name: string;
  seed_reason: string;
  graph: string;
  explanation: ExplanationStep[];
}

export interface TestPlan {
  id: string;
  change_set_id: string;
  analysis_run_id: string;
  policy: string;
  budget: Record<string, unknown> | null;
  scope: string;
  fallback_reason: string | null;
  total_tests: number;
  selected_tests: number;
  estimated_seconds: number | null;
  budget_limited: boolean;
  evidence_sources: Record<string, unknown>;
  created_at: string;
}

export interface PlanEvidence {
  kind: string;
  weight: number;
  depth: number;
  seed?: string;
  seed_reason?: string;
  path?: ExplanationStep[];
  lines?: Record<string, number[]>;
  reason?: string;
  imports_modules?: string[];
}

export interface TestPlanItem {
  id: number;
  node_id: string;
  selected: boolean;
  rank: number;
  score: number;
  evidence: PlanEvidence[];
  exclusion_reason: string | null;
  estimated_seconds: number | null;
}

export type RunOutcome =
  | "PASSED"
  | "FAILED"
  | "TIMED_OUT"
  | "CANCELLED"
  | "INFRASTRUCTURE_ERROR"
  | "COLLECTION_ERROR"
  | "SECURITY_POLICY_BLOCKED"
  | "RESOURCE_LIMIT_EXCEEDED";

export interface TestRun {
  id: string;
  repository_id: string;
  analysis_run_id: string;
  snapshot_id: string;
  commit_sha: string;
  plan_id: string | null;
  mode: string;
  requested_node_ids: string[];
  coverage: boolean;
  status: "QUEUED" | "RUNNING" | "COMPLETED";
  outcome: RunOutcome | null;
  outcome_reason: string | null;
  pytest_exit_code: number | null;
  container_exit_code: number | null;
  counts: Record<string, number> | null;
  duration_ms: number | null;
  image: string;
  limits: Record<string, unknown>;
  output_truncated: boolean;
  cleanup_ok: boolean | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  job: Job | null;
}

export interface TestResult {
  id: number;
  node_id: string;
  outcome: string;
  duration_ms: number;
  message: string | null;
}

export interface RunLogs {
  stdout_tail: string | null;
  stderr_tail: string | null;
  output_truncated: boolean;
  note: string;
}

export interface CoverageSummary {
  test_run_id: string;
  files: { path: string; lines_covered: number; tests: number }[];
  note: string;
}

export interface Evidence {
  id: string;
  category: "SOURCE_FACT" | "STATIC_RELATIONSHIP" | "RUNTIME_OBSERVATION" | "UNCERTAINTY";
  statement: string;
  path: string | null;
  start_line: number | null;
  end_line: number | null;
  symbol_id: number | null;
  qualified_name: string | null;
  text: string | null;
  commit_sha: string;
}

export interface QuestionAnswer {
  question: string;
  intent: string;
  analysis_id: string;
  commit_sha: string;
  mode: "deterministic" | "model";
  answer_markdown: string;
  model_explanation: string | null;
  model_note: string | null;
  insufficient_evidence: boolean;
  matched_symbols: string[];
  evidence: Evidence[];
}

/** ``GET /analyses/{id}/insights``: the repository health report. */
export interface InsightComponent {
  key: string;
  label: string;
  /** ``null`` when the analysis has no data for this component. */
  score: number | null;
  /** Share of the overall score actually given, after rescaling for dropped components. */
  weight: number;
  detail: string;
}

export interface InsightSymbolRef {
  symbol_id: number;
  qualified_name: string;
  kind?: string;
  file_id?: number | null;
  path: string;
  line: number;
}

export type FindingSeverity = "high" | "medium" | "low" | "info";

export interface InsightEvidence {
  path: string | null;
  line: number | null;
  symbol_id: number | null;
  file_id?: number | null;
  label: string;
}

export interface InsightFinding {
  id: string;
  severity: FindingSeverity;
  category: string;
  title: string;
  detail: string;
  evidence: InsightEvidence[];
}

export interface Insights {
  analysis_id: string;
  results_run_id?: string;
  commit_sha: string;
  score: {
    /** Both ``null`` only when no component has data. */
    overall: number | null;
    grade: string | null;
    components: InsightComponent[];
    method: string;
  };
  overview: {
    files: number;
    source_files: number;
    test_files: number;
    bytes: number;
    languages: { language: string; files: number; bytes: number }[];
    roles: Record<string, number>;
    modules: number;
    symbols: number;
    functions: number;
    classes: number;
    methods: number;
  };
  complexity: {
    available: boolean;
    functions?: number;
    distribution: { bucket: string; count: number }[];
    mean: number | null;
    p90: number | null;
    top: (InsightSymbolRef & { complexity: number; lines: number })[];
  };
  size: {
    longest_functions: (InsightSymbolRef & { lines: number })[];
    largest_files: { file_id: number; path: string; bytes: number; symbols: number; lines?: number }[];
  };
  documentation: {
    public_symbols: number;
    documented: number;
    coverage: number | null;
    undocumented: (InsightSymbolRef & { callers: number })[];
  };
  testing: {
    test_files: number;
    test_functions: number;
    modules_with_tests: number;
    modules_without_tests: number;
    /** Share of source modules a test imports or references; not line coverage. */
    coverage: number | null;
    untested_modules: {
      module: string;
      symbol_id?: number | null;
      file_id?: number | null;
      path: string;
      symbols: number;
      fan_in: number;
    }[];
  };
  coupling: {
    modules: { module: string; symbol_id?: number | null; path?: string; fan_in: number; fan_out: number; instability: number | null }[];
    cycles: string[][];
    hubs: (InsightSymbolRef & { callers: number })[];
  };
  dead_code: {
    count: number;
    candidates: (InsightSymbolRef & { kind: string; lines: number })[];
    note: string;
  };
  /** Counts per stored resolution status, plus the rate (external references excluded). */
  references: Record<string, number | null> & { resolution_rate?: number | null };
  findings: InsightFinding[];
}
