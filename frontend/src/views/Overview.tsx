import { useMemo, type ReactNode } from "react";
import {
  AlertTriangle,
  ArrowRight,
  CircleHelp,
  ShieldCheck,
} from "lucide-react";
import { Chart } from "../components/Chart";
import { SulfurAtMoment } from "../components/MonitoringCharts";
import { HelpTooltip } from "../components/HelpTooltip";
import { chartTimeLabel } from "../visualization";
import { useChartTheme } from "../ui/useChartTheme";
import {
  type AttentionTarget,
  type OperatorAssessment,
} from "../operatorStatus";
import type {
  Manifest,
  Metric,
  SeriesResponse,
  Snapshot,
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
import { Disclosure } from "../ui/Controls";
import { timeMenu } from "../ui/chartMenu";

export function Overview({
  mode,
  metrics,
  snapshot,
  summary,
  series,
  assessment,
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
  assessment: OperatorAssessment;
  onSelectTime?: (time: string) => void;
  onNavigate?: (target: AttentionTarget, metricId?: string) => void;
  toolbar?: ReactNode;
}) {
  const theme = useChartTheme();
  const map = useMemo(() => metricMap(metrics), [metrics]);
  const sv = useMemo(
    () => new Map(snapshot?.values.map((v) => [v.metric_id, v])),
    [snapshot],
  );
  const chartMenu = useMemo(() => timeMenu(onSelectTime), [onSelectTime]);
  const primaryFinding = assessment.findings[0];
  const secondaryFindings = assessment.findings.slice(1, 3);
  const sulfurSeries = useMemo(
    () => series?.series.filter((s) => s.metric_id.includes("Sulfur")) || [],
    [series],
  );
  const option = useMemo(() => {
    const times = sulfurSeries.flatMap((s) =>
      s.points.map((p) => epoch(p.timestamp)),
    );
    const timeSpan = times.length ? Math.max(...times) - Math.min(...times) : 0;
    const crossesDay =
      times.length > 0 &&
      Math.floor(Math.min(...times) / 86400000) !==
        Math.floor(Math.max(...times) / 86400000);
    return {
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
            itemStyle: { color: lab ? theme.ok : theme.accent },
            data: s.points.map((p) => [epoch(p.timestamp), p.value]),
            markLine: {
              silent: true,
              symbol: "none",
              label: { formatter: "10 мг/кг", position: "insideEndTop" },
              lineStyle: { color: theme.danger },
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
  }, [sulfurSeries, map, theme]);
  const history = (
    <section className="panel chart-panel">
      <header>
        <h2>Содержание серы, мг/кг</h2>
      </header>
      {sulfurSeries.some((s) => s.points.length) ? (
        <Chart
          option={option}
          height={230}
          onSelectTime={onSelectTime}
          contextMenu={chartMenu}
        />
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
          <Disclosure
            className="history-context"
            summary="Предшествующие 24 часа"
          >
            {history}
          </Disclosure>
        </>
      )}
    </>
  );
}
