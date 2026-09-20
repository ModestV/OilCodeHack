import { lazy, Suspense } from "react";
import type { ChartProps } from "./ChartImpl";

/* ECharts is the largest dependency; it loads in its own chunk the first time a
   chart mounts, so the application shell and data requests are not blocked. */
const ChartImpl = lazy(() => import("./ChartImpl"));

export function Chart(props: ChartProps) {
  return (
    <Suspense
      fallback={
        <div
          className="chart chart-loading"
          style={{ height: props.height ?? 300 }}
          aria-busy="true"
        />
      }
    >
      <ChartImpl {...props} />
    </Suspense>
  );
}
