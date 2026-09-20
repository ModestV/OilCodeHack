import { useMemo, useState } from "react";
import { Chart } from "../components/Chart";
import { MedianComparison } from "../components/MonitoringCharts";
import { HelpTooltip } from "../components/HelpTooltip";
import { useChartTheme } from "../ui/useChartTheme";
import type { Distribution, Metric, Snapshot, Stat, Summary } from "../types";
import { Empty, FlagLine, format, freshness, metricMap, stamp } from "./shared";
import { Select } from "../ui/Select";
import { DataTable, type Column } from "../ui/DataTable";
import { Disclosure, SearchField } from "../ui/Controls";

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
  const metricOptions = useMemo(
    () =>
      metrics.map((m) => ({
        value: m.id,
        label: m.label,
        description: `${m.source.toUpperCase()} · ${m.id}`,
      })),
    [metrics],
  );
  const statColumns = useMemo<Column<Stat>[]>(
    () => [
      {
        key: "metric",
        header: "Показатель",
        fixed: true,
        role: "primary",
        cell: (s) => (
          <>
            {map.get(s.metric_id)?.label || s.metric_id}
            <small>{map.get(s.metric_id)?.unit || ""}</small>
          </>
        ),
      },
      { key: "n", header: "n", align: "right", cell: (s) => s.count },
      {
        key: "median",
        header: "Медиана",
        align: "right",
        cell: (s) => format(s.median),
      },
      {
        key: "range",
        header: "Мин. – макс.",
        align: "right",
        cell: (s) => `${format(s.min)} – ${format(s.max)}`,
      },
      {
        key: "p",
        header: (
          <>
            P05–P95{" "}
            <HelpTooltip label="P05–P95">
              Диапазон, внутри которого находится 90% измерений: от 5-го до
              95-го процентиля.
            </HelpTooltip>
          </>
        ),
        title: "P05–P95",
        align: "right",
        cell: (s) => `${format(s.p05)}–${format(s.p95)}`,
      },
      {
        key: "change",
        header: "Изменение медианы",
        align: "right",
        cell: (s) => (
          <>
            {format(s.median_change)}
            <small>от первого: {format(s.change)}</small>
          </>
        ),
      },
      {
        key: "quality",
        header: "Ошиб. / подозр.",
        title: "Ошибочных / подозрительных",
        align: "right",
        cell: (s) => `${s.invalid_count} / ${s.suspect_count}`,
      },
      {
        key: "mean",
        header: "Среднее",
        align: "right",
        optional: true,
        role: "detail",
        cell: (s) => format(s.mean),
      },
      {
        key: "previous",
        header: "Пред. медиана",
        title: "Предыдущая медиана",
        align: "right",
        optional: true,
        role: "detail",
        cell: (s) => format(s.previous_median),
      },
      {
        key: "std",
        header: (
          <>
            σ{" "}
            <HelpTooltip label="σ">
              Стандартное отклонение: разброс относительно среднего.
            </HelpTooltip>
          </>
        ),
        title: "σ",
        align: "right",
        optional: true,
        role: "detail",
        cell: (s) => format(s.std),
      },
      {
        key: "iqr",
        header: "IQR",
        align: "right",
        optional: true,
        role: "detail",
        cell: (s) => format(s.iqr),
      },
      {
        key: "spread",
        header: "Размах",
        align: "right",
        optional: true,
        role: "detail",
        cell: (s) => format(s.range),
      },
      {
        key: "firstlast",
        header: "Первое / последнее",
        optional: true,
        role: "detail",
        cell: (s) => (
          <>
            {format(s.first)} / {format(s.last)}
            <small>
              {stamp(s.first_at)} — {stamp(s.last_at)}
            </small>
          </>
        ),
      },
      {
        key: "extrema",
        header: "Время min / max",
        optional: true,
        role: "detail",
        cell: (s) => (
          <>
            {stamp(s.min_at)}
            <small>{stamp(s.max_at)}</small>
          </>
        ),
      },
    ],
    [map],
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
          <Select
            label="Показатель распределения"
            value={selected}
            onChange={setSelected}
            searchable
            className="statistics-metric"
            options={metricOptions}
          />
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
          <SearchField
            label="Поиск в статистике"
            placeholder="Показатель или тег"
            value={query}
            onChange={setQuery}
          />
          <small>Показателей: {tableRows.length}</small>
        </div>
        <DataTable
          className="statistics-table"
          label="Статистика по показателям"
          rows={tableRows}
          columns={statColumns}
          rowKey={(s) => s.metric_id}
          rowClassName={(s) =>
            s.metric_id === selected ? "selected-row" : undefined
          }
          storageKey="statistics"
          stickyFirst
          maxHeight={560}
          minWidth={900}
          emptyText="Нет показателей по запросу"
        />
        <Disclosure
          className="agreement analysis-detail"
          summary="Согласованность ЛИМС и ПАК"
          meta="Смещение — систематическая разница ПАК и ЛИМС; средняя абсолютная ошибка — типичный размер расхождения без учёта направления"
        >
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
        </Disclosure>
        <Disclosure
          className="analysis-detail"
          summary="Лабораторный паспорт за период"
        >
          <Passport
            metrics={metrics}
            snapshot={null}
            mode="period"
            summary={summary}
          />
        </Disclosure>
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
