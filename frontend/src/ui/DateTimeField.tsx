import { useEffect, useId, useRef, useState } from "react";
import { Calendar, ChevronLeft, ChevronRight } from "lucide-react";
import { Popover } from "./Popover";
import { NumberField } from "./NumberField";
import {
  MASK,
  MONTHS,
  WEEKDAYS,
  applyMask,
  clampIso,
  daysInMonth,
  formatMasked,
  monthGrid,
  parseMasked,
  parts,
  toIso,
} from "./datetime";
import "./controls.css";

export interface DateTimePreset {
  label: string;
  value: string;
}

export interface DateTimeFieldProps {
  value: string;
  onChange: (iso: string) => void;
  label: string;
  id?: string;
  min?: string;
  max?: string;
  presets?: DateTimePreset[];
  className?: string;
  disabled?: boolean;
  /** Visually hide the label (it is still read to assistive tech). */
  hideLabel?: boolean;
}

/**
 * Masked text field (ДД.ММ.ГГГГ ЧЧ:ММ) plus our own calendar/time popover, so
 * the control is identical in every browser instead of the native picker.
 */
export function DateTimeField({
  value,
  onChange,
  label,
  id,
  min,
  max,
  presets,
  className = "",
  disabled,
  hideLabel,
}: DateTimeFieldProps) {
  const autoId = useId();
  const inputId = id ?? autoId;
  const [text, setText] = useState(() => formatMasked(value));
  const [invalid, setInvalid] = useState(false);
  const [open, setOpen] = useState(false);
  const anchorRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    setText(formatMasked(value));
    setInvalid(false);
  }, [value]);

  const commit = () => {
    const iso = parseMasked(text);
    if (!iso) {
      setInvalid(text.trim() !== "" && text !== formatMasked(value));
      if (text.trim() === "") setText(formatMasked(value));
      return;
    }
    const clamped = clampIso(iso, min, max);
    setInvalid(false);
    if (clamped !== value) onChange(clamped);
    else setText(formatMasked(value));
  };

  const current = parts(value) ??
    parts(max ?? min ?? "") ?? { y: 2026, mo: 1, d: 1, h: 0, mi: 0 };
  const [view, setView] = useState({ y: current.y, mo: current.mo });
  useEffect(() => {
    if (open) setView({ y: current.y, mo: current.mo });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const pick = (
    y: number,
    mo: number,
    d: number,
    h = current.h,
    mi = current.mi,
  ) => {
    onChange(
      clampIso(toIso(y, mo, Math.min(d, daysInMonth(y, mo)), h, mi), min, max),
    );
  };
  const shiftMonth = (delta: number) => {
    let mo = view.mo + delta;
    let y = view.y;
    if (mo < 1) {
      mo = 12;
      y -= 1;
    } else if (mo > 12) {
      mo = 1;
      y += 1;
    }
    setView({ y, mo });
  };
  const inRange = (iso: string) =>
    (!min || iso >= min.slice(0, 10)) && (!max || iso <= max.slice(0, 10));
  const todayIso = toIso(current.y, current.mo, current.d).slice(0, 10);

  return (
    <div className={`field datetime-field ${className}`} ref={anchorRef}>
      <label htmlFor={inputId} className={hideLabel ? "sr-only" : undefined}>
        {label}
      </label>
      <div className={`input-shell${invalid ? " invalid" : ""}`}>
        <input
          id={inputId}
          className="input"
          inputMode="numeric"
          autoComplete="off"
          placeholder={MASK}
          value={text}
          disabled={disabled}
          aria-invalid={invalid || undefined}
          aria-describedby={invalid ? `${inputId}-hint` : undefined}
          onChange={(event) => {
            setText(applyMask(event.target.value));
            setInvalid(false);
          }}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commit();
            } else if (event.key === "ArrowDown" && event.altKey) {
              event.preventDefault();
              setOpen(true);
            }
          }}
        />
        <button
          ref={buttonRef}
          type="button"
          className="input-addon"
          aria-label={`${label}: открыть календарь`}
          aria-haspopup="dialog"
          aria-expanded={open}
          disabled={disabled}
          onClick={() => setOpen((v) => !v)}
        >
          <Calendar />
        </button>
      </div>
      {invalid && (
        <small id={`${inputId}-hint`} className="field-hint error" role="alert">
          Формат: {MASK}
        </small>
      )}
      <Popover
        open={open}
        onClose={() => setOpen(false)}
        anchorRef={anchorRef}
        label={label}
        className="calendar-popover"
        sheetTitle={label}
        initialFocus={() =>
          document.querySelector<HTMLElement>(
            ".calendar-day[aria-selected='true']",
          ) ??
          document.querySelector<HTMLElement>(".calendar-day:not(:disabled)")
        }
      >
        <div className="calendar">
          <div className="calendar-head">
            <button
              type="button"
              className="icon-button"
              aria-label="Предыдущий месяц"
              onClick={() => shiftMonth(-1)}
            >
              <ChevronLeft />
            </button>
            <span className="calendar-title" aria-live="polite">
              {MONTHS[view.mo - 1]} {view.y}
            </span>
            <button
              type="button"
              className="icon-button"
              aria-label="Следующий месяц"
              onClick={() => shiftMonth(1)}
            >
              <ChevronRight />
            </button>
          </div>
          <div
            className="calendar-grid"
            role="grid"
            aria-label={`${MONTHS[view.mo - 1]} ${view.y}`}
          >
            <div role="row" className="calendar-week">
              {WEEKDAYS.map((w) => (
                <span key={w} role="columnheader" className="calendar-weekday">
                  {w}
                </span>
              ))}
            </div>
            {monthGrid(view.y, view.mo).map((row, r) => (
              <div key={r} role="row" className="calendar-week">
                {row.map((d, c) => {
                  if (d === null) return <span key={c} role="gridcell" />;
                  const iso = toIso(view.y, view.mo, d).slice(0, 10);
                  const selected = iso === todayIso;
                  return (
                    <button
                      key={c}
                      type="button"
                      role="gridcell"
                      className={`calendar-day${selected ? " selected" : ""}`}
                      aria-selected={selected}
                      disabled={!inRange(iso)}
                      onClick={() => pick(view.y, view.mo, d)}
                    >
                      {d}
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
          <div className="calendar-time">
            <NumberField
              label="Часы"
              value={current.h}
              min={0}
              max={23}
              step={1}
              decimals={0}
              size="sm"
              onChange={(h) =>
                pick(current.y, current.mo, current.d, h ?? 0, current.mi)
              }
            />
            <span aria-hidden="true">:</span>
            <NumberField
              label="Минуты"
              value={current.mi}
              min={0}
              max={59}
              step={10}
              decimals={0}
              size="sm"
              onChange={(mi) =>
                pick(current.y, current.mo, current.d, current.h, mi ?? 0)
              }
            />
          </div>
          {presets && presets.length > 0 && (
            <div className="calendar-presets">
              {presets.map((preset) => (
                <button
                  key={preset.label}
                  type="button"
                  className="chip-button"
                  onClick={() => {
                    onChange(clampIso(preset.value, min, max));
                    setOpen(false);
                  }}
                >
                  {preset.label}
                </button>
              ))}
            </div>
          )}
          <div className="calendar-actions">
            <button
              type="button"
              className="primary"
              onClick={() => setOpen(false)}
            >
              Готово
            </button>
          </div>
        </div>
      </Popover>
    </div>
  );
}
