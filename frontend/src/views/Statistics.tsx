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

export function Statistics({
  metrics,
  summary,
  distribution,
  selected,
  setSelected,
}: {
  metrics: Metric[];
  summary: Summary | null;
  distribution: Distribution | null;
  selected: string;
  setSelected: (s: string) => void;
}) {
  const theme = useChartTheme();
  const map = useMemo(() => metricMap(metrics), [metrics]);
  const [query, setQuery] = useState("");
  const selectedStat = summary?.metrics.find((s) => s.metric_id === selected);
  const tableRows = useMemo(
    () =>
      summary?.metrics
        .filter((s) =>
          `${s.metric_id} ${map.get(s.metric_id)?.label || ""}`
            .toLowerCase()
            .includes(query.toLowerCase()),
        )
        .sort(
          (a, b) =>
            Number(b.metric_id === selected) - Number(a.metric_id === selected),
        ) || [],
    [summary, map, query, selected],
  );
  const option = useMemo(() => {
    const bins = distribution?.bins || [];
    return {
      grid: { left: 48, right: 20, top: 24, bottom: 52 },
      tooltip: {},
      xAxis: {
        type: "category",
        data: bins.map((b) => `${format(b.from)}–${format(b.to)}`),
        axisLabel: { rotate: 35, hideOverlap: true },
      },
      yAxis: { type: "value", minInterval: 1, name: "Измерений" },
      series: [
        {
          type: "bar",
          data: bins.map((b) => b.count),
          itemStyle: { color: theme.accent, borderRadius: [2, 2, 0, 0] },
        },
      ],
    };
  }, [distribution, theme]);
  const boxOption = useMemo(
    () => ({
      grid: { left: 45, right: 20, top: 25, bottom: 35 },
      tooltip: { trigger: "item" },
      xAxis: { type: "category", data: ["Распределение"] },
      yAxis: { type: "value", scale: true },
      series: [
        {
          type: "boxplot",
          data: [distribution?.quartiles ?? []],
          itemStyle: { color: theme.band, borderColor: theme.accent },
        },
      ],
    }),
    [distribution, theme],
  );
  return (
    <>
      <section className="panel">
        <header>
          <div>
            <h2>Статистика периода</h2>
            <p>Конец периода не включён в расчёт</p>
          </div>
          <select
            aria-label="Показатель распределения"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            {metrics.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label} · {m.source.toUpperCase()} · {m.id}
              </option>
            ))}
          </select>
        </header>
        <h3 className="selected-stat-title">
          {map.get(selected)?.label}
          {map.get(selected)?.unit && (
            <>
              {" "}
              <small>{map.get(selected)?.unit}</small>
            </>
          )}
        </h3>
        {distribution &&
        distribution.count > 1 &&
        distribution.quartiles[0] !== distribution.quartiles[4] ? (
          <div className="distribution-grid">
            <div>
              <h3>Распределение · n={distribution?.count || 0}</h3>
              {distribution.bins.length ? (
                <Chart option={option} height={240} />
              ) : (
                <Empty text="Нет измерений в периоде" />
              )}
            </div>
            <div>
              <h3>
                Квартили
                {map.get(selected)?.unit ? ` · ${map.get(selected)?.unit}` : ""}
              </h3>
              {distribution?.count ? (
                <Chart height={240} option={boxOption} />
              ) : null}
            </div>
          </div>
        ) : (
          <div className="single-observation">
            <span>
              {distribution?.count === 1
                ? "Единственное измерение"
                : distribution?.count
                  ? "Одинаковые значения"
                  : "Нет измерений в периоде"}
            </span>
            {!!distribution?.count && (
              <>
                <strong>
                  {format(distribution.quartiles[2])}{" "}
                  <small>{map.get(selected)?.unit}</small>
                </strong>
                <small>
                  {distribution.count === 1
                    ? stamp(selectedStat?.first_at)
                    : `n=${distribution.count} · размах 0`}
                </small>
              </>
            )}
          </div>
        )}
        <MedianComparison
          stat={selectedStat}
          unit={map.get(selected)?.unit || null}
        />
        <p className="muted">
          Сравнение с {stamp(summary?.comparison_from)} до{" "}
          {stamp(summary?.comparison_to)}. Лабораторные n — отдельные пробы.
        </p>
        <div className="statistics-toolbar">
          <label className="search">
            <Search />
            <input
              aria-label="Поиск в статистике"
              placeholder="Показатель или тег"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          <small>Показателей: {tableRows.length}</small>
        </div>
        <div className="table-scroll statistics-table">
          <table>
            <thead>
              <tr>
                <th>Показатель</th>
                <th>n</th>
                <th>Медиана</th>
                <th>Среднее</th>
                <th>Мин.</th>
                <th>Макс.</th>
                <th>
                  P05–P95{" "}
                  <HelpTooltip label="P05–P95">
                    Диапазон, внутри которого находится 90% измерений: от 5-го
                    до 95-го процентиля.
                  </HelpTooltip>
                </th>
                <th>Пред. медиана</th>
                <th>Изменение</th>
                <th>
                  σ / IQR / размах{" "}
                  <HelpTooltip label="σ и IQR">
                    σ показывает разброс относительно среднего. IQR — ширину
                    центральной половины измерений.
                  </HelpTooltip>
                </th>
                <th>Первое / последнее</th>
                <th>Время min / max</th>
                <th>Ошиб. / подозр.</th>
              </tr>
            </thead>
            <tbody>
              {tableRows.map((s) => (
                <tr
                  key={s.metric_id}
                  className={s.metric_id === selected ? "selected-row" : ""}
                >
                  <td>
                    {map.get(s.metric_id)?.label || s.metric_id}
                    <small>{map.get(s.metric_id)?.unit || ""}</small>
                  </td>
                  <td>{s.count}</td>
                  <td>{format(s.median)}</td>
                  <td>{format(s.mean)}</td>
                  <td>{format(s.min)}</td>
                  <td>{format(s.max)}</td>
                  <td>
                    {format(s.p05)}–{format(s.p95)}
                  </td>
                  <td>{format(s.previous_median)}</td>
                  <td>
                    {format(s.median_change)}
                    <small>От первого: {format(s.change)}</small>
                  </td>
                  <td>
                    {format(s.std)} / {format(s.iqr)} / {format(s.range)}
                  </td>
                  <td>
                    {format(s.first)} / {format(s.last)}
                    <small>
                      {s.first_at?.replace("T", " ")} —{" "}
                      {s.last_at?.replace("T", " ")}
                    </small>
                  </td>
                  <td>
                    {s.min_at?.replace("T", " ")}
                    <small>{s.max_at?.replace("T", " ")}</small>
                  </td>
                  <td>
                    {s.invalid_count} / {s.suspect_count}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <details className="agreement analysis-detail">
          <summary>
            Согласованность ЛИМС и ПАК
            <HelpTooltip label="Согласованность ЛИМС и ПАК">
              Смещение показывает систематическую разницу ПАК и ЛИМС. Средняя
              абсолютная ошибка показывает типичный размер расхождения без учёта
              его направления.
            </HelpTooltip>
          </summary>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Показатель</th>
                  <th>Пар</th>
                  <th>Смещение ПАК − ЛИМС</th>
                  <th>Средняя абсолютная ошибка</th>
                </tr>
              </thead>
              <tbody>
                {summary?.agreement.map((item) => (
                  <tr key={item.metric_id}>
                    <td>{map.get(item.metric_id)?.label || item.metric_id}</td>
                    <td>{item.n}</td>
                    <td>{format(item.bias)}</td>
                    <td>{format(item.mae)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
        <details className="analysis-detail">
          <summary>Лабораторный паспорт за период</summary>
          <Passport
            metrics={metrics}
            snapshot={null}
            mode="period"
            summary={summary}
          />
        </details>
      </section>
    </>
  );
}

function Passport({
  metrics,
  snapshot,
  mode,
  summary,
}: {
  metrics: Metric[];
  snapshot: Snapshot | null;
  mode: "period" | "moment";
  summary: Summary | null;
}) {
  const map = new Map(snapshot?.values.map((v) => [v.metric_id, v]));
  const stats = new Map(summary?.metrics.map((v) => [v.metric_id, v]));
  const labs = metrics.filter(
    (m) =>
      m.source === "lims" && m.plant === "ht" && m.id.startsWith("lims.ht.2."),
  );
  return (
    <section className="panel">
      <h2>
        {mode === "period"
          ? "Лабораторные показатели за период"
          : "Последние лабораторные измерения"}{" "}
        · точка 2
      </h2>
      <div className="passport">
        {labs.length ? (
          labs.map((m) => {
            const v = map.get(m.id);
            return (
              <div key={m.id}>
                <span>{m.label}</span>
                <b>
                  {format(
                    mode === "period" ? stats.get(m.id)?.median : v?.value,
                  )}
                  {m.unit ? ` ${m.unit}` : ""}
                </b>
                <small>
                  {mode === "period"
                    ? "n=" +
                      (stats.get(m.id)?.count || 0) +
                      " · " +
                      format(stats.get(m.id)?.min) +
                      "–" +
                      format(stats.get(m.id)?.max)
                    : v?.timestamp?.replace("T", " ") || "Нет пробы к моменту"}
                </small>
                {mode === "moment" && (
                  <>
                    <small className={v?.freshness === "fresh" ? "" : "warn"}>
                      {freshness(v?.freshness)} · {format(v?.age_minutes, 0)}{" "}
                      мин
                    </small>
                    <FlagLine flags={v?.flags} />
                  </>
                )}
              </div>
            );
          })
        ) : (
          <Empty text="Лабораторные показатели точки 2 отсутствуют" />
        )}
      </div>
    </section>
  );
}
