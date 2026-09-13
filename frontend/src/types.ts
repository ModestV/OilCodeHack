export type SourceKind = "kip" | "lims" | "pak";
export interface SourceMeta {
  kind: SourceKind;
  filename: string;
  rows: number;
  metrics: number;
  start: string | null;
  end: string | null;
  invalid_count: number;
  suspect_count: number;
  duplicate_count: number;
  [key: string]: unknown;
}
export interface Issue {
  code: string;
  message: string;
  count: number;
  metric_id?: string;
}
export interface Metric {
  id: string;
  label: string;
  source: SourceKind;
  unit: string | null;
  unit_status: "confirmed" | "inferred" | "unknown";
  plant: "avt" | "ht";
  point?: string;
  code: string;
  group: string;
  description?: string;
  primary?: boolean;
  mapping_warning?: string;
  [key: string]: unknown;
}
export interface Manifest {
  id: string;
  name: string;
  status: "ready" | "importing" | "error";
  error?: string;
  created_at: string;
  start: string | null;
  end: string | null;
  telemetry_start: string | null;
  telemetry_end: string | null;
  sources: SourceMeta[];
  metrics: Metric[];
  issues: Issue[];
  assumptions: string[];
}
export interface SnapshotValue {
  metric_id: string;
  value: number | null;
  timestamp: string | null;
  age_minutes: number | null;
  unit: string | null;
  flags: string[];
  freshness: "fresh" | "stale" | "missing";
  delta: number | null;
  reason?: string;
  available_at?: string | null;
}
export interface Alert {
  code?: string;
  message?: string;
  metric_id?: string;
  [key: string]: unknown;
}
export interface Snapshot {
  at: string;
  values: SnapshotValue[];
  alerts: Alert[];
}
export interface Stat {
  metric_id: string;
  count: number;
  invalid_count: number;
  suspect_count: number;
  mean: number | null;
  median: number | null;
  min: number | null;
  max: number | null;
  min_at: string | null;
  max_at: string | null;
  p05: number | null;
  p95: number | null;
  std: number | null;
  iqr: number | null;
  range: number | null;
  first: number | null;
  last: number | null;
  first_at: string | null;
  last_at: string | null;
  change: number | null;
  previous_median: number | null;
  median_change: number | null;
}
export interface Summary {
  from: string;
  to: string;
  metrics: Stat[];
  comparison_from: string;
  comparison_to: string;
  sulfur: {
    lab_count: number;
    lab_exceed_count: number;
    lab_exceed_fraction: number | null;
    pak_observed_minutes: number;
    pak_exceed_minutes: number;
    pak_coverage_fraction: number | null;
    pak_suspect_minutes: number;
  };
  agreement: {
    metric_id: string;
    n: number;
    bias: number | null;
    mae: number | null;
  }[];
}
export interface SeriesPoint {
  timestamp: string;
  value: number | null;
  min: number | null;
  max: number | null;
  flags: string[];
  count: number;
}
export interface SeriesResponse {
  series: { metric_id: string; points: SeriesPoint[] }[];
}
export interface Formula {
  id: string;
  label: string;
  plant: string;
  expression: string;
  version: string;
  status: "experimental" | "invalid" | "unresolved" | "verified";
  reason: string;
  inputs: {
    tag: string;
    value: number | null;
    timestamp: string | null;
    flags: string[];
  }[];
  substitution: string;
  result: number | null;
  unit: string | null;
}
export interface Quality {
  sources: SourceMeta[];
  issues: Issue[];
  assumptions: string[];
  metrics: {
    metric_id: string;
    count: number;
    invalid_count: number;
    suspect_count: number;
    flatline_count: number;
    start: string | null;
    end: string | null;
  }[];
}
export interface Distribution {
  metric_id: string;
  count: number;
  bins: { from: number; to: number; count: number }[];
  quartiles: [
    number | null,
    number | null,
    number | null,
    number | null,
    number | null,
  ];
}
export interface Settings {
  freshness_minutes: { kip: number; pak: number; lims: number };
}

export interface ScenarioRequest {
  at: string;
  horizon_minutes: number;
  step_minutes: number;
  baseline_feed_sulfur: number;
  feed_sulfur: number;
  current_sulfur?: number;
  current_t95?: number;
  current_cetane?: number;
  targets: { sulfur_max: number; t95_max: number; cetane_min: number };
  parameters: {
    lag_minutes: number;
    feed_sulfur_transfer: number;
    temperature_effect: number;
    feed_rate_effect: number;
    pressure_effect: number;
    cetane_gain_per_pct: number;
  };
  changes?: { temperature: number; feed_rate_pct: number; pressure: number };
  tanks: {
    name: string;
    share: number;
    sulfur: number;
    t95: number;
    cetane: number;
    cost_index: number;
  }[];
  additive_pct: number;
}

export interface ScenarioResult {
  at: string;
  horizon_minutes: number;
  step_minutes: number;
  baseline: { sulfur: number; t95: number | null; cetane: number | null };
  controls: Record<
    "ht.P8" | "ht.T11" | "ht.F19",
    {
      current: number | null;
      change: number;
      recommended: number | null;
      relative: boolean;
    }
  >;
  predicted_sulfur: number;
  sulfur_target_met: boolean;
  trajectory: { minute: number; timestamp: string; sulfur: number }[];
  blend: null | {
    sulfur: number;
    t95: number;
    cetane: number;
    cost_index: number;
    normalized_shares: { name: string; share: number }[];
    meets_targets: { sulfur: boolean; t95: boolean; cetane: boolean };
    all_targets_met: boolean;
  };
  assumptions: string[];
}
