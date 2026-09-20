import { useSyncExternalStore } from "react";
import { chartTheme, onThemeChange } from "../chartTheme";

/** Re-renders the caller when the colour theme changes so chart options rebuild. */
export function useChartTheme() {
  return useSyncExternalStore(onThemeChange, chartTheme, chartTheme);
}
