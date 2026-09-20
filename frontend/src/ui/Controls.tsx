import {
  useId,
  useRef,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { Check, ChevronDown, Search, X } from "lucide-react";
import "./controls.css";

/* ---------- Button ---------- */
export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost" | "icon";
  size?: "sm" | "md" | "lg";
  icon?: ReactNode;
  /** Small counter rendered after the label (filters count etc.). */
  badge?: number | string;
}

export function Button({
  variant = "secondary",
  size = "md",
  icon,
  badge,
  className = "",
  children,
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={`btn ${variant} ${size} ${className}`}
      {...rest}
    >
      {icon}
      {children}
      {badge != null && badge !== 0 && badge !== "" && (
        <span className="control-badge">{badge}</span>
      )}
    </button>
  );
}

/* ---------- Checkbox / Switch ---------- */
interface ToggleProps extends Omit<
  InputHTMLAttributes<HTMLInputElement>,
  "type" | "size"
> {
  label: ReactNode;
  description?: ReactNode;
}

export function Checkbox({
  label,
  description,
  className = "",
  ...rest
}: ToggleProps) {
  const id = useId();
  return (
    <label className={`checkbox ${className}`} htmlFor={rest.id ?? id}>
      <input type="checkbox" id={rest.id ?? id} {...rest} />
      <span className="checkbox-box" aria-hidden="true">
        <Check />
      </span>
      <span className="checkbox-text">
        {label}
        {description && <small>{description}</small>}
      </span>
    </label>
  );
}

export function Switch({
  label,
  description,
  className = "",
  ...rest
}: ToggleProps) {
  const id = useId();
  return (
    <label className={`switch ${className}`} htmlFor={rest.id ?? id}>
      <input type="checkbox" role="switch" id={rest.id ?? id} {...rest} />
      <span className="switch-track" aria-hidden="true">
        <span className="switch-thumb" />
      </span>
      <span className="checkbox-text">
        {label}
        {description && <small>{description}</small>}
      </span>
    </label>
  );
}

/* ---------- Segmented control (radio group) ---------- */
export interface SegmentOption<T extends string> {
  value: T;
  label: ReactNode;
  ariaLabel?: string;
  disabled?: boolean;
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  label,
  className = "",
  size = "md",
}: {
  value: T;
  onChange: (value: T) => void;
  options: SegmentOption<T>[];
  label: string;
  className?: string;
  size?: "sm" | "md";
}) {
  const ref = useRef<HTMLDivElement>(null);
  const onKeyDown = (event: KeyboardEvent) => {
    const enabled = options.filter((o) => !o.disabled);
    const index = enabled.findIndex((o) => o.value === value);
    let next = index;
    if (event.key === "ArrowRight" || event.key === "ArrowDown")
      next = (index + 1) % enabled.length;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp")
      next = (index - 1 + enabled.length) % enabled.length;
    else return;
    event.preventDefault();
    onChange(enabled[next].value);
    ref.current
      ?.querySelector<HTMLElement>(`[data-value="${enabled[next].value}"]`)
      ?.focus();
  };
  return (
    <div
      ref={ref}
      role="radiogroup"
      aria-label={label}
      className={`segmented ${size} ${className}`}
      onKeyDown={onKeyDown}
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={option.value === value}
          aria-label={option.ariaLabel}
          data-value={option.value}
          tabIndex={option.value === value ? 0 : -1}
          disabled={option.disabled}
          className={option.value === value ? "active" : ""}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/* ---------- Tabs ---------- */
export function Tabs<T extends string>({
  value,
  onChange,
  tabs,
  label,
  className = "",
}: {
  value: T;
  onChange: (value: T) => void;
  tabs: { value: T; label: ReactNode; badge?: number }[];
  label: string;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const onKeyDown = (event: KeyboardEvent) => {
    const index = tabs.findIndex((t) => t.value === value);
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft")
      next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    onChange(tabs[next].value);
    ref.current
      ?.querySelector<HTMLElement>(`[data-value="${tabs[next].value}"]`)
      ?.focus();
  };
  return (
    <div
      ref={ref}
      role="tablist"
      aria-label={label}
      className={`tabs ${className}`}
      onKeyDown={onKeyDown}
    >
      {tabs.map((tab) => (
        <button
          key={tab.value}
          type="button"
          role="tab"
          id={`tab-${tab.value}`}
          aria-selected={tab.value === value}
          aria-controls={`panel-${tab.value}`}
          data-value={tab.value}
          tabIndex={tab.value === value ? 0 : -1}
          className={tab.value === value ? "active" : ""}
          onClick={() => onChange(tab.value)}
        >
          {tab.label}
          {tab.badge ? (
            <span className="control-badge">{tab.badge}</span>
          ) : null}
        </button>
      ))}
    </div>
  );
}

/* ---------- Disclosure ---------- */
export function Disclosure({
  summary,
  children,
  defaultOpen,
  open,
  onToggle,
  className = "",
  summaryClassName = "",
  meta,
  icon,
}: {
  summary: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  open?: boolean;
  onToggle?: (open: boolean) => void;
  className?: string;
  summaryClassName?: string;
  /** Secondary text under the summary. */
  meta?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <details
      className={`disclosure ${className}`}
      open={open ?? defaultOpen}
      onToggle={(event) =>
        onToggle?.((event.target as HTMLDetailsElement).open)
      }
    >
      <summary className={`disclosure-summary ${summaryClassName}`}>
        {icon && <span className="disclosure-icon">{icon}</span>}
        <span className="disclosure-text">
          <span>{summary}</span>
          {meta && <small>{meta}</small>}
        </span>
        <ChevronDown className="disclosure-chevron" aria-hidden="true" />
      </summary>
      <div className="disclosure-body">{children}</div>
    </details>
  );
}

/* ---------- Search field ---------- */
export function SearchField({
  value,
  onChange,
  label,
  placeholder = "Поиск",
  className = "",
  autoFocus,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
  placeholder?: string;
  className?: string;
  autoFocus?: boolean;
}) {
  return (
    <div className={`input-shell search-field ${className}`}>
      <Search className="input-icon" aria-hidden="true" />
      <input
        type="search"
        className="input"
        value={value}
        placeholder={placeholder}
        aria-label={label}
        autoComplete="off"
        autoFocus={autoFocus}
        onChange={(event) => onChange(event.target.value)}
      />
      {value && (
        <button
          type="button"
          className="input-addon"
          aria-label="Очистить"
          onClick={() => onChange("")}
        >
          <X />
        </button>
      )}
    </div>
  );
}

/* ---------- Text field ---------- */
export function TextField({
  label,
  hint,
  id,
  className = "",
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & {
  label: ReactNode;
  hint?: ReactNode;
}) {
  const autoId = useId();
  const inputId = id ?? autoId;
  return (
    <div className={`field ${className}`}>
      <label htmlFor={inputId}>{label}</label>
      <div className="input-shell">
        <input id={inputId} className="input" {...rest} />
      </div>
      {hint && <small className="field-hint">{hint}</small>}
    </div>
  );
}
