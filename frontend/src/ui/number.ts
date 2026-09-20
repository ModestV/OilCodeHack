/* Number parsing/formatting shared by NumberField: accepts "," and ".". */

export function parseNumber(text: string): number | null {
  const cleaned = text.replace(/\s/g, "").replace(",", ".");
  if (cleaned === "" || cleaned === "-" || cleaned === "." || cleaned === "-.")
    return null;
  if (!/^-?\d*\.?\d*$/.test(cleaned)) return null;
  const value = Number(cleaned);
  return Number.isFinite(value) ? value : null;
}

export function formatNumberInput(
  value: number | null | undefined,
  decimals?: number,
): string {
  if (value == null || !Number.isFinite(value)) return "";
  const text =
    decimals == null
      ? String(value)
      : value
          .toFixed(decimals)
          .replace(/(\.\d*?)0+$/, "$1")
          .replace(/\.$/, "");
  return text.replace(".", ",");
}

export function stepValue(
  current: number | null,
  step: number,
  direction: 1 | -1,
  min?: number,
  max?: number,
): number {
  const base =
    current ?? (direction > 0 ? (min ?? 0) - step : (max ?? 0) + step);
  const decimals = Math.max(
    (String(step).split(".")[1] || "").length,
    (String(base).split(".")[1] || "").length,
  );
  let next = Number((base + direction * step).toFixed(decimals));
  if (min != null) next = Math.max(min, next);
  if (max != null) next = Math.min(max, next);
  return next;
}
