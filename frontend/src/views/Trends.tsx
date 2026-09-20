import { useMemo } from "react";
import { Chart } from "../components/Chart";
import { chartTimeLabel } from "../visualization";
import { useChartTheme } from "../ui/useChartTheme";
import { timeMenu } from "../ui/chartMenu";
import { useViewport } from "../ui/hooks";
import type { Metric, SeriesResponse } from "../types";
import { Empty, epoch, metricMap } from "./shared";

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
  const theme = useChartTheme();
  const map = useMemo(() => metricMap(metrics), [metrics]);
  const chartMenu = useMemo(() => timeMenu(onSelectTime), [onSelectTime]);
  const phone = useViewport() === "phone";
  // Options are memoised per unit group so re-renders don't rebuild every chart.
  const groups = useMemo(() => {
    const allTimes =
      series?.series.flatMap((s) => s.points.map((p) => epoch(p.timestamp))) ||
      [];
    const low = allTimes.length ? Math.min(...allTimes) : undefined,
      high = allTimes.length ? Math.max(...allTimes) : undefined;
    const palette = theme.series;
    const grouped = Array.from(
      (series?.series ?? []).reduce((result, item) => {
        const metric = map.get(item.metric_id);
        const key = metric?.unit
          ? `unit:${metric.unit}`
          : `metric:${item.metric_id}`;
        const values = result.get(key) ?? [];
        values.push(item);
        result.set(key, values);
        return result;
      }, new Map<string, NonNullable<SeriesResponse["series"]>>()),
    );
    return grouped.map(([groupKey, groupedSeries]) => {
      const firstMetric = map.get(groupedSeries[0].metric_id);
      const unit = firstMetric?.unit || "";
      const sulfur = groupedSeries.some((item) =>
        item.metric_id.includes("Mg.Sulfur"),
      );
      const option = {
        tooltip: { trigger: "axis" },
        legend: { top: 0, type: "scroll" },
        grid: { left: 52, right: 16, top: 48, bottom: phone ? 36 : 70 },
        dataZoom: phone
          ? [{ type: "inside" }]
          : [
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
        yAxis: {
          type: "value",
          scale: true,
          name: unit || "Значение",
        },
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
                    lineStyle: { color: theme.danger },
                    data: [{ yAxis: 10 }],
                  },
                }
              : {}),
          };
        }),
      };
      const title = sulfur
        ? "Содержание серы"
        : unit
          ? `Показатели · ${unit}`
          : firstMetric?.label || groupedSeries[0].metric_id;
      return { groupKey, option, title, count: groupedSeries.length };
    });
  }, [series, map, theme, phone]);
  return (
    <section className="panel">
      <header>
        <div>
          <h2>Тренды показателей</h2>
          <p>
            {mode === "period"
              ? "Динамика за выбранный период"
              : "За 24 часа до выбранного момента"}
          </p>
        </div>
      </header>
      {groups.length ? (
        groups.map(({ groupKey, option, title, count }) => (
          <div key={groupKey} className="trend-group">
            <h3>
              {title} {count > 1 && <small>Сигналов: {count}</small>}
            </h3>
            <Chart
              option={option}
              height={phone ? 240 : 280}
              group="monitoring-trends"
              onSelectTime={onSelectTime}
              contextMenu={chartMenu}
            />
          </div>
        ))
      ) : (
        <Empty text="Выберите показатели с данными" />
      )}
    </section>
  );
}
