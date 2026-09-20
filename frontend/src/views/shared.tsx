import { AlertTriangle, Database, Download } from "lucide-react";
import { exportUrl } from "../api";
import { sourceEpoch } from "../visualization";
import type { Metric } from "../types";

export const epoch = sourceEpoch;
export const stamp = (value: string | null | undefined) => {
  if (!value) return "Нет измерения";
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  return match
    ? `${match[3]}.${match[2]}.${match[1]}, ${match[4]}:${match[5]}`
    : value.replace("T", " ").slice(0, 16);
};
export const freshness = (s: string | undefined) =>
  s === "fresh" ? "Актуально" : s === "stale" ? "Устарело" : "Нет данных";
export const format = (v: number | null | undefined, d = 2) =>
  v == null
    ? "—"
    : new Intl.NumberFormat("ru-RU", { maximumFractionDigits: d }).format(v);
export const metricMap = (m: Metric[]) => new Map(m.map((x) => [x.id, x]));
const flagNames: Record<string, string> = {
  flatline: "Возможное зависание",
  suspect: "Подозрительное значение",
  invalid: "Некорректное значение",
  conflict: "Конфликт измерений",
};
export function FlagLine({ flags = [] }: { flags?: string[] }) {
  return flags.length ? (
    <span className="flags">
      <AlertTriangle /> {flags.map((f) => flagNames[f] || f).join(", ")}
    </span>
  ) : null;
}
export function Empty({ text }: { text: string }) {
  return (
    <div className="empty">
      <Database />
      <p>{text}</p>
    </div>
  );
}

export function ExportButton({
  id,
  ids,
  from,
  to,
  exclude = false,
}: {
  id: string;
  ids: string[];
  from: string;
  to: string;
  exclude?: boolean;
}) {
  return (
    <a
      className="secondary export"
      href={exportUrl(id, ids, from, to, exclude)}
    >
      <Download /> CSV
    </a>
  );
}
