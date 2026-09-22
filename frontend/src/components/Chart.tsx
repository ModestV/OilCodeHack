import { lazy, Suspense } from "react";
import type { EChartsCoreOption } from "echarts/core";

export interface ChartProps {
  option: EChartsCoreOption;
  height?: number;
  onTime?: (time: string) => void;
  onSelectTime?: (time: string) => void;
  group?: string;
  label?: string;
}

const ChartImpl = lazy(() =>
  import("./ChartImpl").then((module) => ({ default: module.ChartImpl })),
);

export function Chart(props: ChartProps) {
  const height = props.height ?? 300;
  return (
    <Suspense
      fallback={
        <div
          className="chart-skeleton"
          style={{ height }}
          role="status"
          aria-label="Подготовка графика"
        />
      }
    >
      <ChartImpl {...props} />
    </Suspense>
  );
}
