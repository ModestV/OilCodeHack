import { useEffect, useState } from "react";
import {
  AlertTriangle,
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
  SulfurCoverage,
  MedianComparison,
  QualityRanking,
} from "../components/MonitoringCharts";
import { SignalSelector } from "../components/SignalSelector";
import { chartTimeLabel, sourceEpoch } from "../visualization";
import { api, exportUrl } from "../api";
import type {
  Distribution,
  Formula,
  Manifest,
  Metric,
  Quality,
  SeriesResponse,
  Snapshot,
  Stat,
  Summary,
} from "../types";
const epoch = sourceEpoch;
const stamp = (s: string | null | undefined) =>
  s ? s.replace("T", " ").slice(0, 19) : "Нет измерения";
const freshness = (s: string | undefined) =>
  s === "fresh" ? "Актуально" : s === "stale" ? "Устарело" : "Нет данных";
const statLabels: Record<string, string> = {
  median: "Медиана",
  mean: "Среднее",
  min: "Минимум",
  max: "Максимум",
  p05: "P05",
  p95: "P95",
  std: "Стандартное отклонение",
};
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
  cardStatistic = "median",
}: {
  mode: "period" | "moment";
  manifest: Manifest;
  metrics: Metric[];
  snapshot: Snapshot | null;
  summary: Summary | null;
  series: SeriesResponse | null;
  pinned: string[];
  onSelectTime?: (time: string) => void;
  cardStatistic?: string;
}) {
  const map = metricMap(metrics),
    sm = new Map(summary?.metrics.map((s) => [s.metric_id, s])),
    sv = new Map(snapshot?.values.map((v) => [v.metric_id, v]));
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
        <div>
          <h2>Содержание серы</h2>
          <p>ЛИМС · контрольные пробы / ПАК · архив измерений</p>
        </div>
      </header>
      {sulfurSeries.some((s) => s.points.length) ? (
        <Chart option={option} onSelectTime={onSelectTime} />
      ) : (
        <Empty text="Нет измерений серы в выбранном интервале" />
      )}
    </section>
  );
  return (
    <>
      <div className="cards">
        {pinned.map((id) => {
          const m = map.get(id),
            stat = sm.get(id),
            val = sv.get(id);
          const pakId =
            id === "lims.ht.2.Mg.Sulfur"
              ? "pak.ht.Mg.Sulfur"
              : id === "lims.ht.2.D15"
                ? "pak.ht.D15"
                : "";
          const pak = sv.get(pakId),
            pakStat = sm.get(pakId);
          const displayed =
            mode === "period"
              ? (stat?.[cardStatistic as keyof Stat] as number | null)
              : val?.value;
          return (
            <article className="metric-card" key={id}>
              <div>
                <strong>{m?.label || id}</strong>
                <small>
                  {m?.source.toUpperCase() || "НЕТ В НАБОРЕ"} ·{" "}
                  {m?.unit || "единица не подтверждена"}
                </small>
              </div>
              <b>{format(displayed)}</b>
              {mode === "period" ? (
                <>
                  <p>
                    {statLabels[cardStatistic]} · n={stat?.count ?? 0}
                  </p>
                  <p>
                    Мин. {format(stat?.min)} · макс. {format(stat?.max)}
                  </p>
                  <p
                    title={`Предыдущий период: ${stamp(summary?.comparison_from)} — ${stamp(summary?.comparison_to)}`}
                  >
                    Δ медианы: {format(stat?.median_change)}
                  </p>
                  {!!stat?.suspect_count && (
                    <p className="warn">Подозрительных: {stat.suspect_count}</p>
                  )}
                </>
              ) : (
                <>
                  <p>
                    {val?.timestamp
                      ? stamp(val.timestamp)
                      : "Нет измерения к моменту"}
                  </p>
                  <p className={val?.freshness === "fresh" ? "" : "warn"}>
                    {freshness(val?.freshness)}
                    {val?.age_minutes != null
                      ? " · " + format(val.age_minutes, 0) + " мин"
                      : ""}
                  </p>
                  <p>Δ измерения: {format(val?.delta)}</p>
                  <FlagLine flags={val?.flags} />
                </>
              )}
              {pakId && (
                <div className="companion">
                  <p>
                    ПАК · {mode === "period" ? "медиана" : "последний"}:{" "}
                    {format(mode === "period" ? pakStat?.median : pak?.value)}
                  </p>
                  <small>
                    {mode === "period"
                      ? "n=" + (pakStat?.count || 0)
                      : stamp(pak?.timestamp) +
                        " · " +
                        freshness(pak?.freshness)}
                  </small>
                </div>
              )}
              {m?.mapping_warning && (
                <p className="warn">{m.mapping_warning}</p>
              )}
              {val?.reason && <p className="muted">{val.reason}</p>}
            </article>
          );
        })}
      </div>
      {mode === "period" ? (
        <div className="overview-grid">
          {history}
          <section className="panel trust">
            <h2>Сера · контроль периода</h2>
            {summary && <SulfurCoverage summary={summary} />}
            <dl>
              <div>
                <dt>Проб ЛИМС</dt>
                <dd>{summary?.sulfur.lab_count ?? 0}</dd>
              </div>
              <div>
                <dt>Выше 10 мг/кг</dt>
                <dd className={summary?.sulfur.lab_exceed_count ? "warn" : ""}>
                  {summary?.sulfur.lab_exceed_count ?? 0}
                </dd>
              </div>
              <div>
                <dt>Покрытие ПАК</dt>
                <dd>
                  {format(
                    (summary?.sulfur.pak_coverage_fraction ?? 0) * 100,
                    1,
                  )}
                  %
                </dd>
              </div>
            </dl>
            <p className="muted">
              Покрытие и часы превышения рассчитаны по достоверным измерениям
              ПАК.
            </p>
          </section>
        </div>
      ) : (
        <>
          <section className="moment-trust panel">
            <h2>Сера · состояние на момент</h2>
            {snapshot && <SulfurAtMoment snapshot={snapshot} />}
            <div className="moment-sources">
              {["lims.ht.2.Mg.Sulfur", "pak.ht.Mg.Sulfur"].map((id) => {
                const v = sv.get(id),
                  reliable =
                    v?.value != null &&
                    v.freshness === "fresh" &&
                    !v.flags.length;
                return (
                  <div key={id}>
                    <h3>
                      {id.startsWith("lims")
                        ? "ЛИМС · контрольная проба"
                        : "ПАК · оперативная оценка"}
                    </h3>
                    <strong
                      className={
                        reliable ? (v.value! > 10 ? "warn" : "ok") : "muted"
                      }
                    >
                      {reliable
                        ? v.value! > 10
                          ? "Выше порога 10 мг/кг"
                          : "Не выше порога 10 мг/кг"
                        : "Недостаточно достоверных данных"}
                    </strong>
                    <p>
                      {stamp(v?.timestamp)} · {freshness(v?.freshness)} ·{" "}
                      {format(v?.age_minutes, 0)} мин
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
      <p className="domain-note">
        <Info /> Порог серы: 10 мг/кг. Продукт после гидроочистки, без
        заключения о соответствии товарного топлива.
      </p>
      {mode === "period" && (
        <details className="agreement">
          <summary>Согласованность ЛИМС и ПАК</summary>
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
                {summary?.agreement.map((a) => (
                  <tr key={a.metric_id}>
                    <td>{map.get(a.metric_id)?.label || a.metric_id}</td>
                    <td>{a.n}</td>
                    <td>{format(a.bias)}</td>
                    <td>{format(a.mae)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
      <Passport
        metrics={metrics}
        snapshot={snapshot}
        mode={mode}
        summary={summary}
      />
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
  metrics,
  series,
  selected,
  setSelected,
  onSelectTime,
}: {
  metrics: Metric[];
  series: SeriesResponse | null;
  selected: string[];
  setSelected: (v: string[]) => void;
  onSelectTime?: (time: string) => void;
}) {
  const map = metricMap(metrics);
  const allTimes =
    series?.series.flatMap((s) => s.points.map((p) => epoch(p.timestamp))) ||
    [];
  const low = allTimes.length ? Math.min(...allTimes) : undefined,
    high = allTimes.length ? Math.max(...allTimes) : undefined;
  return (
    <section className="panel">
      <header>
        <div>
          <h2>Тренды показателей</h2>
          <p>Динамика за выбранный период</p>
        </div>
        <SignalSelector
          metrics={metrics}
          selected={selected}
          onChange={setSelected}
        />
      </header>
      {series?.series.length ? (
        series.series.map((s) => {
          const metric = map.get(s.metric_id);
          const option = {
            tooltip: { trigger: "axis" },
            grid: { left: 62, right: 24, top: 24, bottom: 70 },
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
            yAxis: { type: "value", scale: true, name: metric?.unit || "" },
            series: [
              {
                name: metric?.label || s.metric_id,
                type: "line",
                symbol: metric?.source === "lims" ? "diamond" : "circle",
                showSymbol: metric?.source === "lims",
                symbolSize: 8,
                connectNulls: false,
                data: s.points.map((p) => [
                  Date.parse(p.timestamp + "Z"),
                  p.value,
                ]),
                lineStyle: { width: metric?.source === "lims" ? 0 : 2 },
              },
              {
                name: "Минимум",
                type: "line",
                symbol: "none",
                data:
                  metric?.source === "lims"
                    ? []
                    : s.points.map((p) => [
                        Date.parse(p.timestamp + "Z"),
                        p.min,
                      ]),
                lineStyle: { width: 1, opacity: 0.3 },
              },
              {
                name: "Максимум",
                type: "line",
                symbol: "none",
                data:
                  metric?.source === "lims"
                    ? []
                    : s.points.map((p) => [
                        Date.parse(p.timestamp + "Z"),
                        p.max,
                      ]),
                lineStyle: { width: 1, opacity: 0.3 },
              },
            ],
          };
          return (
            <div key={s.metric_id}>
              <h3>
                {metric?.label || s.metric_id}{" "}
                <small>{metric?.unit || "единица не указана"}</small>
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
                <th>P05–P95</th>
                <th>Пред. медиана</th>
                <th>Изменение</th>
                <th>σ / IQR / размах</th>
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
}: {
  metrics: Metric[];
  snapshot: Snapshot | null;
  mode?: "period" | "moment";
  summary?: Summary | null;
  onTrend?: (id: string) => void;
}) {
  const [q, setQ] = useState(""),
    [plant, setPlant] = useState<"all" | "avt" | "ht">("all");
  const vals = new Map(snapshot?.values.map((v) => [v.metric_id, v])),
    stats = new Map(summary?.metrics.map((v) => [v.metric_id, v]));
  const rows = metrics.filter(
    (m) =>
      m.source === "kip" &&
      (plant === "all" || m.plant === plant) &&
      `${m.label} ${m.id} ${m.group} ${m.description || ""}`
        .toLowerCase()
        .includes(q.toLowerCase()),
  );
  return (
    <section className="panel">
      <header>
        <div>
          <h2>Технология и КИП</h2>
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
      <h2>Расчёты ВАК</h2>
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
}: {
  quality: Quality | null;
  datasetId?: string;
  metrics?: Metric[];
}) {
  return (
    <>
      <section className="quality-grid">
        {quality?.sources.map((s) => (
          <article className="panel" key={`${s.kind}-${s.filename}`}>
            <Database />
            <h3>{s.kind.toUpperCase()}</h3>
            <p>{s.filename}</p>
            <dl>
              <div>
                <dt>Наблюдений</dt>
                <dd>{format(s.rows, 0)}</dd>
              </div>
              <div>
                <dt>Фреймов</dt>
                <dd>
                  {format(
                    typeof s.frame_count === "number" ? s.frame_count : null,
                    0,
                  )}
                </dd>
              </div>
              <div>
                <dt>Метрик</dt>
                <dd>{s.metrics}</dd>
              </div>
              <div>
                <dt>Некорректных</dt>
                <dd>{s.invalid_count}</dd>
              </div>
              <div>
                <dt>Подозрительных</dt>
                <dd>{s.suspect_count}</dd>
              </div>
            </dl>
          </article>
        ))}
      </section>
      {quality && <QualityRanking quality={quality} metrics={metrics} />}
      <section className="panel">
        <h2>Проблемы и допущения · весь набор</h2>
        {quality?.issues.length ? (
          quality.issues.map((i, n) => (
            <div className="issue" key={`${i.code}-${n}`}>
              <AlertTriangle />
              <span>
                <b>{i.message}</b>
                <small>
                  {i.code}
                  {i.metric_id ? ` · ${i.metric_id}` : ""}
                </small>
              </span>
              <strong>{i.count}</strong>
            </div>
          ))
        ) : (
          <p className="empty-line">
            <ShieldCheck /> Зарегистрированных проблем нет. Это не означает, что
            данные прошли пороговую проверку.
          </p>
        )}
        {quality?.assumptions.map((a, i) => (
          <div className="assumption" key={i}>
            <Info /> {a}
          </div>
        ))}
      </section>
      {datasetId ? <FreshnessSettings datasetId={datasetId} /> : null}
      <details className="quality-detail">
        <summary>Покрытие и качество по показателям · весь набор</summary>
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
