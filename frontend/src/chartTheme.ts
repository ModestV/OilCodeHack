/* Chart colours come from the CSS tokens so ECharts follows the active theme.
   Works without a DOM (tests) by falling back to the light palette. */

export interface ChartTheme {
  accent: string;
  ok: string;
  warn: string;
  danger: string;
  text: string;
  text2: string;
  text3: string;
  grid: string;
  axis: string;
  muted: string;
  band: string;
  surface: string;
  series: string[];
  font: string;
}

const fallback: ChartTheme = {
  accent: "#35618f",
  ok: "#2b7654",
  warn: "#8f6210",
  danger: "#b1403a",
  text: "#1c2128",
  text2: "#49515b",
  text3: "#626b76",
  grid: "#e8ebef",
  axis: "#a9b1bb",
  muted: "#8a939e",
  band: "#d8e2ee",
  surface: "#ffffff",
  series: ["#35618f", "#3a8a68", "#a97f22", "#75689f", "#b5544c", "#5d7a90"],
  font: 'Inter, "Segoe UI", Roboto, Arial, sans-serif',
};

let cache: ChartTheme | null = null;
const listeners = new Set<() => void>();
let watching = false;

function invalidate() {
  cache = null;
  listeners.forEach((fn) => fn());
}

function watch() {
  if (watching || typeof document === "undefined") return;
  watching = true;
  new MutationObserver(invalidate).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener(
    "change",
    invalidate,
  );
}

export function chartTheme(): ChartTheme {
  if (typeof document === "undefined") return fallback;
  if (cache) return cache;
  watch();
  const styles = getComputedStyle(document.documentElement);
  const read = (name: string, value: string) =>
    styles.getPropertyValue(name).trim() || value;
  cache = {
    accent: read("--accent", fallback.accent),
    ok: read("--ok", fallback.ok),
    warn: read("--warn", fallback.warn),
    danger: read("--danger", fallback.danger),
    text: read("--text", fallback.text),
    text2: read("--text-2", fallback.text2),
    text3: read("--text-3", fallback.text3),
    grid: read("--chart-grid", fallback.grid),
    axis: read("--chart-axis", fallback.axis),
    muted: read("--chart-muted", fallback.muted),
    band: read("--chart-band", fallback.band),
    surface: read("--surface", fallback.surface),
    series: [1, 2, 3, 4, 5, 6].map((i) =>
      read(`--series-${i}`, fallback.series[i - 1]),
    ),
    font: read("--font", fallback.font),
  };
  return cache;
}

/** Subscribe to theme changes; returns an unsubscribe function. */
export function onThemeChange(listener: () => void) {
  watch();
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
