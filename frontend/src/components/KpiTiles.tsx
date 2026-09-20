import { AlertTriangle, Settings2 } from "lucide-react";
import type { Metric, Snapshot, Stat, Summary } from "../types";
import { formatNumber } from "../visualization";
import { Tooltip } from "../ui/Tooltip";

const statisticName: Record<string, string> = {
  median: "медиана",
  mean: "среднее",
  min: "минимум",
  max: "максимум",
  p05: "P05",
  p95: "P95",
  std: "σ",
};

/** Pinned metrics as compact tiles on the overview. */
export function KpiTiles({
  pinned,
  metrics,
  mode,
  snapshot,
  summary,
  statistic,
  onConfigure,
}: {
  pinned: string[];
  metrics: Metric[];
  mode: "period" | "moment";
  snapshot: Snapshot | null;
  summary: Summary | null;
  statistic: string;
  onConfigure: () => void;
}) {
  if (!pinned.length) return null;
  const metricById = new Map(metrics.map((m) => [m.id, m]));
  const values = new Map(snapshot?.values.map((v) => [v.metric_id, v]));
  const stats = new Map(summary?.metrics.map((s) => [s.metric_id, s]));
  return (
    <section className="kpi" aria-label="Ключевые показатели">
      <header className="kpi-head">
        <h3>
          Показатели
          <small>
            {mode === "period"
              ? ` · ${statisticName[statistic] ?? statistic} за период`
              : " · на момент"}
          </small>
        </h3>
        <Tooltip text="Настроить набор показателей">
          <button
            type="button"
            className="icon-button"
            aria-label="Настроить набор показателей"
            onClick={onConfigure}
          >
            <Settings2 />
          </button>
        </Tooltip>
      </header>
      <div className="kpi-grid">
        {pinned.map((id) => {
          const metric = metricById.get(id);
          const value = values.get(id);
          const stat = stats.get(id);
          const displayed =
            mode === "period"
              ? (stat?.[statistic as keyof Stat] as number | null | undefined)
              : value?.value;
          const flagged = !!(mode === "period"
            ? stat?.suspect_count
            : value?.flags.length || (value && value.freshness !== "fresh"));
          const source = metric?.source.toUpperCase();
          return (
            <article
              key={id}
              className={`kpi-tile${flagged ? " flagged" : ""}`}
            >
              <span className="kpi-label" title={undefined}>
                {metric?.label || id}
              </span>
              <strong className="num">
                {formatNumber(displayed)}
                {metric?.unit && <small> {metric.unit}</small>}
              </strong>
              <small className="kpi-meta">
                {source}
                {mode === "period" && stat ? ` · n=${stat.count}` : ""}
                {flagged && (
                  <>
                    {" "}
                    <AlertTriangle aria-hidden="true" /> проверить
                  </>
                )}
              </small>
            </article>
          );
        })}
      </div>
    </section>
  );
}
