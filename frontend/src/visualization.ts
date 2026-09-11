import type { Summary } from "./types";

export const sourceEpoch = (value: string) =>
  Date.parse(value.replace(/Z$/, "") + "Z");

export function formatNumber(
  value: number | null | undefined,
  maximumFractionDigits = 1,
): string {
  return value == null
    ? "—"
    : new Intl.NumberFormat("ru-RU", {
        maximumFractionDigits,
      }).format(value);
}

export function formatChartTimestamp(epoch: number): string {
  const date = new Date(epoch).toISOString();
  return `${date.slice(8, 10)}.${date.slice(5, 7)}.${date.slice(0, 4)}, ${date.slice(11, 16)}`;
}

export function chartTimeLabel(
  value: number,
  spanMs: number,
  crossesDay = false,
): string {
  const date = new Date(value).toISOString();
  if (spanMs < 86400000 && !crossesDay) return date.slice(11, 16);
  const day = `${date.slice(8, 10)}.${date.slice(5, 7)}`;
  return spanMs > 366 * 86400000
    ? `${day}.${date.slice(0, 4)}`
    : `${day}\n${date.slice(11, 16)}`;
}

export function sulfurComposition(summary: Summary) {
  const total = Math.max(
    0,
    (sourceEpoch(summary.to) - sourceEpoch(summary.from)) / 60000,
  );
  const trusted = Math.min(
    total,
    Math.max(0, summary.sulfur.pak_observed_minutes),
  );
  const above = Math.min(
    trusted,
    Math.max(0, summary.sulfur.pak_exceed_minutes),
  );
  const suspect = Math.min(
    total - trusted,
    Math.max(0, summary.sulfur.pak_suspect_minutes),
  );
  return [
    {
      key: "below",
      label: "Не выше 10 мг/кг",
      minutes: trusted - above,
      color: "#0079c2",
    },
    { key: "above", label: "Выше 10 мг/кг", minutes: above, color: "#bf3d42" },
    {
      key: "suspect",
      label: "Подозрительный сигнал",
      minutes: suspect,
      color: "#b77b19",
    },
    {
      key: "missing",
      label: "Нет достоверного наблюдения",
      minutes: Math.max(0, total - trusted - suspect),
      color: "#748391",
    },
  ].map((item) => ({
    ...item,
    percent: total > 0 ? (item.minutes / total) * 100 : 0,
  }));
}

type ChartPoint =
  | number
  | string
  | null
  | undefined
  | (number | null)[]
  | { value?: number | null | (number | null)[] };

type ChartSeries = {
  name?: string;
  type?: string;
  data?: ChartPoint[];
  tooltip?: { show?: boolean };
  silent?: boolean;
  lineStyle?: Record<string, unknown>;
  emphasis?: Record<string, unknown>;
  [key: string]: unknown;
};

type ChartOptionLike = {
  title?: Record<string, unknown>;
  tooltip?: Record<string, unknown>;
  legend?: Record<string, unknown>;
  grid?: Record<string, unknown>;
  dataZoom?: unknown;
  xAxis?: Record<string, unknown>;
  yAxis?: Record<string, unknown>;
  series?: ChartSeries | ChartSeries[];
  [key: string]: unknown;
};

const isNumeric = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

const pointValue = (point: ChartPoint) =>
  point &&
  typeof point === "object" &&
  !Array.isArray(point) &&
  "value" in point
    ? point.value
    : point;

const pointX = (point: ChartPoint) => {
  const value = pointValue(point);
  return Array.isArray(value) ? value[0] : null;
};

const pointY = (point: ChartPoint) => {
  const value = pointValue(point);
  return Array.isArray(value) ? value[1] : value;
};

const asSeriesArray = (series: ChartOptionLike["series"]) =>
  Array.isArray(series) ? series : series ? [series] : [];

const hasNonZeroRange = (
  minSeries: ChartSeries | undefined,
  maxSeries: ChartSeries | undefined,
) => {
  const maxByTime = new Map<unknown, number>();
  for (const point of maxSeries?.data ?? []) {
    const y = pointY(point);
    if (isNumeric(y)) maxByTime.set(pointX(point), y);
  }
  for (const point of minSeries?.data ?? []) {
    const y = pointY(point);
    const max = maxByTime.get(pointX(point));
    if (isNumeric(y) && isNumeric(max) && Math.abs(max - y) > 0.000001) {
      return true;
    }
  }
  return false;
};

