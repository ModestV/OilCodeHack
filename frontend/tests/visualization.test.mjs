import test from "node:test";
import assert from "node:assert/strict";
import {
  sulfurComposition,
  chartTimeLabel,
  sourceEpoch,
} from "../src/visualization.ts";
import { buildOperatorAssessment } from "../src/operatorStatus.ts";
const summary = (values) => ({
  from: "2025-07-01T00:00:00",
  to: "2025-07-02T00:00:00",
  sulfur: {
    pak_observed_minutes: 720,
    pak_exceed_minutes: 120,
    pak_suspect_minutes: 360,
    ...values,
  },
});
test("coverage partitions time, not sample counts; exceedance is part of trusted coverage", () => {
  const parts = sulfurComposition(summary({}));
  assert.deepEqual(
    parts.map((p) => p.minutes),
    [600, 120, 360, 360],
  );
  assert.equal(
    parts.reduce((n, p) => n + p.percent, 0),
    100,
  );
});
test("flatline and missing data cannot appear as normal operation", () => {
  assert.deepEqual(
    sulfurComposition(
      summary({
        pak_observed_minutes: 0,
        pak_exceed_minutes: 0,
        pak_suspect_minutes: 1440,
      }),
    ).map((p) => p.percent),
    [0, 0, 100, 0],
  );
  assert.deepEqual(
    sulfurComposition(
      summary({
        pak_observed_minutes: 0,
        pak_exceed_minutes: 0,
        pak_suspect_minutes: 0,
      }),
    ).map((p) => p.percent),
    [0, 0, 0, 100],
  );
});
test("time display preserves source time and includes dates on long ranges", () => {
  const t = sourceEpoch("2025-07-01T12:03:00");
  assert.equal(chartTimeLabel(t, 3600000), "12:03");
  assert.equal(chartTimeLabel(t, 3600000, true), "01.07\n12:03");
  assert.equal(chartTimeLabel(t, 86400000), "01.07\n12:03");
  assert.equal(chartTimeLabel(t, 400 * 86400000), "01.07.2025");
});

const periodAssessment = (sulfur) =>
  buildOperatorAssessment({
    mode: "period",
    snapshot: null,
    summary: {
      from: "2025-03-16T00:00:00",
      to: "2025-03-19T00:00:00",
      metrics: [],
      agreement: [],
      comparison_from: "2025-03-13T00:00:00",
      comparison_to: "2025-03-16T00:00:00",
      sulfur,
    },
    metrics: [],
    pinned: [],
  });

test("operator status prioritizes a measured sulfur exceedance", () => {
  const result = periodAssessment({
    lab_count: 3,
    lab_exceed_count: 3,
    lab_exceed_fraction: 1,
    pak_observed_minutes: 4320,
    pak_exceed_minutes: 3330,
    pak_coverage_fraction: 1,
    pak_suspect_minutes: 0,
  });
  assert.equal(result.level, "danger");
  assert.equal(result.findings[0].code, "sulfur-exceeded");
});

test("operator status separates incomplete and missing data from a safe conclusion", () => {
  const incomplete = periodAssessment({
    lab_count: 1,
    lab_exceed_count: 0,
    lab_exceed_fraction: 0,
    pak_observed_minutes: 600,
    pak_exceed_minutes: 0,
    pak_coverage_fraction: 0.5,
    pak_suspect_minutes: 0,
  });
  const missing = periodAssessment({
    lab_count: 0,
    lab_exceed_count: 0,
    lab_exceed_fraction: null,
    pak_observed_minutes: 0,
    pak_exceed_minutes: 0,
    pak_coverage_fraction: null,
    pak_suspect_minutes: 0,
  });
  assert.equal(incomplete.level, "warning");
  assert.equal(missing.level, "unknown");
});
