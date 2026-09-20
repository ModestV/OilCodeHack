import { memo, useEffect, useRef } from "react";
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
import type { EChartsCoreOption, ECharts } from "echarts/core";
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

export interface ChartProps {
  option: EChartsCoreOption;
  height?: number;
  onSelectTime?: (time: string) => void;
  group?: string;
  label?: string;
}

const configured = (value: EChartsCoreOption) => composeChartOption(value);

function ChartComponent({
  option,
  height = 300,
  onSelectTime,
  group,
  label = "График данных",
}: ChartProps) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ECharts>();
  const latest = useRef({ option, onSelectTime });
  latest.current = { option, onSelectTime };
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
        const select = latest.current.onSelectTime;
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
      className="chart"
      style={{ height, minWidth: 0 }}
      role="img"
      aria-label={label}
    />
  );
}

export const Chart = memo(ChartComponent);
export default Chart;