const isOverviewSulfurOption = (option: ChartOptionLike) => {
  const series = asSeriesArray(option.series);
  const names = new Set(series.map((item) => item.name));
  return names.has("ЛИМС · пробы") || names.has("ПАК · медиана");
};

const defaultTooltipFormatter = (
  params: unknown,
  maximumFractionDigits = 1,
  unit = "",
) => {
  const items = Array.isArray(params) ? params : [params];
  const rows = items
    .map((item) => {
      if (!item || typeof item !== "object") return null;
      const data = "data" in item ? (item as { data?: ChartPoint }).data : null;
      const value =
        data != null
          ? pointY(data)
          : "value" in item
            ? pointY((item as { value?: ChartPoint }).value)
            : null;
      if (!isNumeric(value)) return null;
      const name =
        "seriesName" in item
          ? (item as { seriesName?: string }).seriesName
          : "";
      if (name === "ПАК · минимум" || name === "ПАК · максимум") return null;
      return `${name}: ${formatNumber(value, maximumFractionDigits)}${unit}`;
    })
    .filter((row): row is string => row != null);
  const first = items[0];
  const axis =
    first && typeof first === "object" && "axisValue" in first
      ? (first as { axisValue?: unknown }).axisValue
      : null;
  const header = isNumeric(axis) ? [formatChartTimestamp(axis)] : [];
  return [...header, ...rows].join("\n");
};

export function composeChartOption<T extends ChartOptionLike>(value: T): T {
  const overviewSulfur = isOverviewSulfurOption(value);
  const tooltip: Record<string, unknown> = {
    valueFormatter: (v: number | null | undefined) => formatNumber(v),
    ...(value.tooltip || {}),
    renderMode: "richText",
  };
  if (
    !("formatter" in tooltip) &&
    (tooltip.trigger === "axis" || overviewSulfur)
  ) {
    tooltip.formatter = overviewSulfur
      ? (params: unknown) => defaultTooltipFormatter(params, 1, " мг/кг")
      : defaultTooltipFormatter;
  }
  let option: ChartOptionLike = {
    animation: false,
    textStyle: { fontFamily: "Inter, Arial, sans-serif", fontSize: 12 },
    color: ["#0079c2", "#168160", "#b77b19"],
    ...value,
    tooltip,
  };

  if (overviewSulfur) {
    const series = asSeriesArray(option.series);
    const minSeries = series.find((item) => item.name === "ПАК · минимум");
    const maxSeries = series.find((item) => item.name === "ПАК · максимум");
    const drawRange = hasNonZeroRange(minSeries, maxSeries);
    const legendData = [
      series.some((item) => item.name === "ЛИМС · пробы") ? "ЛИМС" : null,
      series.some((item) => item.name === "ПАК · медиана") ? "ПАК" : null,
    ].filter((name): name is string => name != null);
    option = {
      ...option,
      legend: { ...(option.legend || {}), top: 0, data: legendData },
      grid: { left: 44, right: 16, top: 36, bottom: 34 },
      dataZoom: Array.isArray(option.dataZoom)
        ? option.dataZoom.filter(
            (zoom) =>
              !(
                zoom &&
                typeof zoom === "object" &&
                "type" in zoom &&
                (zoom as { type?: unknown }).type === "slider"
              ),
          )
        : option.dataZoom,
      xAxis: {
        ...(option.xAxis || {}),
        axisLine: { lineStyle: { color: "#d8e0e8" } },
        axisTick: { show: false },
        splitLine: { show: false },
      },
      yAxis: {
        ...(option.yAxis || {}),
        name: "",
        splitLine: { lineStyle: { color: "#edf2f7" } },
        axisLine: { show: false },
        axisTick: { show: false },
      },
      series: series
        .filter((item) =>
          drawRange
            ? true
            : item.name !== "ПАК · минимум" && item.name !== "ПАК · максимум",
        )
        .map((item) => {
          if (item.name === "ЛИМС · пробы") return { ...item, name: "ЛИМС" };
          if (item.name === "ПАК · медиана") return { ...item, name: "ПАК" };
          if (item.name === "ПАК · минимум" || item.name === "ПАК · максимум") {
            return {
              ...item,
              tooltip: { show: false },
              silent: true,
              lineStyle: { ...(item.lineStyle || {}), width: 1, opacity: 0.25 },
              emphasis: { disabled: true },
            };
          }
          return item;
        }),
    };
  }

  return option as T;
}
