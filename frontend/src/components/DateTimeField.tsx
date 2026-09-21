import { CalendarDays, ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { UiSelect } from "./UiSelect";
import "./Controls.css";

const pad = (value: number) => String(value).padStart(2, "0");

function fromValue(value: string) {
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!match) return new Date();
  return new Date(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]),
    Number(match[4]),
    Number(match[5]),
  );
}

function serialize(date: Date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function display(value: string) {
  const date = fromValue(value);
  return `${pad(date.getDate())}.${pad(date.getMonth() + 1)}.${date.getFullYear()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function parse(text: string) {
  const match = text.trim().match(/^(\d{2})\.(\d{2})\.(\d{4})[ T](\d{2}):(\d{2})$/);
  if (!match) return null;
  const date = new Date(
    Number(match[3]),
    Number(match[2]) - 1,
    Number(match[1]),
    Number(match[4]),
    Number(match[5]),
  );
  if (
    date.getFullYear() !== Number(match[3]) ||
    date.getMonth() !== Number(match[2]) - 1 ||
    date.getDate() !== Number(match[1]) ||
    date.getHours() !== Number(match[4]) ||
    date.getMinutes() !== Number(match[5])
  ) return null;
  return serialize(date);
}

const weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const months = [
  "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
];
const hours = Array.from({ length: 24 }, (_, value) => ({
  value: pad(value), label: pad(value),
}));
const minutes = Array.from({ length: 60 }, (_, value) => ({
  value: pad(value), label: pad(value),
}));

export function DateTimeField({
  value,
  onChange,
  ariaLabel,
}: {
  value: string;
  onChange: (value: string) => void;
  ariaLabel: string;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(() => display(value));
  const [month, setMonth] = useState(() => {
    const date = fromValue(value);
    return new Date(date.getFullYear(), date.getMonth(), 1);
  });
  const root = useRef<HTMLDivElement>(null);
  const selected = fromValue(value);

  useEffect(() => setDraft(display(value)), [value]);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  const days = useMemo(() => {
    const start = new Date(month);
    const mondayOffset = (start.getDay() + 6) % 7;
    start.setDate(start.getDate() - mondayOffset);
    return Array.from({ length: 42 }, (_, index) => {
      const date = new Date(start);
      date.setDate(start.getDate() + index);
      return date;
    });
  }, [month]);

  function commit() {
    const parsed = parse(draft);
    if (parsed) onChange(parsed);
    else setDraft(display(value));
  }

  function updatePart(part: "hours" | "minutes", next: string) {
    const date = fromValue(value);
    if (part === "hours") date.setHours(Number(next));
    else date.setMinutes(Number(next));
    onChange(serialize(date));
  }

  return (
    <div className="datetime-field" ref={root}>
      <input
        type="text"
        inputMode="numeric"
        aria-label={ariaLabel}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") commit();
          if (event.key === "ArrowDown" && event.altKey) setOpen(true);
        }}
      />
      <button
        type="button"
        className="datetime-field__toggle"
        aria-label={`Открыть календарь: ${ariaLabel}`}
        aria-expanded={open}
        onClick={() => {
          const date = fromValue(value);
          setMonth(new Date(date.getFullYear(), date.getMonth(), 1));
          setOpen((current) => !current);
        }}
      >
        <CalendarDays aria-hidden="true" />
      </button>
      {open && (
        <div className="datetime-popover" role="dialog" aria-label={ariaLabel}>
          <header>
            <button
              type="button"
              aria-label="Предыдущий месяц"
              onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}
            ><ChevronLeft /></button>
            <strong>{months[month.getMonth()]} {month.getFullYear()}</strong>
            <button
              type="button"
              aria-label="Следующий месяц"
              onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}
            ><ChevronRight /></button>
          </header>
          <div className="datetime-popover__weekdays">
            {weekdays.map((day) => <span key={day}>{day}</span>)}
          </div>
          <div className="datetime-popover__days">
            {days.map((date) => {
              const sameMonth = date.getMonth() === month.getMonth();
              const isSelected =
                date.getFullYear() === selected.getFullYear() &&
                date.getMonth() === selected.getMonth() &&
                date.getDate() === selected.getDate();
              return (
                <button
                  type="button"
                  key={date.toISOString()}
                  className={`${sameMonth ? "" : "is-outside"} ${isSelected ? "is-selected" : ""}`.trim()}
                  aria-pressed={isSelected}
                  onClick={() => {
                    const next = fromValue(value);
                    next.setFullYear(date.getFullYear(), date.getMonth(), date.getDate());
                    onChange(serialize(next));
                  }}
                >{date.getDate()}</button>
              );
            })}
          </div>
          <footer>
            <span>Время</span>
            <UiSelect
              ariaLabel="Часы"
              className="datetime-popover__time"
              value={pad(selected.getHours())}
              options={hours}
              onChange={(next) => updatePart("hours", next)}
            />
            <span>:</span>
            <UiSelect
              ariaLabel="Минуты"
              className="datetime-popover__time"
              value={pad(selected.getMinutes())}
              options={minutes}
              onChange={(next) => updatePart("minutes", next)}
            />
            <button type="button" className="secondary" onClick={() => setOpen(false)}>Готово</button>
          </footer>
        </div>
      )}
    </div>
  );
}
