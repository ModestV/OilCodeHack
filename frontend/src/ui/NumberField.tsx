import { useEffect, useId, useState, type ReactNode } from "react";
import { Minus, Plus } from "lucide-react";
import { formatNumberInput, parseNumber, stepValue } from "./number";
import "./controls.css";

export interface NumberFieldProps {
  value: number | null | undefined;
  onChange: (value: number | null) => void;
  label: ReactNode;
  id?: string;
  min?: number;
  max?: number;
  step?: number;
  /** Decimal places used when formatting a committed value. */
  decimals?: number;
  unit?: string;
  hint?: ReactNode;
  required?: boolean;
  disabled?: boolean;
  size?: "sm" | "md";
  className?: string;
  hideLabel?: boolean;
  /** Show −/+ stepper buttons (default true). */
  stepper?: boolean;
}

/**
 * Text-based numeric input: accepts "," and ".", never shows native spinners,
 * ignores wheel scrolling, and steps with buttons or arrow keys.
 */
export function NumberField({
  value,
  onChange,
  label,
  id,
  min,
  max,
  step = 1,
  decimals,
  unit,
  hint,
  required,
  disabled,
  size = "md",
  className = "",
  hideLabel,
  stepper = true,
}: NumberFieldProps) {
  const autoId = useId();
  const inputId = id ?? autoId;
  const [text, setText] = useState(() => formatNumberInput(value, decimals));
  const [invalid, setInvalid] = useState(false);
  useEffect(() => {
    setText(formatNumberInput(value, decimals));
    setInvalid(false);
  }, [value, decimals]);

  const commit = (raw = text) => {
    const parsed = parseNumber(raw);
    if (raw.trim() === "") {
      setInvalid(Boolean(required));
      if (value != null) onChange(null);
      return;
    }
    if (parsed == null) {
      setInvalid(true);
      return;
    }
    let next = parsed;
    if (min != null) next = Math.max(min, next);
    if (max != null) next = Math.min(max, next);
    setInvalid(false);
    if (next !== value) onChange(next);
    else setText(formatNumberInput(value, decimals));
  };
  const bump = (direction: 1 | -1) => {
    if (disabled) return;
    onChange(
      stepValue(parseNumber(text) ?? value ?? null, step, direction, min, max),
    );
  };

  return (
    <div className={`field number-field ${size} ${className}`}>
      <label htmlFor={inputId} className={hideLabel ? "sr-only" : undefined}>
        {label}
      </label>
      <div className={`input-shell${invalid ? " invalid" : ""}`}>
        {stepper && (
          <button
            type="button"
            className="input-addon"
            aria-label="Уменьшить"
            tabIndex={-1}
            disabled={
              disabled || (min != null && value != null && value <= min)
            }
            onClick={() => bump(-1)}
          >
            <Minus />
          </button>
        )}
        <input
          id={inputId}
          className="input num"
          inputMode="decimal"
          autoComplete="off"
          value={text}
          disabled={disabled}
          required={required}
          aria-invalid={invalid || undefined}
          aria-describedby={hint ? `${inputId}-hint` : undefined}
          onChange={(event) => {
            setText(event.target.value);
            setInvalid(false);
          }}
          onBlur={() => commit()}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commit();
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              bump(1);
            } else if (event.key === "ArrowDown") {
              event.preventDefault();
              bump(-1);
            }
          }}
          onWheel={(event) => (event.target as HTMLElement).blur()}
        />
        {unit && <span className="input-unit">{unit}</span>}
        {stepper && (
          <button
            type="button"
            className="input-addon"
            aria-label="Увеличить"
            tabIndex={-1}
            disabled={
              disabled || (max != null && value != null && value >= max)
            }
            onClick={() => bump(1)}
          >
            <Plus />
          </button>
        )}
      </div>
      {hint && (
        <small id={`${inputId}-hint`} className="field-hint">
          {hint}
        </small>
      )}
    </div>
  );
}
