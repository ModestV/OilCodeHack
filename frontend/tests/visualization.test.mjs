import test from "node:test";
import assert from "node:assert/strict";
import {
  sulfurComposition,
  chartTimeLabel,
  composeChartOption,
  formatChartTimestamp,
  formatNumber,
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

test("formatNumber keeps UI values compact and detailed values precise", () => {
  assert.equal(formatNumber(null), "—");
  assert.equal(formatNumber(undefined), "—");
  assert.equal(formatNumber(10), "10");
  assert.equal(formatNumber(10.04), "10");
  assert.equal(formatNumber(10.05), "10,1");
  assert.equal(formatNumber(10.555, 2), "10,56");
});

test("formatChartTimestamp renders full tooltip timestamps", () => {
  const t = sourceEpoch("2025-07-01T12:03:00");
  assert.equal(formatChartTimestamp(t), "01.07.2025, 12:03");
});

test("chart composition formats axis tooltips without binary floats", () => {
  const t = sourceEpoch("2025-07-01T12:03:00");
  const option = composeChartOption({
    tooltip: { trigger: "axis" },
    series: [{ name: "ПАК", data: [[t, 0.30000000000000004]] }],
  });
  assert.equal(
    option.tooltip.formatter([
      {
        axisValue: t,
        seriesName: "ПАК",
        data: [t, 0.30000000000000004],
      },
    ]),
    "01.07.2025, 12:03\nПАК: 0,3",
  );
});

test("sulfur overview composition removes slider and zero min/max service series", () => {
  const t = sourceEpoch("2025-07-01T12:03:00");
  const option = composeChartOption({
    tooltip: { trigger: "axis" },
    legend: { top: 0, data: ["ЛИМС · пробы", "ПАК · медиана"] },
    dataZoom: [{ type: "inside" }, { type: "slider" }],
    xAxis: { type: "time" },
    yAxis: { type: "value", name: "мг/кг" },
    series: [
      { name: "ЛИМС · пробы", data: [[t, 10.234]] },
      { name: "ПАК · медиана", data: [[t, 10.234]] },
      { name: "ПАК · максимум", data: [[t, 10.234]] },
      { name: "ПАК · минимум", data: [[t, 10.234]] },
    ],
  });
  assert.equal(option.title, undefined);
  assert.deepEqual(option.legend.data, ["ЛИМС", "ПАК"]);
  assert.deepEqual(option.dataZoom, [{ type: "inside" }]);
  assert.deepEqual(
    option.series.map((item) => item.name),
    ["ЛИМС", "ПАК"],
  );
  assert.equal(option.yAxis.name, "");
});

test("sulfur overview keeps aggregated min/max out of tooltip and legend", () => {
  const t = sourceEpoch("2025-07-01T12:03:00");
  const option = composeChartOption({
    tooltip: { trigger: "axis" },
    legend: { top: 0, data: ["ЛИМС · пробы", "ПАК · медиана"] },
    dataZoom: [{ type: "inside" }, { type: "slider" }],
    xAxis: { type: "time" },
    yAxis: { type: "value", name: "мг/кг" },
    series: [
      { name: "ЛИМС · пробы", data: [[t, 9]] },
      { name: "ПАК · медиана", data: [[t, 9.5]] },
      { name: "ПАК · максимум", data: [[t, 11]] },
      { name: "ПАК · минимум", data: [[t, 8]] },
    ],
  });
  assert.deepEqual(option.legend.data, ["ЛИМС", "ПАК"]);
  assert.deepEqual(
    option.series.map((item) => item.name),
    ["ЛИМС", "ПАК", "ПАК · максимум", "ПАК · минимум"],
  );
  assert.equal(option.series[2].tooltip.show, false);
  assert.equal(option.series[3].tooltip.show, false);
  assert.equal(
    option.tooltip.formatter([
      { axisValue: t, seriesName: "ЛИМС", data: [t, 9] },
      { axisValue: t, seriesName: "ПАК", data: [t, 9.5] },
      { axisValue: t, seriesName: "ПАК · максимум", data: [t, 11] },
    ]),
    "01.07.2025, 12:03\nЛИМС: 9 мг/кг\nПАК: 9,5 мг/кг",
  );
});

test("sulfur overview legend only names sources present in the interval", () => {
  const t = sourceEpoch("2025-07-01T12:03:00");
  const option = composeChartOption({
    tooltip: { trigger: "axis" },
    legend: { data: ["ЛИМС · пробы"] },
    dataZoom: [{ type: "inside" }, { type: "slider" }],
    series: [{ name: "ЛИМС · пробы", data: [[t, 9]] }],
  });
  assert.deepEqual(option.legend.data, ["ЛИМС"]);
  assert.deepEqual(option.dataZoom, [{ type: "inside" }]);
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
