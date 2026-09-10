import test from "node:test";
import assert from "node:assert/strict";
import {
  sulfurComposition,
  chartTimeLabel,
  sourceEpoch,
} from "../src/visualization.ts";
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
