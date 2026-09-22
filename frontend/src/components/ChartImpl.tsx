import { useEffect, useRef } from "react";
import { connect, init, use } from "echarts/core";
import {
  BarChart,
  BoxplotChart,
  LineChart,
  ScatterChart,
} from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TitleComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { ECharts, EChartsCoreOption } from "echarts/core";
import type { ChartProps } from "./Chart";
import { composeChartOption } from "../visualization";

use([
  LineChart,
  BarChart,
  BoxplotChart,
  ScatterChart,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  DataZoomComponent,
  MarkLineComponent,
  CanvasRenderer,
]);

const configured = (value: EChartsCoreOption) => composeChartOption(value);

export function ChartImpl({
  option,
  height = 300,
  onTime,
  onSelectTime,
  group,
  label = "График данных",
}: ChartProps) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ECharts>();
  const latest = useRef({ option, onTime, onSelectTime });
  latest.current = { option, onTime, onSelectTime };

  useEffect(() => {
    if (!ref.current) return;
    let chart: ECharts | undefined;
    const render = () => {
      if (!ref.current?.clientWidth) return;
      if (chart) {
        chart.resize();
        return;
      }
      chart = init(ref.current);
      chartRef.current = chart;
      if (group) {
        chart.group = group;
        connect(group);
      }
      chart.setOption(configured(latest.current.option));
      chart.on("click", (event: { value?: unknown }) => {
        const select = latest.current.onSelectTime || latest.current.onTime;
        if (!select) return;
        if (Array.isArray(event.value) && event.value[0] != null)
          select(String(event.value[0]));
      });
    };
    render();
    const observer = new ResizeObserver(render);
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      chart?.dispose();
      chartRef.current = undefined;
    };
  }, [group]);

  useEffect(() => {
    chartRef.current?.setOption(configured(option), {
      replaceMerge: ["series"],
    });
  }, [option]);

  return (
    <div
      ref={ref}
      style={{ height, minWidth: 0 }}
      role="img"
      aria-label={label}
    />
  );
}
