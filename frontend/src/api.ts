import type {
  Distribution,
  Formula,
  Manifest,
  Metric,
  Quality,
  SeriesResponse,
  Settings,
  Snapshot,
  Summary,
} from "./types";
const enc = encodeURIComponent;
async function request<T>(
  path: string,
  signal?: AbortSignal,
  init?: RequestInit,
): Promise<T> {
  const r = await fetch(path, { ...init, signal });
  if (!r.ok) {
    const body = await r.json().catch(() => null);
    const detail = body?.detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((item: { msg?: string }) => item.msg).join("; ")
          : `Ошибка запроса: HTTP ${r.status}`,
    );
  }
  return r.json();
}
const base = (id: string) => `/api/datasets/${enc(id)}`;
const interval = (from: string, to: string, exclude = false) =>
  `from=${enc(from)}&to=${enc(to)}&exclude_suspect=${exclude}`;
export interface Distillation {
  timestamp: string | null;
  age_minutes: number | null;
  points: { fraction: number; temperature: number; metric_id: string }[];
  reason?: string;
}
export const api = {
  datasets: (s?: AbortSignal) =>
    request<{ datasets: Manifest[]; default_id: string | null }>(
      "/api/datasets",
      s,
    ),
  dataset: (id: string, s?: AbortSignal) => request<Manifest>(base(id), s),
  metrics: (id: string, s?: AbortSignal) =>
    request<{ metrics: Metric[] }>(`${base(id)}/metrics`, s),
  snapshot: (id: string, at: string, s?: AbortSignal) =>
    request<Snapshot>(`${base(id)}/snapshot?at=${enc(at)}`, s),
  summary: (
    id: string,
    from: string,
    to: string,
    s?: AbortSignal,
    exclude = false,
  ) =>
    request<Summary>(`${base(id)}/summary?${interval(from, to, exclude)}`, s),
  series: (
    id: string,
    ids: string[],
    from: string,
    to: string,
    s?: AbortSignal,
    exclude = false,
  ) =>
    ids.length
      ? request<SeriesResponse>(
          `${base(id)}/series?metrics=${enc(ids.join(","))}&${interval(from, to, exclude)}&limit=600`,
          s,
        )
      : Promise.resolve({ series: [] }),
  formulas: (id: string, at: string, s?: AbortSignal) =>
    request<{ formulas: Formula[] }>(`${base(id)}/formulas?at=${enc(at)}`, s),
  quality: (id: string, s?: AbortSignal) =>
    request<Quality>(`${base(id)}/quality`, s),
  settings: (id: string, s?: AbortSignal) =>
    request<Settings>(`${base(id)}/settings`, s),
  saveSettings: (id: string, value: Settings) =>
    request<Settings>(`${base(id)}/settings`, undefined, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(value),
    }),
  distribution: (
    id: string,
    metric: string,
    from: string,
    to: string,
    s?: AbortSignal,
    exclude = false,
  ) =>
    request<Distribution>(
      `${base(id)}/distribution?metric=${enc(metric)}&${interval(from, to, exclude)}`,
      s,
    ),
  distillation: (id: string, at: string, s?: AbortSignal) =>
    request<Distillation>(`${base(id)}/distillation?at=${enc(at)}`, s),
  upload: (data: FormData) =>
    request<Manifest>("/api/datasets", undefined, {
      method: "POST",
      body: data,
    }),
};
export const exportUrl = (
  id: string,
  ids: string[],
  from: string,
  to: string,
  exclude = false,
) =>
  `${base(id)}/export?metrics=${enc(ids.join(","))}&${interval(from, to, exclude)}`;
