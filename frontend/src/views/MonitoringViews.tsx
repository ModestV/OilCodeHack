import { useEffect, useState, type ReactNode } from "react";
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
import {
  buildOperatorAssessment,
  type AttentionTarget,
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
const epoch = sourceEpoch;
const stamp = (value: string | null | undefined) => {
  if (!value) return "Нет измерения";
  const match = value.match(
    /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/,
  );
  return match
    ? `${match[3]}.${match[2]}.${match[1]}, ${match[4]}:${match[5]}`
    : value.replace("T", " ").slice(0, 16);
};
const freshness = (s: string | undefined) =>
  s === "fresh" ? "Актуально" : s === "stale" ? "Устарело" : "Нет данных";
const format = (v: number | null | undefined, d = 2) =>
  v == null
    ? "—"
    : new Intl.NumberFormat("ru-RU", { maximumFractionDigits: d }).format(v);
const metricMap = (m: Metric[]) => new Map(m.map((x) => [x.id, x]));
const flagNames: Record<string, string> = {
  flatline: "Возможное зависание",
  suspect: "Подозрительное значение",
  invalid: "Некорректное значение",
  conflict: "Конфликт измерений",
};
function FlagLine({ flags = [] }: { flags?: string[] }) {
  return flags.length ? (
    <span className="flags">
      <AlertTriangle /> {flags.map((f) => flagNames[f] || f).join(", ")}
    </span>
  ) : null;
}
export function Overview({
  mode,
  metrics,
  snapshot,
  summary,
  series,
  pinned,
  onSelectTime,
  onNavigate,
  toolbar,
}: {
  mode: "period" | "moment";
  manifest: Manifest;
  metrics: Metric[];
  snapshot: Snapshot | null;
  summary: Summary | null;
  series: SeriesResponse | null;
  pinned: string[];
  onSelectTime?: (time: string) => void;
  onNavigate?: (target: AttentionTarget, metricId?: string) => void;
  toolbar?: ReactNode;
}) {
  const map = metricMap(metrics),
    sv = new Map(snapshot?.values.map((v) => [v.metric_id, v]));
  const assessment = buildOperatorAssessment({
    mode,
    snapshot,
    summary,
    metrics,
    pinned,
  });
  const primaryFinding = assessment.findings[0];
  const secondaryFindings = assessment.findings.slice(1, 3);
  const sulfurSeries =
    series?.series.filter((s) => s.metric_id.includes("Sulfur")) || [];
  const times = sulfurSeries.flatMap((s) =>
    s.points.map((p) => epoch(p.timestamp)),
  );
  const timeSpan = times.length ? Math.max(...times) - Math.min(...times) : 0;
  const crossesDay =
    times.length > 0 &&
    Math.floor(Math.min(...times) / 86400000) !==
      Math.floor(Math.max(...times) / 86400000);
  const option = {
    useUTC: true,
    tooltip: { trigger: "axis" },
    legend: { top: 0, data: ["ЛИМС · пробы", "ПАК · медиана"] },
    grid: { left: 50, right: 20, top: 60, bottom: 60 },
    dataZoom: [
      { type: "inside" },
      { type: "slider", height: 15, bottom: 3, showDetail: false },
    ],
    xAxis: {
      type: "time",
      axisLabel: {
        hideOverlap: true,
        formatter: (value: number) =>
          chartTimeLabel(value, timeSpan, crossesDay),
      },
      splitNumber: 4,
    },
    yAxis: { type: "value", name: "мг/кг" },
    series: sulfurSeries.flatMap((s) => {
      const lab = map.get(s.metric_id)?.source === "lims";
      return [
        {
          name: lab ? "ЛИМС · пробы" : "ПАК · медиана",
          type: "line",
          symbol: lab ? "diamond" : "circle",
          showSymbol: lab,
          symbolSize: lab ? 9 : 5,
          connectNulls: false,
          lineStyle: { width: lab ? 0 : 2 },
          itemStyle: { color: lab ? "#15805b" : "#0079c2" },
          data: s.points.map((p) => [epoch(p.timestamp), p.value]),
          markLine: {
            silent: true,
            symbol: "none",
            label: { formatter: "10 мг/кг", position: "insideEndTop" },
            lineStyle: { color: "#c53a3a" },
            data: [{ yAxis: 10 }],
          },
        },
        ...(!lab
          ? [
              {
                name: "ПАК · максимум",
                type: "line",
                symbol: "none",
                lineStyle: { width: 1, opacity: 0.4 },
                data: s.points.map((p) => [epoch(p.timestamp), p.max]),
              },
              {
                name: "ПАК · минимум",
                type: "line",
                symbol: "none",
                lineStyle: { width: 1, opacity: 0.4 },
                data: s.points.map((p) => [epoch(p.timestamp), p.min]),
              },
            ]
          : []),
      ];
    }),
  };
  const history = (
    <section className="panel chart-panel">
      <header>
        <h2>Содержание серы, мг/кг</h2>
      </header>
      {sulfurSeries.some((s) => s.points.length) ? (
        <Chart option={option} height={230} onSelectTime={onSelectTime} />
      ) : (
        <Empty text="Нет измерений серы в выбранном интервале" />
      )}
    </section>
  );
  return (
    <>
      <section
        className={`operator-summary ${assessment.level} ${secondaryFindings.length ? "" : "solo"}`}
        aria-labelledby="operator-status-title"
      >
        <div className="operator-status">
          <span className="operator-status-icon" aria-hidden="true">
            {assessment.level === "ok" ? (
              <ShieldCheck />
            ) : assessment.level === "unknown" ? (
              <CircleHelp />
            ) : (
              <AlertTriangle />
            )}
          </span>
          <div>
            <h2 id="operator-status-title">{assessment.title}</h2>
            <p>{assessment.description}</p>
            <div className="operator-context">
              <small>
                {assessment.source} ·{" "}
                {mode === "period"
                  ? `${stamp(summary?.from)} — ${stamp(summary?.to)}`
                  : stamp(snapshot?.at)}
              </small>
              <HelpTooltip label="Как интерпретировать вывод">
                Вывод относится к доступным историческим данным после
                гидроочистки. Он помогает оператору найти отклонение, но не
                является заключением о соответствии товарного топлива.
              </HelpTooltip>
            </div>
            {primaryFinding && (
              <button
                className="status-action"
                onClick={() =>
                  onNavigate?.(primaryFinding.target, primaryFinding.metricId)
                }
              >
                {primaryFinding.action} <ArrowRight />
              </button>
            )}
          </div>
        </div>
        {secondaryFindings.length > 0 && (
          <div className="attention-list">
            <div className="attention-heading">
              <h3>Также проверить</h3>
              <span>{secondaryFindings.length}</span>
            </div>
            <>
              {secondaryFindings.map((finding) => (
                <button
                  key={finding.code}
                  className={`attention-item ${finding.level}`}
                  onClick={() => onNavigate?.(finding.target, finding.metricId)}
                >
                  <span>
                    <b>{finding.title}</b>
                    {finding.description !== assessment.description && (
                      <small>{finding.description}</small>
                    )}
                  </span>
                  <span className="attention-action">
                    {finding.action} <ArrowRight />
                  </span>
                </button>
              ))}
            </>
          </div>
        )}
      </section>
      {toolbar}
      {mode === "period" ? (
        <>
          {history}
          <div className="coverage-strip">
            <span>
              Покрытие ПАК{" "}
              <HelpTooltip label="Покрытие ПАК">
                Доля выбранного периода, для которой есть пригодные измерения
                ПАК. Пропуски и подозрительные интервалы не засчитываются.
              </HelpTooltip>
            </span>
            <div aria-hidden="true">
              <i
                style={{
                  width: `${Math.max(0, Math.min(100, (summary?.sulfur.pak_coverage_fraction ?? 0) * 100))}%`,
                }}
              />
            </div>
            <b>
              {format((summary?.sulfur.pak_coverage_fraction ?? 0) * 100, 1)}%
            </b>
            <small>
              {summary?.sulfur.lab_count ?? 0} проб ЛИМС ·{" "}
              {summary?.sulfur.lab_exceed_count ?? 0} выше порога
            </small>
          </div>
        </>
      ) : (
        <>
          <section className="moment-trust panel chart-panel">
            <h2>Содержание серы, мг/кг</h2>
            {snapshot && <SulfurAtMoment snapshot={snapshot} />}
            <div className="moment-sources">
              {["lims.ht.2.Mg.Sulfur", "pak.ht.Mg.Sulfur"].map((id) => {
                const v = sv.get(id);
                return (
                  <div key={id}>
                    <h3>
                      {id.startsWith("lims")
                        ? "ЛИМС · контрольная проба"
                        : "ПАК · оперативная оценка"}
                      <HelpTooltip
                        label={id.startsWith("lims") ? "ЛИМС" : "ПАК"}
                      >
                        {id.startsWith("lims")
                          ? "ЛИМС — независимый лабораторный результат. Пробы появляются реже оперативных измерений."
                          : "ПАК — архив оперативного анализатора. Его значение оценивается вместе со свежестью и диагностическими флагами."}
                      </HelpTooltip>
                    </h3>
                    <p>
                      {stamp(v?.timestamp)} · {freshness(v?.freshness)}
                      {v?.age_minutes != null
                        ? ` · ${format(v.age_minutes, 0)} мин`
                        : ""}
                    </p>
                    <FlagLine flags={v?.flags} />
                  </div>
                );
              })}
            </div>
          </section>
          <details className="history-context">
            <summary>Предшествующие 24 часа</summary>
            {history}
          </details>
        </>
      )}
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
                  )}{" "}
                  {m.unit || "единица не указана"}
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
export function Trends({
  mode = "period",
  metrics,
  series,
  onSelectTime,
}: {
  mode?: "period" | "moment";
  metrics: Metric[];
  series: SeriesResponse | null;
  onSelectTime?: (time: string) => void;
}) {
  const map = metricMap(metrics);
  const allTimes =
    series?.series.flatMap((s) => s.points.map((p) => epoch(p.timestamp))) ||
    [];
  const low = allTimes.length ? Math.min(...allTimes) : undefined,
    high = allTimes.length ? Math.max(...allTimes) : undefined;
  const groups = Array.from(
    (series?.series ?? []).reduce((result, item) => {
      const metric = map.get(item.metric_id);
      const key = metric?.unit || "Единица не подтверждена";
      const values = result.get(key) ?? [];
      values.push(item);
      result.set(key, values);
      return result;
    }, new Map<string, NonNullable<SeriesResponse["series"]>>()),
  );
  const palette = [
    "#0079c2",
    "#16805b",
    "#9b650b",
    "#7559a6",
    "#c1484b",
    "#4b7189",
  ];
  return (
    <section className="panel">
      <header>
        <div>
          <h2>Тренды показателей</h2>
          <p>
            {mode === "period"
              ? "Динамика за выбранный период"
              : "Контекст за 24 часа до выбранного момента"}
          </p>
        </div>
      </header>
      {groups.length ? (
        groups.map(([unit, groupedSeries]) => {
          const sulfur = groupedSeries.some((item) =>
            item.metric_id.includes("Mg.Sulfur"),
          );
          const option = {
            tooltip: { trigger: "axis" },
            legend: { top: 0, type: "scroll" },
            grid: { left: 62, right: 24, top: 48, bottom: 70 },
            dataZoom: [
              { type: "inside" },
              { type: "slider", height: 18, bottom: 3, showDetail: false },
            ],
            useUTC: true,
            xAxis: {
              type: "time",
              min: low,
              max: high,
              splitNumber: 4,
              axisLabel: {
                hideOverlap: true,
                formatter: (value: number) =>
                  chartTimeLabel(
                    value,
                    (high || 0) - (low || 0),
                    Math.floor((low || 0) / 86400000) !==
                      Math.floor((high || 0) / 86400000),
                  ),
              },
            },
            yAxis: { type: "value", scale: true, name: unit },
            series: groupedSeries.map((item, index) => {
              const metric = map.get(item.metric_id);
              const lab = metric?.source === "lims";
              const source = metric?.source.toUpperCase() || "";
              return {
                name: `${metric?.label || item.metric_id} · ${source}`,
                type: "line",
                symbol: lab ? "diamond" : "circle",
                showSymbol: lab,
                symbolSize: lab ? 9 : 5,
                connectNulls: false,
                data: item.points.map((p) => [
                  Date.parse(p.timestamp + "Z"),
                  p.value,
                ]),
                lineStyle: { width: lab ? 0 : 2 },
                itemStyle: { color: palette[index % palette.length] },
                ...(sulfur
                  ? {
                      markLine: {
                        silent: true,
                        symbol: "none",
                        label: { formatter: "Порог 10 мг/кг" },
                        lineStyle: { color: "#bc3737" },
                        data: [{ yAxis: 10 }],
                      },
                    }
                  : {}),
              };
            }),
          };
          return (
            <div key={unit} className="trend-group">
              <h3>
                {sulfur ? "Содержание серы" : `Показатели · ${unit}`}{" "}
                <small>{groupedSeries.length} сигналов</small>
              </h3>
              <Chart
                option={option}
                height={280}
                group="monitoring-trends"
                onSelectTime={onSelectTime}
              />
            </div>
          );
        })
      ) : (
        <Empty text="Выберите показатели с данными" />
      )}
    </section>
  );
}
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
  const map = metricMap(metrics);
  const [query, setQuery] = useState("");
  const selectedStat = summary?.metrics.find((s) => s.metric_id === selected);
  const tableRows =
    summary?.metrics
      .filter((s) =>
        `${s.metric_id} ${map.get(s.metric_id)?.label || ""}`
          .toLowerCase()
          .includes(query.toLowerCase()),
      )
      .sort(
        (a, b) =>
          Number(b.metric_id === selected) - Number(a.metric_id === selected),
      ) || [];
  const bins = distribution?.bins || [];
  const option = {
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
        itemStyle: { color: "#0786d8", borderRadius: [3, 3, 0, 0] },
      },
    ],
  };
  return (
    <>
      <section className="panel">
        <header>
          <div>
            <h2>Статистика периода</h2>
            <p>Интервал расчёта [начало, конец)</p>
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
          {map.get(selected)?.label}{" "}
          <small>{map.get(selected)?.unit || "единица не подтверждена"}</small>
        </h3>
        {distribution &&
        distribution.count > 1 &&
        distribution.quartiles[0] !== distribution.quartiles[4] ? (
          <div className="distribution-grid">
            <div>
              <h3>Распределение · n={distribution?.count || 0}</h3>
              {bins.length ? (
                <Chart option={option} height={240} />
              ) : (
                <Empty text="Нет измерений в периоде" />
              )}
            </div>
            <div>
              <h3>
                Квартили ·{" "}
                {map.get(selected)?.unit || "единица не подтверждена"}
              </h3>
              {distribution?.count ? (
                <Chart
                  height={240}
                  option={{
                    grid: { left: 45, right: 20, top: 25, bottom: 35 },
                    tooltip: { trigger: "item" },
                    xAxis: { type: "category", data: ["Распределение"] },
                    yAxis: { type: "value", scale: true },
                    series: [
                      {
                        type: "boxplot",
                        data: [distribution.quartiles],
                        itemStyle: { color: "#dceef9", borderColor: "#0079c2" },
                      },
                    ],
                  }}
                />
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
  const vals = new Map(snapshot?.values.map((v) => [v.metric_id, v])),
    stats = new Map(summary?.metrics.map((v) => [v.metric_id, v]));
  const selectedSet = new Set(selected);
  const rows = metrics
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
                    {format(mode === "period" ? s?.median : v?.value)}{" "}
                    {m.unit || "единица не подтверждена"}
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
export function Formulas({ formulas }: { formulas: Formula[] }) {
  return (
    <section className="panel">
      <h2>
        Расчёты ВАК{" "}
        <HelpTooltip label="ВАК">
          Диагностические расчёты по значениям КИП. Они помогают анализу, но не
          заменяют лабораторную оценку.
        </HelpTooltip>
      </h2>
      <p className="lead">
        Диагностические формулы. Результаты не заменяют лабораторную оценку.
      </p>
      <div className="formula-list">
        {formulas.map((f) => (
          <details key={f.id}>
            <summary>
              <span>
                <b>{f.label}</b>
                <small>
                  {f.expression} · версия {f.version}
                </small>
              </span>
              <span className={`status ${f.status}`} title={f.status}>
                {{
                  experimental: "Экспериментальный",
                  invalid: "Некорректная формула",
                  unresolved: "Нет зависимостей",
                  verified: "Проверен",
                }[f.status] || f.status}
              </span>
              <ChevronDown />
            </summary>
            <div>
              <p>
                <strong>Подстановка:</strong> {f.substitution || "недоступна"}
              </p>
              <p>
                <strong>Результат:</strong> {format(f.result)} {f.unit || ""}
              </p>
              <p>
                <strong>Основание статуса:</strong> {f.reason || "не указано"}
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Вход</th>
                    <th>Значение</th>
                    <th>Время</th>
                    <th>Флаги</th>
                  </tr>
                </thead>
                <tbody>
                  {f.inputs.map((i) => (
                    <tr key={i.tag}>
                      <td>{i.tag}</td>
                      <td>{format(i.value)}</td>
                      <td>{i.timestamp?.replace("T", " ") || "—"}</td>
                      <td>{i.flags.join(", ") || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        ))}
      </div>
    </section>
  );
}
export function DataQuality({
  quality,
  datasetId,
  metrics = [],
  formulas = [],
  mode = "period",
  onOpenMoment,
}: {
  quality: Quality | null;
  datasetId?: string;
  metrics?: Metric[];
  formulas?: Formula[];
  mode?: "period" | "moment";
  onOpenMoment?: () => void;
}) {
  const metricCount = quality?.metrics.length ?? 0;
  const coveredCount =
    quality?.metrics.filter((metric) => metric.count > 0).length ?? 0;
  const invalidCount =
    quality?.metrics.reduce((sum, metric) => sum + metric.invalid_count, 0) ??
    0;
  const suspectCount =
    quality?.metrics.reduce((sum, metric) => sum + metric.suspect_count, 0) ??
    0;
  const signalsToCheck =
    quality?.metrics.filter(
      (metric) => metric.invalid_count > 0 || metric.suspect_count > 0,
    ).length ?? 0;
  const issueGroups = Array.from(
    (quality?.issues ?? [])
      .reduce(
        (groups, issue) => {
          const group = groups.get(issue.code) ?? {
            code: issue.code,
            message: issue.message,
            count: 0,
            items: [] as Issue[],
          };
          group.count += issue.count;
          group.items.push(issue);
          groups.set(issue.code, group);
          return groups;
        },
        new Map<
          string,
          {
            code: string;
            message: string;
            count: number;
            items: Issue[];
          }
        >(),
      )
      .values(),
  ).sort((a, b) => b.count - a.count);
  const issueNames: Record<string, string> = {
    flatline: "Возможные зависания сигналов",
    duplicate: "Повторяющиеся записи",
    conflict: "Конфликтующие значения",
    suspect: "Значения вне контрольного диапазона",
    invalid: "Некорректные значения",
    invalid_timestamp: "Некорректные временные метки",
  };
  return (
    <>
      <section className="panel diagnostic-overview">
        <header>
          <h2>Диагностика набора данных</h2>
        </header>
        <div className="diagnostic-stats">
          <article>
            <small>Пригодность данных</small>
            <strong>
              {coveredCount} <i>из {metricCount}</i>
            </strong>
            <span>Метрик содержат наблюдения</span>
          </article>
          <article className={invalidCount ? "danger" : "ok"}>
            <small>Некорректных</small>
            <strong>{format(invalidCount, 0)}</strong>
            <span>Не участвуют в статистике</span>
          </article>
          <article className={signalsToCheck ? "warning" : "ok"}>
            <small>
              Сигналов требуют проверки{" "}
              <HelpTooltip label="Сигналы требуют проверки">
                Есть некорректные или подозрительные отметки. Подозрение не
                доказывает неисправность прибора.
              </HelpTooltip>
            </small>
            <strong>{format(signalsToCheck, 0)}</strong>
            <span>{format(suspectCount, 0)} подозрительных отметок</span>
          </article>
        </div>
      </section>

      <details className="panel diagnostic-section">
        <summary>
          <span>
            <b>Выявленные проблемы</b>
            <small>
              {issueGroups.length
                ? `${issueGroups.length} групп · весь набор`
                : "Зарегистрированных проблем нет"}
            </small>
          </span>
          <ChevronDown />
        </summary>
        {issueGroups.length ? (
          <div className="issue-groups">
            {issueGroups.map((group) => (
              <details key={group.code}>
                <summary>
                  <AlertTriangle />
                  <span>
                    <b>{issueNames[group.code] || group.message}</b>
                    <small>{group.message}</small>
                  </span>
                  <strong>{format(group.count, 0)}</strong>
                  <ChevronDown />
                </summary>
                <ul>
                  {group.items.map((issue, index) => (
                    <li key={`${issue.metric_id || issue.code}-${index}`}>
                      <span>{issue.metric_id || "Источник целиком"}</span>
                      <b>{format(issue.count, 0)}</b>
                    </li>
                  ))}
                </ul>
              </details>
            ))}
          </div>
        ) : (
          <p className="empty-line">
            <ShieldCheck /> Зарегистрированных проблем нет. Это не означает
            прохождение всех возможных проверок.
          </p>
        )}
        <details className="assumptions-detail">
          <summary>Методические допущения</summary>
          {quality?.assumptions.map((assumption, index) => (
            <div className="assumption" key={index}>
              <Info /> {assumption}
            </div>
          ))}
        </details>
      </details>

      <details className="panel diagnostic-section">
        <summary>
          <span>
            <b>Паспорт набора</b>
            <small>Файлы, объём, фреймы и состав источников</small>
          </span>
          <ChevronDown />
        </summary>
        <div className="quality-grid">
          {quality?.sources.map((source) => (
            <article key={`${source.kind}-${source.filename}`}>
              <Database />
              <h3>{source.kind.toUpperCase()}</h3>
              <p>{source.filename}</p>
              <dl>
                <div>
                  <dt>Наблюдений</dt>
                  <dd>{format(source.rows, 0)}</dd>
                </div>
                <div>
                  <dt>Фреймов</dt>
                  <dd>
                    {format(
                      typeof source.frame_count === "number"
                        ? source.frame_count
                        : null,
                      0,
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Метрик</dt>
                  <dd>{source.metrics}</dd>
                </div>
                <div>
                  <dt>Некорректных</dt>
                  <dd>{source.invalid_count}</dd>
                </div>
                <div>
                  <dt>Подозрительных</dt>
                  <dd>{source.suspect_count}</dd>
                </div>
              </dl>
            </article>
          ))}
        </div>
      </details>

      <details className="panel diagnostic-section">
        <summary>
          <span>
            <b>Экспертный анализ сигналов</b>
            <small>Рейтинг и полная таблица качества</small>
          </span>
          <ChevronDown />
        </summary>
        {quality ? (
          <QualityRanking quality={quality} metrics={metrics} />
        ) : null}
        <details className="quality-detail">
          <summary>Покрытие и качество по всем показателям</summary>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Показатель</th>
                  <th>Измерений</th>
                  <th>Некорректных</th>
                  <th>Подозрительных</th>
                  <th>Зависших</th>
                  <th>Начало / конец</th>
                </tr>
              </thead>
              <tbody>
                {quality?.metrics.map((m) => (
                  <tr key={m.metric_id}>
                    <td>{m.metric_id}</td>
                    <td>{format(m.count, 0)}</td>
                    <td>{format(m.invalid_count, 0)}</td>
                    <td>{format(m.suspect_count, 0)}</td>
                    <td>{format(m.flatline_count, 0)}</td>
                    <td>
                      {stamp(m.start)}
                      <small>{stamp(m.end)}</small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      </details>

      <details className="panel diagnostic-section">
        <summary>
          <span>
            <b>Расчёты ВАК</b>
            <small>
              {mode === "moment"
                ? "Диагностические формулы для выбранного момента"
                : "Доступны для конкретного момента"}
            </small>
          </span>
          <ChevronDown />
        </summary>
        {mode === "moment" ? (
          <Formulas formulas={formulas} />
        ) : (
          <div className="diagnostic-prompt">
            <p>Выберите конкретный момент, чтобы подставить значения КИП.</p>
            <button className="secondary" onClick={onOpenMoment}>
              Открыть момент
            </button>
          </div>
        )}
      </details>

      {datasetId ? (
        <details className="panel diagnostic-section">
          <summary>
            <span>
              <b>Настройки диагностики</b>
              <small>Экспериментальные пороги свежести</small>
            </span>
            <ChevronDown />
          </summary>
          <FreshnessSettings datasetId={datasetId} />
        </details>
      ) : null}
    </>
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
function Empty({ text }: { text: string }) {
  return (
    <div className="empty">
      <Database />
      <p>{text}</p>
    </div>
  );
}
function FreshnessSettings({ datasetId }: { datasetId: string }) {
  const [values, setValues] = useState({ kip: 10, pak: 30, lims: 2880 }),
    [message, setMessage] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    api
      .settings(datasetId, controller.signal)
      .then((s) => setValues(s.freshness_minutes))
      .catch(() => {
        if (!controller.signal.aborted)
          setMessage("Не удалось загрузить настройки");
      });
    return () => controller.abort();
  }, [datasetId]);
  async function save() {
    setMessage("");
    try {
      await api.saveSettings(datasetId, { freshness_minutes: values });
      setMessage("Настройки сохранены");
    } catch {
      setMessage("Не удалось сохранить настройки");
    }
  }
  return (
    <section className="panel">
      <h2>Порог свежести</h2>
      <p className="lead">
        Экспериментальные пороги давности, не производственный регламент.
      </p>
      <div className="filters">
        {(["kip", "pak", "lims"] as const).map((source) => (
          <label className="field" key={source}>
            <span>{source.toUpperCase()}, минут</span>
            <input
              type="number"
              min="1"
              value={values[source]}
              onChange={(e) =>
                setValues((current) => ({
                  ...current,
                  [source]: Math.max(1, Number(e.target.value) || 1),
                }))
              }
            />
          </label>
        ))}
      </div>
      <button className="primary" onClick={save}>
        Сохранить
      </button>
      {message ? <p>{message}</p> : null}
    </section>
  );
}
