import test from "node:test";
import assert from "node:assert/strict";
import {
  applyMask,
  parseMasked,
  formatMasked,
  monthGrid,
  addMinutes,
} from "../src/ui/datetime.ts";
import { parseNumber, formatNumberInput, stepValue } from "../src/ui/number.ts";

test("mask lays digits out progressively", () => {
  assert.equal(applyMask("0"), "0");
  assert.equal(applyMask("0608"), "06.08");
  assert.equal(applyMask("06082026"), "06.08.2026");
  assert.equal(applyMask("0608202600"), "06.08.2026 00");
  assert.equal(applyMask("060820260030"), "06.08.2026 00:30");
  assert.equal(applyMask("06.08.2026 00:30extra9"), "06.08.2026 00:30");
});

test("masked round trip and validation", () => {
  assert.equal(parseMasked("06.08.2026 00:30"), "2026-08-06T00:30:00");
  assert.equal(formatMasked("2026-08-06T00:30:00"), "06.08.2026 00:30");
  assert.equal(parseMasked("31.02.2026 00:00"), null);
  assert.equal(parseMasked("29.02.2024 23:59"), "2024-02-29T23:59:00");
  assert.equal(parseMasked("06.08.2026 24:00"), null);
  assert.equal(parseMasked("06.08.20"), null);
});

test("month grid starts on Monday and pads to full weeks", () => {
  const grid = monthGrid(2026, 8); // 1 Aug 2026 is a Saturday
  assert.equal(grid[0].filter((d) => d === null).length, 5);
  assert.equal(grid[0][5], 1);
  assert.equal(grid.flat().filter(Boolean).length, 31);
  assert.ok(grid.every((row) => row.length === 7));
});

test("addMinutes keeps naive time", () => {
  assert.equal(addMinutes("2026-08-06T00:00:00", 10), "2026-08-06T00:10:00");
  assert.equal(addMinutes("2026-08-06T00:00:00", -1440), "2026-08-05T00:00:00");
});

test("number field accepts comma and dot", () => {
  assert.equal(parseNumber("0,93"), 0.93);
  assert.equal(parseNumber("0.93"), 0.93);
  assert.equal(parseNumber("-10"), -10);
  assert.equal(parseNumber(""), null);
  assert.equal(parseNumber("abc"), null);
  assert.equal(parseNumber("1 000"), 1000);
  assert.equal(formatNumberInput(0.93), "0,93");
  assert.equal(formatNumberInput(2.5, 2), "2,5");
  assert.equal(formatNumberInput(0, 0), "0");
  assert.equal(formatNumberInput(10, 0), "10");
  assert.equal(formatNumberInput(120, 1), "120");
  assert.equal(formatNumberInput(null), "");
});

test("stepping respects bounds and decimals", () => {
  assert.equal(stepValue(0.9, 0.1, 1), 1);
  assert.equal(stepValue(1, 0.5, -1, 0.75), 0.75);
  assert.equal(stepValue(null, 5, 1, 0), 0);
  assert.equal(stepValue(9.5, 0.5, 1, undefined, 10), 10);
});
