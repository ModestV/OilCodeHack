import { CalendarDays, ChevronLeft, ChevronRight } from "lucide-react";
export type Mode = "period" | "moment";
export const fmt = (d: Date) => d.toISOString().slice(0, 19);
export const offset = (s: string, minutes: number) =>
  fmt(new Date(Date.parse(s.replace(/Z$/, "") + "Z") + minutes * 60000));
const presets = [
  ["1 ч", 1],
  ["8 ч", 8],
  ["24 ч", 24],
  ["7 д", 168],
  ["30 д", 720],
] as const;
export function TimeControls({
  mode,
  setMode,
  from,
  to,
  setRange,
  allRange,
  scenarios = false,
}: {
  mode: Mode;
  setMode: (m: Mode) => void;
  from: string;
  to: string;
  setRange: (f: string, t: string) => void;
  allRange: [string, string];
  scenarios?: boolean;
}) {
  const shift = (min: number) =>
    setRange(mode === "period" ? offset(from, min) : from, offset(to, min));
  return (
    <>
      <section className="timebar" aria-label="Выбор времени">
        <div className="segment">
          {(["period", "moment"] as const).map((m) => (
            <button
              key={m}
              aria-pressed={mode === m}
              className={mode === m ? "active" : ""}
              onClick={() => setMode(m)}
            >
              {m === "period" ? "Период" : "Момент"}
            </button>
          ))}
        </div>
        <div className="date-control">
          <CalendarDays />
          {mode === "period" && (
            <>
              <input
                type="datetime-local"
                step="1"
                value={from}
                onChange={(e) => setRange(e.target.value, to)}
                aria-label="Начало периода"
              />
              <span>→</span>
            </>
          )}
          <input
            type="datetime-local"
            step="1"
            value={to}
            onChange={(e) => setRange(from, e.target.value)}
            aria-label={
              mode === "moment"
                ? "Момент измерения"
                : "Конец периода (не включён)"
            }
          />
        </div>
        <div className="move">
          <button
            onClick={() => shift(-10)}
            title="Назад на 10 минут"
            aria-label="Назад на 10 минут"
          >
            <ChevronLeft />
          </button>
          <button
            onClick={() => shift(10)}
            title="Вперёд на 10 минут"
            aria-label="Вперёд на 10 минут"
          >
            <ChevronRight />
          </button>
        </div>
        {mode === "period" && (
          <div className="presets">
            {presets.map(([l, h]) => (
              <button key={l} onClick={() => setRange(offset(to, -h * 60), to)}>
                {l}
              </button>
            ))}
            <button onClick={() => setRange(...allRange)}>Всё</button>
          </div>
        )}
      </section>
      {scenarios && (
        <div className="scenarios">
          <span>Сценарии:</span>
          {[
            [
              "01.07.2025",
              "2025-07-01T00:00",
              "2025-07-02T00:00",
              "2025-07-01T12:00",
            ],
            [
              "16–18.03.2025",
              "2025-03-16T00:00",
              "2025-03-19T00:00",
              "2025-03-17T12:00",
            ],
            [
              "19.06.2026",
              "2026-06-19T00:00",
              "2026-06-20T00:00",
              "2026-06-19T12:00",
            ],
          ].map(([label, f, t, m]) => (
            <button
              key={label}
              onClick={() => setRange(f, mode === "moment" ? m : t)}
            >
              {label}
            </button>
          ))}
        </div>
      )}
    </>
  );
}
