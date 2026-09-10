import type { Summary } from "./types";

export const sourceEpoch = (value: string) =>
  Date.parse(value.replace(/Z$/, "") + "Z");

export function chartTimeLabel(
  value: number,
  spanMs: number,
  crossesDay = false,
): string {
  const date = new Date(value).toISOString();
  if (spanMs < 86400000 && !crossesDay) return date.slice(11, 16);
  const day = `${date.slice(8, 10)}.${date.slice(5, 7)}`;
  return spanMs > 366 * 86400000
    ? `${day}.${date.slice(0, 4)}`
    : `${day}\n${date.slice(11, 16)}`;
}

export function sulfurComposition(summary: Summary) {
  const total = Math.max(
    0,
    (sourceEpoch(summary.to) - sourceEpoch(summary.from)) / 60000,
  );
  const trusted = Math.min(
    total,
    Math.max(0, summary.sulfur.pak_observed_minutes),
  );
  const above = Math.min(
    trusted,
    Math.max(0, summary.sulfur.pak_exceed_minutes),
  );
  const suspect = Math.min(
    total - trusted,
    Math.max(0, summary.sulfur.pak_suspect_minutes),
  );
  return [
    {
      key: "below",
      label: "Не выше 10 мг/кг",
      minutes: trusted - above,
      color: "#0079c2",
    },
    { key: "above", label: "Выше 10 мг/кг", minutes: above, color: "#bf3d42" },
    {
      key: "suspect",
      label: "Подозрительный сигнал",
      minutes: suspect,
      color: "#b77b19",
    },
    {
      key: "missing",
      label: "Нет достоверного наблюдения",
      minutes: Math.max(0, total - trusted - suspect),
      color: "#748391",
    },
  ].map((item) => ({
    ...item,
    percent: total > 0 ? (item.minutes / total) * 100 : 0,
  }));
}
