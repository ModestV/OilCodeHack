import { ChevronLeft, ChevronRight, Clock } from "lucide-react";
import { HelpTooltip } from "./HelpTooltip";
import { DateTimeField } from "../ui/DateTimeField";
import { Menu, MenuItem, MenuLabel, MenuSeparator } from "../ui/Menu";
import { Segmented } from "../ui/Controls";
import { Tooltip } from "../ui/Tooltip";

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

export const demoScenarios = [
  {
    label: "Стабильный период",
    date: "01.07.2025",
    from: "2025-07-01T00:00:00",
    to: "2025-07-02T00:00:00",
    moment: "2025-07-01T12:00:00",
  },
  {
    label: "Превышение серы",
    date: "16–18.03.2025",
    from: "2025-03-16T00:00:00",
    to: "2025-03-19T00:00:00",
    moment: "2025-03-17T12:00:00",
  },
  {
    label: "Зависание ПАК",
    date: "19.06.2026",
    from: "2026-06-19T00:00:00",
    to: "2026-06-20T00:00:00",
    moment: "2026-06-19T12:00:00",
  },
];

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
  const isAll = from === allRange[0] && to === allRange[1];
  const [dataStart, dataEnd] = allRange;
  const presetsFor = (kind: "from" | "to") => [
    ...(dataStart ? [{ label: "Начало данных", value: dataStart }] : []),
    ...(dataEnd
      ? [
          {
            label: "Конец данных",
            value: kind === "to" ? dataEnd : offset(dataEnd, -1440),
          },
        ]
      : []),
  ];
  return (
    <section className="timebar" aria-label="Выбор времени">
      <div className="mode-control">
        <Segmented
          label="Режим времени"
          value={mode}
          onChange={setMode}
          options={[
            { value: "period", label: "Период" },
            { value: "moment", label: "Момент" },
          ]}
        />
        <HelpTooltip label="Режим времени">
          «Период» показывает агрегаты и динамику между двумя датами. «Момент»
          показывает последние доступные измерения к выбранному времени.
        </HelpTooltip>
      </div>
      <div className="date-control">
        {mode === "period" && (
          <>
            <DateTimeField
              label="Начало периода"
              hideLabel
              value={from}
              min={dataStart || undefined}
              max={to}
              presets={presetsFor("from")}
              onChange={(value) => setRange(value, to)}
            />
            <span className="date-arrow" aria-hidden="true">
              →
            </span>
          </>
        )}
        <DateTimeField
          label={
            mode === "moment"
              ? "Момент измерения"
              : "Конец периода (не включён)"
          }
          hideLabel
          value={to}
          min={mode === "period" ? from : dataStart || undefined}
          max={dataEnd || undefined}
          presets={presetsFor("to")}
          onChange={(value) => setRange(from, value)}
        />
      </div>
      <div className="move" role="group" aria-label="Сдвиг времени">
        <Tooltip text="Назад на 10 минут">
          <button
            type="button"
            className="btn icon"
            onClick={() => shift(-10)}
            aria-label="Назад на 10 минут"
          >
            <ChevronLeft />
          </button>
        </Tooltip>
        <Tooltip text="Вперёд на 10 минут">
          <button
            type="button"
            className="btn icon"
            onClick={() => shift(10)}
            aria-label="Вперёд на 10 минут"
          >
            <ChevronRight />
          </button>
        </Tooltip>
      </div>
      {mode === "period" && (
        <Menu
          label={isAll ? "Всё" : activePreset || "Интервал"}
          ariaLabel="Интервал периода"
          icon={<Clock aria-hidden="true" />}
          sheetTitle="Интервал"
        >
          {presets.map(([label, hours]) => (
            <MenuItem
              key={label}
              selected={!isAll && activePreset === label}
              onSelect={() => setRange(offset(to, -hours * 60), to)}
            >
              {label}
            </MenuItem>
          ))}
          <MenuItem selected={isAll} onSelect={() => setRange(...allRange)}>
            Всё
          </MenuItem>
          {scenarios && (
            <>
              <MenuSeparator />
              <MenuLabel>Демо-сценарии</MenuLabel>
              {demoScenarios.map((s) => (
                <MenuItem
                  key={s.label}
                  description={s.date}
                  onSelect={() => setRange(s.from, s.to)}
                >
                  {s.label}
                </MenuItem>
              ))}
            </>
          )}
        </Menu>
      )}
      {scenarios && mode === "moment" && (
        <Menu label="Демо-сценарии" sheetTitle="Демо-сценарии">
          {demoScenarios.map((s) => (
            <MenuItem
              key={s.label}
              description={s.date}
              onSelect={() => setRange(s.from, s.moment)}
            >
              {s.label}
            </MenuItem>
          ))}
        </Menu>
      )}
    </section>
  );
}
