import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  ArrowRight,
  CircleHelp,
  ChevronDown,
  Database,
  Download,
  Info,
  ShieldCheck,
  ChartNoAxesCombined,
  Search,
} from "lucide-react";
import { Chart } from "../components/Chart";
import {
  SulfurAtMoment,
  MedianComparison,
  QualityRanking,
} from "../components/MonitoringCharts";
import { HelpTooltip } from "../components/HelpTooltip";
import { chartTimeLabel, sourceEpoch } from "../visualization";
import { useChartTheme } from "../ui/useChartTheme";
import {
  buildOperatorAssessment,
  type AttentionTarget,
  type OperatorAssessment,
} from "../operatorStatus";
import { api, exportUrl } from "../api";
import type {
  Distribution,
  Formula,
  Issue,
  Manifest,
  Metric,
  Quality,
  SeriesResponse,
  Snapshot,
  Stat,
  Summary,
} from "../types";
import {
  Empty,
  FlagLine,
  epoch,
  format,
  freshness,
  metricMap,
  stamp,
} from "./shared";

export function Kip({
  metrics,
  snapshot,
  mode = "moment",
  summary,
  onTrend,
  selected = [],
}: {
  metrics: Metric[];
  snapshot: Snapshot | null;
  mode?: "period" | "moment";
  summary?: Summary | null;
  onTrend?: (id: string) => void;
  selected?: string[];
}) {
  const [q, setQ] = useState(""),
    [plant, setPlant] = useState<"all" | "avt" | "ht">("all");
  const vals = useMemo(
    () => new Map(snapshot?.values.map((v) => [v.metric_id, v])),
    [snapshot],
  );
  const stats = useMemo(
    () => new Map(summary?.metrics.map((v) => [v.metric_id, v])),
    [summary],
  );
  const rows = useMemo(() => {
    const selectedSet = new Set(selected);
    return metrics
      .filter(
        (m) =>
          m.source === "kip" &&
          (plant === "all" || m.plant === plant) &&
          `${m.label} ${m.id} ${m.group} ${m.description || ""}`
            .toLowerCase()
            .includes(q.toLowerCase()),
      )
      .sort((a, b) => {
        const aProblem =
          mode === "period"
            ? (stats.get(a.id)?.suspect_count ?? 0) > 0
            : (vals.get(a.id)?.flags.length ?? 0) > 0;
        const bProblem =
          mode === "period"
            ? (stats.get(b.id)?.suspect_count ?? 0) > 0
            : (vals.get(b.id)?.flags.length ?? 0) > 0;
        return (
          Number(bProblem) - Number(aProblem) ||
          Number(selectedSet.has(b.id)) - Number(selectedSet.has(a.id))
        );
      });
  }, [metrics, plant, q, mode, stats, vals, selected]);
  return (
    <section className="panel">
      <header>
        <div>
          <h2>
            Технология и КИП{" "}
            <HelpTooltip label="КИП">
              КИП — технологические контрольно-измерительные приборы установки.
            </HelpTooltip>
          </h2>
          <p>Показателей: {rows.length}</p>
        </div>
        <div className="filters">
          <input
            aria-label="Поиск КИП"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Тег, аппарат или описание"
          />
          <select
            aria-label="Установка"
            value={plant}
            onChange={(e) => setPlant(e.target.value as typeof plant)}
          >
            <option value="all">Все установки</option>
            <option value="avt">АВТ</option>
            <option value="ht">Гидроочистка</option>
          </select>
        </div>
      </header>
      <AvtScheme />
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Показатель / аппарат</th>
              <th>{mode === "period" ? "Медиана и диапазон" : "Значение"}</th>
              <th>
                {mode === "period"
                  ? "Первое / последнее измерение"
                  : "Время / возраст"}
              </th>
              <th>Качество</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => {
              const v = vals.get(m.id),
                s = stats.get(m.id);
              return (
                <tr key={m.id}>
                  <td>
                    {m.label}
                    <small>
                      {m.id} · {m.group}
                    </small>
                  </td>
                  <td>
                    {format(mode === "period" ? s?.median : v?.value)}
                    {m.unit ? ` ${m.unit}` : ""}
                    {mode === "period" ? (
                      <small>
                        мин. {format(s?.min)} · макс. {format(s?.max)} · n=
                        {s?.count || 0}
                      </small>
                    ) : (
                      <small className={v?.freshness === "fresh" ? "" : "warn"}>
                        {freshness(v?.freshness)}
                      </small>
                    )}
                  </td>
                  <td>
                    {mode === "period" ? (
                      <>
                        {stamp(s?.first_at)}
                        <small>{stamp(s?.last_at)}</small>
                      </>
                    ) : (
                      <>
                        {stamp(v?.timestamp)}
                        <small>
                          {format(v?.age_minutes, 0)} мин · Δ {format(v?.delta)}
                        </small>
                      </>
                    )}
                  </td>
                  <td>
                    {mode === "period" ? (
                      <small>
                        Некорректных: {s?.invalid_count || 0} · подозрительных:{" "}
                        {s?.suspect_count || 0}
                      </small>
                    ) : (
                      <FlagLine flags={v?.flags} />
                    )}
                    <small className="warn">{m.mapping_warning || ""}</small>
                    {mode === "period" && m.available !== false && onTrend && (
                      <button
                        className="icon-btn kip-trend"
                        title={`Тренд ${m.id}`}
                        aria-label={`Открыть тренд ${m.id}`}
                        onClick={() => onTrend(m.id)}
                      >
                        <ChartNoAxesCombined />
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
function AvtScheme() {
  const [page, setPage] = useState(1);
  return (
    <details>
      <summary>Схема АВТ</summary>
      <div className="view-switch">
        {["К-1", "К-2", "К-10"].map((label, index) => (
          <button
            className={page === index + 1 ? "active" : ""}
            onClick={() => setPage(index + 1)}
            key={label}
          >
            {label}
          </button>
        ))}
      </div>
      <a href={"/api/reference/avt/" + page} target="_blank" rel="noreferrer">
        <img
          src={"/api/reference/avt/" + page}
          alt={"Технологическая схема АВТ, страница " + page}
          style={{ width: "100%", maxHeight: 620, objectFit: "contain" }}
        />
      </a>
    </details>
  );
}
