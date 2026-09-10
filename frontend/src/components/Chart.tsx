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
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsCoreOption, ECharts } from "echarts/core";

use([
  LineChart,
  BarChart,
  BoxplotChart,
  ScatterChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  DataZoomComponent,
  MarkLineComponent,
  CanvasRenderer,
]);

interface ChartProps {
  option: EChartsCoreOption;
  height?: number;
  onTime?: (time: string) => void;
  onSelectTime?: (time: string) => void;
  group?: string;
  label?: string;
}

const configured = (value: EChartsCoreOption) => ({
  animation: false,
  textStyle: { fontFamily: "Inter, Arial, sans-serif", fontSize: 12 },
  color: ["#0079c2", "#168160", "#b77b19"],
  ...value,
  tooltip: { ...(value.tooltip as object), renderMode: "richText" },
});

export function Chart({
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
