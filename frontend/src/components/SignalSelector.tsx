import { BarChart3, Search, SlidersHorizontal, X } from "lucide-react";
import {
  useEffect,
  useLayoutEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import type { Metric } from "../types";
import "./SignalSelector.css";

export function SignalSelector({
  metrics,
  selected: committed,
  onChange,
  max = 20,
}: {
  metrics: Metric[];
  selected: string[];
  onChange: (ids: string[]) => void;
  max?: number;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(committed);
  const selected = open ? draft : committed;
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [plant, setPlant] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const wasOpenRef = useRef(false);
  const dialogId = useId();

  const metricById = useMemo(
    () => new Map(metrics.map((metric) => [metric.id, metric])),
    [metrics],
  );
  const selectedMetrics = selected
    .map((id) => metricById.get(id))
    .filter((metric): metric is Metric => Boolean(metric));
  const selectedIds = new Set(selected);

  const sources = useMemo(
    () => Array.from(new Set(metrics.map((metric) => metric.source))).sort(),
    [metrics],
  );
  const plants = useMemo(
    () => Array.from(new Set(metrics.map((metric) => metric.plant))).sort(),
    [metrics],
  );
  const rows = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const visible = metrics.filter((metric) => {
      const haystack =
        `${metric.label} ${metric.id} ${metric.unit ?? ""}`.toLocaleLowerCase();
      return (
        (!normalizedQuery || haystack.includes(normalizedQuery)) &&
        (!source || metric.source === source) &&
        (!plant || metric.plant === plant)
      );
    });
    const order = new Map(selected.map((id, index) => [id, index]));
    return visible.slice().sort((a, b) => {
      const aOrder = order.get(a.id);
      const bOrder = order.get(b.id);
      if (aOrder !== undefined && bOrder !== undefined) return aOrder - bOrder;
      if (aOrder !== undefined) return -1;
      if (bOrder !== undefined) return 1;
      return 0;
    });
  }, [metrics, plant, query, selected, source]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    searchRef.current?.focus();
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  useLayoutEffect(() => {
    if (!open || !dialogRef.current) return;
    const place = () => {
      const box = triggerRef.current?.getBoundingClientRect(),
        dialog = dialogRef.current;
      if (!box || !dialog) return;
      dialog.style.top = `${Math.max(16, Math.min(box.bottom + 8, window.innerHeight - dialog.offsetHeight - 16))}px`;
      dialog.style.left = `${Math.max(16, Math.min(box.right - dialog.offsetWidth, window.innerWidth - dialog.offsetWidth - 16))}px`;
    };
    place();
    const observer = new ResizeObserver(place);
    observer.observe(dialogRef.current);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  useEffect(() => {
    if (open) {
      wasOpenRef.current = true;
    } else if (wasOpenRef.current) {
      triggerRef.current?.focus();
    }
  }, [open]);

  function close() {
    setOpen(false);
  }

  function remove(id: string) {
    setDraft(selected.filter((selectedId) => selectedId !== id));
  }

  function toggle(metric: Metric) {
    if (selectedIds.has(metric.id)) {
      remove(metric.id);
      return;
    }
    if (metric.available === false || selected.length >= max) return;
    setDraft([...selected, metric.id]);
  }

  function trapFocus(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
    );
    if (!focusable?.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="signal-selector" ref={rootRef}>
      <button
        ref={triggerRef}
        className="signal-selector__trigger"
        type="button"
        aria-expanded={open}
        aria-controls={dialogId}
        onClick={() => {
          if (!open) setDraft(committed);
          setOpen((current) => !current);
        }}
      >
        <SlidersHorizontal aria-hidden="true" />
        <span>Выбрать сигналы · {selected.length}</span>
      </button>
      {open && (
        <div
          className="signal-selector__popover"
          id={dialogId}
          ref={dialogRef}
          role="dialog"
          aria-modal="false"
          aria-label="Выбор показателей"
          onKeyDown={trapFocus}
        >
          <div className="signal-selector__topline">
            <strong>Показатели</strong>
            <button
              className="signal-selector__icon-button"
              type="button"
              aria-label="Закрыть выбор показателей"
              title="Закрыть"
              onClick={close}
            >
              <X aria-hidden="true" />
            </button>
          </div>
          <label
            className="signal-selector__search"
            htmlFor={`${dialogId}-search`}
          >
            <Search aria-hidden="true" />
            <span className="signal-selector__sr-only">Поиск сигнала</span>
            <input
              id={`${dialogId}-search`}
              ref={searchRef}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Поиск сигнала"
            />
          </label>
          <div className="signal-selector__filters">
            <label>
              <span>Источник</span>
              <select
                value={source}
                onChange={(event) => setSource(event.target.value)}
              >
                <option value="">Все</option>
                {sources.map((item) => (
                  <option value={item} key={item}>
                    {item.toUpperCase()}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Установка</span>
              <select
                value={plant}
                onChange={(event) => setPlant(event.target.value)}
              >
                <option value="">Все</option>
                {plants.map((item) => (
                  <option value={item} key={item}>
                    {item.toUpperCase()}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {selectedMetrics.length > 0 && (
            <div
              className="signal-selector__chips"
              aria-label="Выбранные показатели"
            >
              {selectedMetrics.map((metric) => (
                <span className="signal-selector__chip" key={metric.id}>
                  <span title={metric.label}>{metric.label}</span>
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
                className="signal-selector__clear"
                type="button"
                onClick={() => setDraft([])}
              >
                Сбросить
              </button>
            </div>
          )}
          <div
            className="signal-selector__list"
            role="group"
            aria-label="Сигналы"
          >
            {rows.map((metric) => {
              const checked = selectedIds.has(metric.id);
              const unavailable = metric.available === false;
              const limitReached = !checked && selected.length >= max;
              const disabled = unavailable || limitReached;
              const warning =
                typeof metric.mapping_warning === "string"
                  ? metric.mapping_warning
                  : undefined;
              return (
                <label
                  className={`signal-selector__row${disabled ? " signal-selector__row--disabled" : ""}`}
                  key={metric.id}
                  data-metric-id={metric.id}
                  title={unavailable ? warning : undefined}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={disabled}
                    onChange={() => toggle(metric)}
                  />
                  <span className="signal-selector__metric">
                    <strong>{metric.label}</strong>
                    <small>
                      {metric.id}
                      {metric.unit ? ` · ${metric.unit}` : ""}
                    </small>
                  </span>
                  {unavailable && (
                    <BarChart3
                      className="signal-selector__warning"
                      aria-label={warning || "Сигнал недоступен"}
                    />
                  )}
                </label>
              );
            })}
            {rows.length === 0 && (
              <p className="signal-selector__empty">Нет подходящих сигналов</p>
            )}
          </div>
          <footer className="signal-selector__footer">
            <small>
              {selected.length} / {max}
            </small>
            <button
              type="button"
              className="primary"
              onClick={() => {
                onChange(draft);
                close();
              }}
            >
              Применить
            </button>
          </footer>
        </div>
      )}
    </div>
  );
}
