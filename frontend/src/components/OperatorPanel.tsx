import { useDeferredValue, useEffect, useRef, useState } from "react";
import { AlertTriangle, Search, Settings2, X } from "lucide-react";
import type { OperatorAssessment, AttentionTarget } from "../operatorStatus";
import type { Metric, Snapshot, Stat, Summary } from "../types";
import { HelpTooltip } from "./HelpTooltip";
import { SignalSelector } from "./SignalSelector";
import { formatNumber } from "../visualization";

export type SidePanelMode = "metrics" | "filters" | "warnings" | "closed";

const freshness = (value: string | undefined) =>
  value === "fresh"
    ? "Актуально"
    : value === "stale"
      ? "Устарело"
      : "Нет данных";

export function OperatorPanel({
  open,
  onClose,
  metrics,
  pinned,
  setPinned,
  mode,
  snapshot,
  summary,
  statistic,
  setStatistic,
  exclude,
  setExclude,
  selected,
  setSelected,
  assessment,
  onNavigate,
}: {
  open: Exclude<SidePanelMode, "closed">;
  onClose: () => void;
  metrics: Metric[];
  pinned: string[];
  setPinned: (ids: string[]) => void;
  mode: "period" | "moment";
  snapshot: Snapshot | null;
  summary: Summary | null;
  statistic: string;
  setStatistic: (value: string) => void;
  exclude: boolean;
  setExclude: (value: boolean) => void;
  selected: string[];
  setSelected: (ids: string[]) => void;
  assessment: OperatorAssessment;
  onNavigate: (target: AttentionTarget, metricId?: string) => void;
}) {
  const panel = useRef<HTMLElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query.toLowerCase());
  const metricById = new Map(metrics.map((metric) => [metric.id, metric]));
  const values = new Map(
    snapshot?.values.map((value) => [value.metric_id, value]),
  );
  const stats = new Map(summary?.metrics.map((stat) => [stat.metric_id, stat]));
  const titles = {
    metrics: "Показатели",
    filters: "Фильтры анализа",
    warnings: "Все предупреждения",
  };

  useEffect(() => {
    if (!previousFocus.current) {
      previousFocus.current = document.activeElement as HTMLElement;
    }
    panel.current?.querySelector<HTMLElement>("button, input, select")?.focus();
  }, []);

  function close() {
    const target = previousFocus.current;
    onClose();
    window.setTimeout(() => target?.focus(), 0);
  }

  function trap(event: React.KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== "Tab") return;
    const nodes = panel.current?.querySelectorAll<HTMLElement>(
      "button:not(:disabled), input:not(:disabled), select:not(:disabled), summary, [href]",
    );
    if (!nodes?.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  const available = metrics.filter(
    (metric) =>
      metric.available !== false &&
      `${metric.label} ${metric.id}`.toLowerCase().includes(deferredQuery),
  );

  return (
    <div className="side-panel-backdrop" onPointerDown={close}>
      <aside
        ref={panel}
        className="side-panel"
        role="dialog"
        aria-modal="true"
        aria-label={titles[open]}
        onPointerDown={(event) => event.stopPropagation()}
        onKeyDown={trap}
      >
        <header>
          <div>
            <h2>{titles[open]}</h2>
            {open === "warnings" && (
              <p>{assessment.findings.length} событий по важности</p>
            )}
          </div>
          <button
            className="icon-btn panel-close"
            onClick={close}
            aria-label="Закрыть панель"
          >
            <X />
          </button>
        </header>

        {open === "metrics" && (
          <>
            <div className="side-metric-list">
              {pinned.slice(0, 6).map((id) => {
                const metric = metricById.get(id);
                const value = values.get(id);
                const stat = stats.get(id);
                const displayed =
                  mode === "period"
                    ? (stat?.[statistic as keyof Stat] as number | null)
                    : value?.value;
                return (
                  <article key={id}>
                    <div>
                      <b>{metric?.label || id}</b>
                      <small>
                        {metric?.source.toUpperCase() || "—"} ·{" "}
                        {metric?.unit || "единица не подтверждена"}
                      </small>
                    </div>
                    <strong>{formatNumber(displayed)}</strong>
                    <details>
                      <summary>Подробнее</summary>
                      {mode === "period" ? (
                        <p>
                          {stat?.count ?? 0} измерений · мин.{" "}
                          {formatNumber(stat?.min, 2)} · макс.{" "}
                          {formatNumber(stat?.max, 2)} · изменение медианы{" "}
                          {formatNumber(stat?.median_change, 2)}
                        </p>
                      ) : (
                        <p>
                          {value?.timestamp?.replace("T", " ") ||
                            "Нет измерения"}{" "}
                          · {freshness(value?.freshness)} ·{" "}
                          {formatNumber(value?.age_minutes, 0)} мин · изменение{" "}
                          {formatNumber(value?.delta, 2)}
                        </p>
                      )}
                      {!!(mode === "period"
                        ? stat?.suspect_count
                        : value?.flags.length) && (
                        <p className="warn">Измерение требует проверки</p>
                      )}
                    </details>
                  </article>
                );
              })}
            </div>
            <details className="panel-settings">
              <summary>
                <Settings2 /> Настроить набор
              </summary>
              <label className="search">
                <Search />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Название или тег"
                  aria-label="Поиск показателя"
                />
              </label>
              <div className="panel-check-list">
                {available.map((metric) => {
                  const checked = pinned.includes(metric.id);
                  return (
                    <label key={metric.id}>
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={!checked && pinned.length >= 6}
                        onChange={() =>
                          setPinned(
                            checked
                              ? pinned.filter((id) => id !== metric.id)
                              : [...pinned, metric.id],
                          )
                        }
                      />
                      <span>
                        {metric.label}
                        <small>{metric.id}</small>
                      </span>
                    </label>
                  );
                })}
              </div>
            </details>
          </>
        )}

        {open === "filters" && (
          <div className="panel-filter-list">
            {mode === "period" && (
              <label>
                <input
                  type="checkbox"
                  checked={exclude}
                  onChange={(event) => setExclude(event.target.checked)}
                />
                <span>Исключить подозрительные измерения</span>
                <HelpTooltip label="Подозрительные измерения">
                  Диагностическое правило пометило измерение для проверки. Это
                  не доказывает неисправность прибора.
                </HelpTooltip>
              </label>
            )}
            {mode === "period" && (
              <label className="stacked-field">
                <span>Показывать за период</span>
                <select
                  value={statistic}
                  onChange={(event) => setStatistic(event.target.value)}
                >
                  <option value="median">Медиана</option>
                  <option value="mean">Среднее</option>
                  <option value="min">Минимум</option>
                  <option value="max">Максимум</option>
                  <option value="p05">P05</option>
                  <option value="p95">P95</option>
                  <option value="std">Стандартное отклонение</option>
                </select>
              </label>
            )}
            <div className="panel-signal-picker">
              <SignalSelector
                metrics={metrics}
                selected={selected}
                onChange={setSelected}
              />
            </div>
          </div>
        )}

        {open === "warnings" && (
          <div className="panel-warning-list">
            {assessment.findings.map((finding) => (
              <button
                key={finding.code}
                className={finding.level}
                onClick={() => {
                  close();
                  onNavigate(finding.target, finding.metricId);
                }}
              >
                <AlertTriangle />
                <span>
                  <b>{finding.title}</b>
                  <small>{finding.description}</small>
                </span>
                <em>{finding.action}</em>
              </button>
            ))}
          </div>
        )}
      </aside>
    </div>
  );
}
