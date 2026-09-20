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
  /** Feed sulphur, % mass (LIMS hydro-treating point 1). */
  baseline_feed_sulfur: number;
  feed_sulfur: number;
  current_sulfur?: number;
  current_t95?: number;
  current_cetane?: number;
  targets: {
    sulfur_max: number;
    t95_max: number;
    cetane_min: number;
    max_exceedance_probability: number;
  };
  /** Log-domain surrogate coefficients: ln(mg/kg) per °C, per % feed, per MPa. */
  parameters: {
    lag_minutes: number;
    feed_sulfur_elasticity: number;
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
  baseline: {
    sulfur: number;
    sulfur_source: string;
    t95: number | null;
    cetane: number | null;
    feed_sulfur: number;
    feed_sulfur_source: string;
    baseline_kind: "model_forecast" | "flat_baseline";
  };
  controls: Record<
    "ht.T6" | "ht.F9" | "ht.P13",
    {
      current: number | null;
      change: number;
      recommended: number | null;
      relative: boolean;
      model_change_limit: number;
    }
  >;
  predicted_sulfur: number;
  predicted_sulfur_lower: number | null;
  predicted_sulfur_upper: number | null;
  exceedance_probability: number | null;
  exceedance_target_met: boolean | null;
  steady_state_sulfur: number;
  sulfur_target_met: boolean;
  hard_sulfur_limit_met: boolean;
  parameters: ScenarioRequest["parameters"];
  trajectory: {
    minute: number;
    timestamp: string;
    sulfur: number;
    baseline_sulfur: number;
  }[];
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

export interface DecisionTraceItem {
  step: number;
  role: "quality" | "reliability" | "optimization";
  status: string;
  summary: string;
}

export interface ForecastPoint {
  prediction: number;
  lower: number;
  upper: number;
  exceedance_probability: number;
}

export interface SulfurForecast {
  at: string;
  status: "ok" | "abstain";
  reasons: string[];
  target_time: string;
  horizon_minutes: number;
  lims_publication_delay_minutes: number;
  target: { metric_id: string; unit: string };
  hard_limit: number;
  nowcast: ForecastPoint | null;
  prediction: number | null;
  prediction_lower: number | null;
  prediction_upper: number | null;
  exceedance_probability: number | null;
  alarm_probability: number;
  alarm_above_10: boolean | null;
  prediction_previous_lab: number | null;
  lab_anchor: { level: number | null; samples: number; pairs: number };
  analysers: Record<
    "q21" | "pak",
    {
      metric_id: string;
      value: number | null;
      timestamp: string | null;
      age_minutes?: number;
      offset: number | null;
      adjusted: number | null;
    }
  >;
  horizons: ({ minutes: number } & ForecastPoint)[];
  feature_cutoff: string;
  feature_time: string | null;
  feature_count: number;
  imputed_feature_count: number;
  model: {
    name: string;
    artifact: string;
    artifact_sha256: string;
    model_columns: string[];
  };
  leakage_check: {
    passed: boolean;
    feature_time: string | null;
    cutoff: string;
    violations: number;
  };
  warnings: string[];
}

export interface DecisionResult {
  at: string;
  status: "recommendation" | "abstain";
  basis: "scenario_only" | "observed_and_forecast";
  agents?: { quality: { evidence: { sulfur: {
    source: string | null; value: number | null; timestamp: string | null;
    available_at: string | null; freshness: string;
  } } } };
  recommendation: {
    action: string;
    predicted_sulfur: number;
    predicted_sulfur_lower?: number | null;
    predicted_sulfur_upper?: number | null;
    exceedance_probability?: number | null;
    target_sulfur: number;
    target_met: boolean;
    controls: ScenarioResult["controls"];
    model_forecast: SulfurForecast | null;
  } | null;
  scenario: ScenarioResult | null;
  candidates?: {
    id: string;
    label: string;
    status: string;
    feasible: boolean;
    reason?: string | null;
    predicted_sulfur?: number;
    exceedance_probability?: number | null;
    target_met?: boolean;
    effort?: number;
    objectives?: {
      throughput_change_pct: number;
      energy_cost_index: number;
      regime_severity: { index: number | null; class?: string };
      ranking_loss: number | null;
    };
    controls?: ScenarioResult["controls"];
    safety_gate?: { passed: boolean; reasons: string[] };
    scenario?: ScenarioResult | null;
  }[];
  selected_candidate?: string | null;
  safety_gate?: { passed: boolean; reasons: string[] };
  abstain: { reason: string; missing: string[] } | null;
  forecast: SulfurForecast | null;
  trace: DecisionTraceItem[];
  assumptions: string[];
}

