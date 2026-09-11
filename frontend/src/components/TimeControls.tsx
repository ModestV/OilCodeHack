import {
  CalendarDays,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import { HelpTooltip } from "./HelpTooltip";
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
  const spanHours = Math.round(
    (Date.parse(to.replace(/Z$/, "") + "Z") -
      Date.parse(from.replace(/Z$/, "") + "Z")) /
      3600000,
  );
  const activePreset = presets.find(([, hours]) => hours === spanHours)?.[0];
  return (
    <>
      <section className="timebar" aria-label="Выбор времени">
        <div className="mode-control">
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
          <HelpTooltip label="Режим времени">
            «Период» показывает агрегаты и динамику между двумя датами. «Момент»
            показывает последние доступные измерения к выбранному времени.
          </HelpTooltip>
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
          <details className="compact-menu interval-menu">
            <summary>
              {activePreset || "Интервал"} <ChevronDown />
            </summary>
            <div>
              {presets.map(([label, hours]) => (
                <button
                  key={label}
                  onClick={(event) => {
                    setRange(offset(to, -hours * 60), to);
                    event.currentTarget
                      .closest("details")
                      ?.removeAttribute("open");
                  }}
                >
                  {label}
                </button>
              ))}
              <button
                onClick={(event) => {
                  setRange(...allRange);
                  event.currentTarget
                    .closest("details")
                    ?.removeAttribute("open");
                }}
              >
                Всё
              </button>
            </div>
          </details>
        )}
      </section>
      {scenarios && (
        <details className="compact-menu scenario-menu">
          <summary>
            Демо-сценарии <ChevronDown />
          </summary>
          <div>
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
                onClick={(event) => {
                  setRange(f, mode === "moment" ? m : t);
                  event.currentTarget
                    .closest("details")
                    ?.removeAttribute("open");
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </details>
      )}
    </>
  );
}
