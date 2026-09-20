/* Keeps the operator's context (page, tab, mode, time range, signals, dataset)
   in the URL query, so a refresh, the back button or a shared link restore it. */

export interface UrlState {
  page?: string;
  tab?: string;
  mode?: string;
  from?: string;
  to?: string;
  dataset?: string;
  signals?: string[];
}

const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/;

export function readUrlState(search = window.location.search): UrlState {
  const params = new URLSearchParams(search);
  const time = (key: string) => {
    const value = params.get(key);
    return value && ISO.test(value)
      ? value.length === 16
        ? `${value}:00`
        : value
      : undefined;
  };
  const signals = params.get("signals");
  return {
    page: params.get("page") ?? undefined,
    tab: params.get("tab") ?? undefined,
    mode: params.get("mode") ?? undefined,
    from: time("from"),
    to: time("to"),
    dataset: params.get("dataset") ?? undefined,
    signals: signals ? signals.split(",").filter(Boolean) : undefined,
  };
}

export function writeUrlState(state: UrlState) {
  const params = new URLSearchParams();
  if (state.dataset) params.set("dataset", state.dataset);
  if (state.page && state.page !== "monitoring") params.set("page", state.page);
  if (state.tab && state.tab !== "overview") params.set("tab", state.tab);
  if (state.mode && state.mode !== "period") params.set("mode", state.mode);
  if (state.from && state.mode !== "moment")
    params.set("from", state.from.slice(0, 16));
  if (state.to) params.set("to", state.to.slice(0, 16));
  if (state.signals?.length) params.set("signals", state.signals.join(","));
  const query = params.toString();
  const next = `${window.location.pathname}${query ? `?${query}` : ""}`;
  if (next !== `${window.location.pathname}${window.location.search}`)
    window.history.replaceState(null, "", next);
}
