import { Chart } from "./Chart";
import type { Metric, Quality, Snapshot, Stat, Summary } from "../types";
import { formatNumber, sulfurComposition } from "../visualization";

export function SulfurCoverage({ summary }: { summary: Summary }) {
  const parts = sulfurComposition(summary);
  return (
    <div className="coverage-visual">
      <h3>Структура периода ПАК</h3>
      <Chart
        height={65}
        label="Доли периода ПАК: ниже порога, превышение, подозрительные данные и отсутствие наблюдения"
        option={{
          grid: { left: 0, right: 0, top: 8, bottom: 25 },
          tooltip: {
            trigger: "item",
            valueFormatter: (v: number) => `${formatNumber(v)}%`,
          },
          xAxis: {
            type: "value",
            min: 0,
            max: 100,
            axisLabel: { formatter: "{value}%", fontSize: 10 },
            splitLine: { show: false },
          },
          yAxis: { type: "category", data: ["ПАК"], show: false },
          series: parts.map((p) => ({
            name: p.label,
            type: "bar",
            stack: "period",
            barWidth: 16,
            data: [p.percent],
            itemStyle: { color: p.color },
          })),
        }}
      />
      <dl className="coverage-legend">
        {parts.map((p) => (
          <div key={p.key}>
            <dt>
              <i style={{ background: p.color }} />
              {p.label}
            </dt>
            <dd>
              {formatNumber(p.minutes / 60)} ч{" "}
              <small>{formatNumber(p.percent)}%</small>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export function SulfurAtMoment({ snapshot }: { snapshot: Snapshot }) {
  const values = ["lims.ht.2.Mg.Sulfur", "pak.ht.Mg.Sulfur"].map((id) =>
    snapshot.values.find((v) => v.metric_id === id),
  );
  if (!values.some((v) => v?.value != null)) return null;
  const maximum = Math.max(15, ...values.map((v) => (v?.value ?? 0) * 1.15));
  return (
    <Chart
      height={150}
      label="Сера ЛИМС и ПАК на момент относительно порога 10 мг/кг"
      option={{
        grid: { left: 55, right: 65, top: 25, bottom: 30 },
        tooltip: {
          trigger: "item",
          valueFormatter: (v: number) => `${formatNumber(v)} мг/кг`,
        },
        xAxis: {
          type: "value",
          min: 0,
          max: maximum,
          name: "мг/кг",
          nameLocation: "end",
          splitNumber: 4,
        },
        yAxis: {
          type: "category",
          inverse: true,
          data: ["ЛИМС", "ПАК"],
          axisTick: { show: false },
          axisLine: { show: false },
        },
        series: [
          {
            type: "bar",
            barWidth: 13,
            data: values.map((v) => ({
              value: v?.value ?? null,
              itemStyle: {
                color:
                  !v || v.freshness !== "fresh" || v.flags.length
                    ? "#748391"
                    : v.value! > 10
                      ? "#bf3d42"
                      : "#0079c2",
              },
            })),
            label: {
              show: true,
              position: "right",
              formatter: (p: { value: number | null }) => formatNumber(p.value),
            },
            markLine: {
              symbol: "none",
              silent: true,
              lineStyle: { color: "#bf3d42" },
              label: {
                formatter: "Порог 10",
                position: "insideEndTop",
                rotate: 0,
              },
              data: [{ xAxis: 10 }],
            },
          },
        ],
      }}
    />
  );
}

export function MedianComparison({
  stat,
  unit,
}: {
  stat: Stat | undefined;
  unit: string | null;
}) {
  if (!stat || stat.median == null)
    return <p className="muted">Нет измерений для сравнения.</p>;
  const previous = stat.previous_median;
  const available = previous != null;
  return (
    <div className="median-comparison">
      <h3>Медиана · предыдущий и выбранный период</h3>
      <div className="comparison-numbers">
        <span>
          <i className="previous-dot" />
          Предыдущий <b>{formatNumber(previous, 2)}</b>
        </span>
        <span>
          <i className="current-dot" />
          Выбранный <b>{formatNumber(stat.median, 2)}</b>
        </span>
        <span>
          Изменение{" "}
          <b>
            {stat.median_change != null && stat.median_change > 0 ? "+" : ""}
            {formatNumber(stat.median_change, 2)} {unit || ""}
          </b>
        </span>
      </div>
      {available ? (
        <Chart
          height={105}
          label="Сравнение медиан двух соседних периодов"
          option={{
            grid: { left: 35, right: 35, top: 20, bottom: 30 },
            tooltip: { trigger: "item" },
            xAxis: {
              type: "value",
              scale: true,
              name: unit || "",
              splitNumber: 4,
            },
            yAxis: { type: "value", min: -1, max: 1, show: false },
            series: [
              {
                type: "line",
                data: [
                  [previous, 0],
                  [stat.median, 0],
                ],
                symbol: "none",
                silent: true,
                lineStyle: { color: "#a3b0be", width: 2 },
              },
              {
                name: "Предыдущий",
                type: "scatter",
                data: [[previous, 0]],
                symbol: "emptyCircle",
                symbolSize: 13,
                itemStyle: { color: "#61748a" },
              },
              {
                name: "Выбранный",
                type: "scatter",
                data: [[stat.median, 0]],
                symbol: "diamond",
                symbolSize: 13,
                itemStyle: { color: "#0079c2" },
              },
            ],
          }}
        />
      ) : (
        <p className="muted">В предыдущем периоде нет измерений.</p>
      )}
    </div>
  );
}

export function QualityRanking({
  quality,
  metrics,
}: {
  quality: Quality;
  metrics: Metric[];
}) {
  const map = new Map(metrics.map((m) => [m.id, m]));
  const rows = quality.metrics
    .filter((m) => m.count > 0 && m.suspect_count > 0)
    .map((m) => ({ ...m, percentage: (m.suspect_count / m.count) * 100 }))
    .sort(
      (a, b) =>
        b.percentage - a.percentage || b.suspect_count - a.suspect_count,
    )
    .slice(0, 8);
  if (!rows.length) return null;
  return (
    <section className="panel quality-ranking">
      <header>
        <div>
          <h2>Сигналы с наибольшей долей подозрительных измерений</h2>
          <p>
            Весь набор · включая зависания · доля от наблюдений каждого сигнала
          </p>
        </div>
      </header>
      <Chart
        height={rows.length * 32 + 45}
        label="Восемь сигналов с наибольшей долей подозрительных измерений"
        option={{
          grid: { left: 95, right: 55, top: 10, bottom: 35 },
          tooltip: {
            trigger: "item",
            formatter: (p: { dataIndex: number }) => {
              const r = rows[p.dataIndex];
              return `${r.metric_id}: ${formatNumber(r.percentage)}% (${formatNumber(r.suspect_count, 0)} / ${formatNumber(r.count, 0)})`;
            },
          },
          xAxis: {
            type: "value",
            min: 0,
            max: 100,
            axisLabel: { formatter: "{value}%" },
            splitNumber: 4,
          },
          yAxis: {
            type: "category",
            inverse: true,
            data: rows.map((r) => r.metric_id),
            axisLabel: { fontSize: 11 },
            axisTick: { show: false },
          },
          series: [
            {
              type: "bar",
              barWidth: 12,
              data: rows.map((r) => r.percentage),
              itemStyle: { color: "#b77b19" },
              label: {
                show: true,
                position: "right",
                formatter: (p: { value: number }) =>
                  `${formatNumber(p.value)}%`,
              },
            },
          ],
        }}
      />
      <details>
        <summary>Показатели в диаграмме</summary>
        <ul className="rank-key">
          {rows.map((r) => (
            <li key={r.metric_id}>
              <b>{r.metric_id}</b>
              <span>{map.get(r.metric_id)?.label || r.metric_id}</span>
              <small>
                {formatNumber(r.suspect_count, 0)} из {formatNumber(r.count, 0)}
              </small>
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}
