import { useDeferredValue, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  ChevronDown,
  ChevronUp,
  CircleHelp,
  OctagonAlert,
  Settings2,
} from "lucide-react";
import type { OperatorAssessment, AttentionTarget } from "../operatorStatus";
import type { Metric, Snapshot, Stat, Summary } from "../types";
import { HelpTooltip } from "./HelpTooltip";
import { SignalPicker } from "./SignalPicker";
import { formatNumber } from "../visualization";
import { Dialog } from "../ui/Dialog";
import { Select } from "../ui/Select";
import { Checkbox, Disclosure, SearchField, Switch } from "../ui/Controls";
import { stamp } from "../views/shared";
import "./panels.css";

export type SidePanelMode = "metrics" | "filters" | "warnings" | "closed";
const MAX_PINNED = 6;

const freshness = (value: string | undefined) =>
  value === "fresh"
    ? "Актуально"
    : value === "stale"
      ? "Устарело"
      : "Нет данных";

const statistics = [
  { value: "median", label: "Медиана" },
  { value: "mean", label: "Среднее" },
  { value: "min", label: "Минимум" },
  { value: "max", label: "Максимум" },
  { value: "p05", label: "P05" },
  { value: "p95", label: "P95" },
  { value: "std", label: "Стандартное отклонение" },
];

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
  filterTarget,
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
  filterTarget: "trends" | "statistics";
  onNavigate: (target: AttentionTarget, metricId?: string) => void;
}) {
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query.toLowerCase());
  const [draft, setDraft] = useState(selected);
  useEffect(() => setDraft(selected), [selected, open]);
  const metricById = useMemo(
    () => new Map(metrics.map((metric) => [metric.id, metric])),
    [metrics],
  );
  const values = useMemo(
    () => new Map(snapshot?.values.map((value) => [value.metric_id, value])),
    [snapshot],
  );
  const stats = useMemo(
    () => new Map(summary?.metrics.map((stat) => [stat.metric_id, stat])),
    [summary],
  );
  const titles = {
    metrics: "Показатели",
    filters:
      filterTarget === "statistics"
        ? "Фильтры статистики"
        : "Сигналы и фильтры",
    warnings: "Все предупреждения",
  };
  const available = useMemo(() => {
    const pinnedOrder = new Map(pinned.map((id, index) => [id, index]));
    return metrics
      .filter(
        (metric) =>
          metric.available !== false &&
          `${metric.label} ${metric.id}`.toLowerCase().includes(deferredQuery),
      )
      .sort((a, b) => {
        const ao = pinnedOrder.get(a.id);
        const bo = pinnedOrder.get(b.id);
        if (ao !== undefined && bo !== undefined) return ao - bo;
        if (ao !== undefined) return -1;
        if (bo !== undefined) return 1;
        return a.label.localeCompare(b.label, "ru");
      });
  }, [metrics, pinned, deferredQuery]);

  const dirty =
    draft.length !== selected.length ||
    draft.some((id, i) => selected[i] !== id);

  return (
    <Dialog
      open
      onClose={onClose}
      variant="drawer"
      title={titles[open]}
      description={
        open === "warnings"
          ? `${assessment.findings.length} событий по важности`
          : undefined
      }
      className="operator-panel"
      footer={
        open === "filters" && filterTarget === "trends" ? (
          <>
            <button type="button" className="secondary" onClick={onClose}>
              Отмена
            </button>
            <button
              type="button"
              className="primary"
              disabled={!dirty}
              onClick={() => {
                setSelected(draft);
                onClose();
              }}
            >
              Применить
            </button>
          </>
        ) : undefined
      }
    >
      {open === "metrics" && (
        <>
          {mode === "period" && (
            <div className="metric-statistic">
              <span id="statistic-label">Значение за период</span>
              <Select
                label="Значение за период"
                value={statistic}
                onChange={setStatistic}
                options={statistics}
              />
            </div>
          )}
          <div className="side-metric-list">
            {pinned.slice(0, MAX_PINNED).map((id) => {
              const metric = metricById.get(id);
              const value = values.get(id);
              const stat = stats.get(id);
              const displayed =
                mode === "period"
                  ? (stat?.[statistic as keyof Stat] as number | null)
                  : value?.value;
              const flagged = !!(mode === "period"
                ? stat?.suspect_count
                : value?.flags.length);
              return (
                <article key={id}>
                  <div>
                    <b>{metric?.label || id}</b>
                    <small>
                      {[metric?.source.toUpperCase(), metric?.unit]
                        .filter(Boolean)
                        .join(" · ") || "Источник не указан"}
                    </small>
                  </div>
                  <strong className="num">{formatNumber(displayed)}</strong>
                  <Disclosure summary="Подробнее" className="inline">
                    {mode === "period" ? (
                      <p>
                        {stat?.count ?? 0} измерений · мин.{" "}
                        {formatNumber(stat?.min, 2)} · макс.{" "}
                        {formatNumber(stat?.max, 2)} · изменение медианы{" "}
                        {formatNumber(stat?.median_change, 2)}
                      </p>
                    ) : (
                      <p>
                        {stamp(value?.timestamp)} ·{" "}
                        {freshness(value?.freshness)} ·{" "}
                        {formatNumber(value?.age_minutes, 0)} мин · изменение{" "}
                        {formatNumber(value?.delta, 2)}
                      </p>
                    )}
                    {flagged && (
                      <p className="warn">Измерение требует проверки</p>
                    )}
                  </Disclosure>
                </article>
              );
            })}
          </div>
          <Disclosure
            summary="Настроить набор"
            icon={<Settings2 />}
            className="panel-settings"
          >
            <div className="panel-selection-status" role="status">
              <span>
                Выбрано <b>{pinned.length}</b> из {MAX_PINNED}
              </span>
              <small>
                {pinned.length >= MAX_PINNED
                  ? "Снимите один из выбранных показателей, чтобы добавить другой."
                  : "Отмеченные показатели отображаются в панели выше."}
              </small>
            </div>
            <SearchField
              label="Поиск показателя"
              placeholder="Название или тег"
              value={query}
              onChange={setQuery}
            />
            <div className="panel-check-list">
              {available.map((metric) => {
                const index = pinned.indexOf(metric.id);
                const checked = index >= 0;
                const move = (delta: number) => {
                  const next = [...pinned];
                  const target = index + delta;
                  if (target < 0 || target >= next.length) return;
                  [next[index], next[target]] = [next[target], next[index]];
                  setPinned(next);
                };
                return (
                  <div className="panel-check-row" key={metric.id}>
                    <Checkbox
                      checked={checked}
                      disabled={!checked && pinned.length >= MAX_PINNED}
                      onChange={() =>
                        setPinned(
                          checked
                            ? pinned.filter((id) => id !== metric.id)
                            : [...pinned, metric.id],
                        )
                      }
                      label={metric.label}
                      description={metric.id}
                    />
                    {checked && !deferredQuery && (
                      <span className="panel-check-order">
                        <button
                          type="button"
                          className="icon-button"
                          aria-label={`Поднять ${metric.label}`}
                          disabled={index === 0}
                          onClick={() => move(-1)}
                        >
                          <ChevronUp />
                        </button>
                        <button
                          type="button"
                          className="icon-button"
                          aria-label={`Опустить ${metric.label}`}
                          disabled={index === pinned.length - 1}
                          onClick={() => move(1)}
                        >
                          <ChevronDown />
                        </button>
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          </Disclosure>
        </>
      )}

      {open === "filters" && (
        <div className="panel-filter-list">
          {mode === "period" && (
            <div className="panel-switch-row">
              <Switch
                checked={exclude}
                onChange={(event) => setExclude(event.target.checked)}
                label="Исключить подозрительные измерения"
              />
              <HelpTooltip label="Подозрительные измерения">
                Диагностическое правило пометило измерение для проверки. Это не
                доказывает неисправность прибора.
              </HelpTooltip>
            </div>
          )}
          {filterTarget === "trends" && (
            <SignalPicker
              metrics={metrics}
              selected={draft}
              onChange={setDraft}
            />
          )}
        </div>
      )}

      {open === "warnings" && (
        <div className="panel-warning-list">
          {assessment.findings.map((finding) => (
            <button
              key={finding.code}
              type="button"
              className={finding.level}
              onClick={() => {
                onClose();
                onNavigate(finding.target, finding.metricId);
              }}
            >
              {finding.level === "danger" ? (
                <OctagonAlert aria-hidden="true" />
              ) : finding.level === "warning" ? (
                <AlertTriangle aria-hidden="true" />
              ) : (
                <CircleHelp aria-hidden="true" />
              )}
              <span>
                <b>{finding.title}</b>
                <small>{finding.description}</small>
              </span>
              <em>
                {finding.action} <ArrowRight aria-hidden="true" />
              </em>
            </button>
          ))}
        </div>
      )}
    </Dialog>
  );
}
