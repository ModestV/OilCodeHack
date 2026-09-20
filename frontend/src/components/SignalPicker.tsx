import { useMemo, useState } from "react";
import { BarChart3, X } from "lucide-react";
import type { Metric } from "../types";
import { Checkbox, SearchField } from "../ui/Controls";
import { Select } from "../ui/Select";
import { Tooltip } from "../ui/Tooltip";
import "./panels.css";

/**
 * Inline multi-select of signals: search, source/plant filters, chips of the
 * current choice and a checkbox list. Fully controlled — the parent decides
 * when the choice is applied.
 */
export function SignalPicker({
  metrics,
  selected,
  onChange,
  max = 20,
}: {
  metrics: Metric[];
  selected: string[];
  onChange: (ids: string[]) => void;
  max?: number;
}) {
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [plant, setPlant] = useState("");
  const metricById = useMemo(
    () => new Map(metrics.map((metric) => [metric.id, metric])),
    [metrics],
  );
  const selectedMetrics = selected
    .map((id) => metricById.get(id))
    .filter((metric): metric is Metric => Boolean(metric));
  const selectedIds = new Set(selected);
  const sources = useMemo(
    () => Array.from(new Set(metrics.map((m) => m.source))).sort(),
    [metrics],
  );
  const plants = useMemo(
    () => Array.from(new Set(metrics.map((m) => m.plant))).sort(),
    [metrics],
  );
  const rows = useMemo(() => {
    const q = query.trim().toLocaleLowerCase();
    const visible = metrics.filter((metric) => {
      const haystack =
        `${metric.label} ${metric.id} ${metric.unit ?? ""}`.toLocaleLowerCase();
      return (
        (!q || haystack.includes(q)) &&
        (!source || metric.source === source) &&
        (!plant || metric.plant === plant)
      );
    });
    const order = new Map(selected.map((id, index) => [id, index]));
    return visible.slice().sort((a, b) => {
      const ao = order.get(a.id);
      const bo = order.get(b.id);
      if (ao !== undefined && bo !== undefined) return ao - bo;
      if (ao !== undefined) return -1;
      if (bo !== undefined) return 1;
      return 0;
    });
  }, [metrics, plant, query, selected, source]);

  const remove = (id: string) => onChange(selected.filter((x) => x !== id));
  const toggle = (metric: Metric) => {
    if (selectedIds.has(metric.id)) return remove(metric.id);
    if (metric.available === false || selected.length >= max) return;
    onChange([...selected, metric.id]);
  };

  return (
    <div className="signal-picker">
      <SearchField
        label="Поиск сигнала"
        placeholder="Название, тег или единица"
        value={query}
        onChange={setQuery}
      />
      <div className="signal-picker-filters">
        <Select
          label="Источник"
          size="sm"
          block
          value={source}
          onChange={setSource}
          options={[
            { value: "", label: "Все источники" },
            ...sources.map((item) => ({
              value: item,
              label: item.toUpperCase(),
            })),
          ]}
        />
        <Select
          label="Установка"
          size="sm"
          block
          value={plant}
          onChange={setPlant}
          options={[
            { value: "", label: "Все установки" },
            ...plants.map((item) => ({
              value: item,
              label: item.toUpperCase(),
            })),
          ]}
        />
      </div>
      {selectedMetrics.length > 0 && (
        <div className="signal-chips" aria-label="Выбранные показатели">
          {selectedMetrics.map((metric) => (
            <span className="signal-chip" key={metric.id}>
              <span>{metric.label}</span>
              <button
                type="button"
                aria-label={`Убрать ${metric.label}`}
                onClick={() => remove(metric.id)}
              >
                <X aria-hidden="true" />
              </button>
            </span>
          ))}
          <button
            type="button"
            className="btn ghost sm"
            onClick={() => onChange([])}
          >
            Сбросить
          </button>
        </div>
      )}
      <div className="signal-list" role="group" aria-label="Сигналы">
        {rows.map((metric) => {
          const checked = selectedIds.has(metric.id);
          const unavailable = metric.available === false;
          const disabled = unavailable || (!checked && selected.length >= max);
          const warning =
            typeof metric.mapping_warning === "string"
              ? metric.mapping_warning
              : "Сигнал недоступен";
          return (
            <div className="signal-row" key={metric.id}>
              <Checkbox
                checked={checked}
                disabled={disabled}
                onChange={() => toggle(metric)}
                label={metric.label}
                description={`${metric.id}${metric.unit ? ` · ${metric.unit}` : ""}`}
              />
              {unavailable && (
                <Tooltip text={warning}>
                  <span
                    className="signal-warning"
                    tabIndex={0}
                    aria-label={warning}
                  >
                    <BarChart3 aria-hidden="true" />
                  </span>
                </Tooltip>
              )}
            </div>
          );
        })}
        {rows.length === 0 && (
          <p className="signal-empty">Нет подходящих сигналов</p>
        )}
      </div>
      <small className="signal-count">
        Выбрано {selected.length} из {max}
      </small>
    </div>
  );
}
