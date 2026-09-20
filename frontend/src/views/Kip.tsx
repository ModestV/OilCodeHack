import { useMemo, useState } from "react";
import { ChartNoAxesCombined } from "lucide-react";
import { HelpTooltip } from "../components/HelpTooltip";
import type { Metric, Snapshot, Summary } from "../types";
import { FlagLine, format, freshness, stamp } from "./shared";
import { Select } from "../ui/Select";
import { Disclosure, SearchField, Segmented } from "../ui/Controls";
import { Tooltip } from "../ui/Tooltip";

export function Kip({
  metrics,
  snapshot,
  mode = "moment",
  summary,
  onTrend,
  selected = [],
}: {
  metrics: Metric[];
  snapshot: Snapshot | null;
  mode?: "period" | "moment";
  summary?: Summary | null;
  onTrend?: (id: string) => void;
  selected?: string[];
}) {
  const [q, setQ] = useState(""),
    [plant, setPlant] = useState<"all" | "avt" | "ht">("all");
  const vals = useMemo(
    () => new Map(snapshot?.values.map((v) => [v.metric_id, v])),
    [snapshot],
  );
  const stats = useMemo(
    () => new Map(summary?.metrics.map((v) => [v.metric_id, v])),
    [summary],
  );
  const rows = useMemo(() => {
    const selectedSet = new Set(selected);
    return metrics
      .filter(
        (m) =>
          m.source === "kip" &&
          (plant === "all" || m.plant === plant) &&
          `${m.label} ${m.id} ${m.group} ${m.description || ""}`
            .toLowerCase()
            .includes(q.toLowerCase()),
      )
      .sort((a, b) => {
        const aProblem =
          mode === "period"
            ? (stats.get(a.id)?.suspect_count ?? 0) > 0
            : (vals.get(a.id)?.flags.length ?? 0) > 0;
        const bProblem =
          mode === "period"
            ? (stats.get(b.id)?.suspect_count ?? 0) > 0
            : (vals.get(b.id)?.flags.length ?? 0) > 0;
        return (
          Number(bProblem) - Number(aProblem) ||
          Number(selectedSet.has(b.id)) - Number(selectedSet.has(a.id))
        );
      });
  }, [metrics, plant, q, mode, stats, vals, selected]);
  return (
    <section className="panel">
      <header>
        <div>
          <h2>
            Технология и КИП{" "}
            <HelpTooltip label="КИП">
              КИП — технологические контрольно-измерительные приборы установки.
            </HelpTooltip>
          </h2>
          <p>Показателей: {rows.length}</p>
        </div>
        <div className="filters">
          <SearchField
            label="Поиск КИП"
            placeholder="Тег, аппарат или описание"
            value={q}
            onChange={setQ}
          />
          <Select
            label="Установка"
            value={plant}
            onChange={setPlant}
            options={[
              { value: "all", label: "Все установки" },
              { value: "avt", label: "АВТ" },
              { value: "ht", label: "Гидроочистка" },
            ]}
          />
        </div>
      </header>
      <AvtScheme />
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Показатель / аппарат</th>
              <th>{mode === "period" ? "Медиана и диапазон" : "Значение"}</th>
              <th>
                {mode === "period"
                  ? "Первое / последнее измерение"
                  : "Время / возраст"}
              </th>
              <th>Качество</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => {
              const v = vals.get(m.id),
                s = stats.get(m.id);
              return (
                <tr key={m.id}>
                  <td>
                    {m.label}
                    <small>
                      {m.id} · {m.group}
                    </small>
                  </td>
                  <td>
                    {format(mode === "period" ? s?.median : v?.value)}
                    {m.unit ? ` ${m.unit}` : ""}
                    {mode === "period" ? (
                      <small>
                        мин. {format(s?.min)} · макс. {format(s?.max)} · n=
                        {s?.count || 0}
                      </small>
                    ) : (
                      <small className={v?.freshness === "fresh" ? "" : "warn"}>
                        {freshness(v?.freshness)}
                      </small>
                    )}
                  </td>
                  <td>
                    {mode === "period" ? (
                      <>
                        {stamp(s?.first_at)}
                        <small>{stamp(s?.last_at)}</small>
                      </>
                    ) : (
                      <>
                        {stamp(v?.timestamp)}
                        <small>
                          {format(v?.age_minutes, 0)} мин · Δ {format(v?.delta)}
                        </small>
                      </>
                    )}
                  </td>
                  <td>
                    {mode === "period" ? (
                      <small>
                        Некорректных: {s?.invalid_count || 0} · подозрительных:{" "}
                        {s?.suspect_count || 0}
                      </small>
                    ) : (
                      <FlagLine flags={v?.flags} />
                    )}
                    <small className="warn">{m.mapping_warning || ""}</small>
                    {mode === "period" && m.available !== false && onTrend && (
                      <Tooltip text={`Открыть тренд ${m.id}`}>
                        <button
                          type="button"
                          className="icon-button kip-trend"
                          aria-label={`Открыть тренд ${m.id}`}
                          onClick={() => onTrend(m.id)}
                        >
                          <ChartNoAxesCombined />
                        </button>
                      </Tooltip>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
function AvtScheme() {
  const [page, setPage] = useState(1);
  return (
    <Disclosure summary="Схема АВТ" className="avt-scheme">
      <Segmented
        label="Страница схемы"
        size="sm"
        value={String(page)}
        onChange={(value) => setPage(Number(value))}
        options={["К-1", "К-2", "К-10"].map((label, index) => ({
          value: String(index + 1),
          label,
        }))}
      />
      <a href={"/api/reference/avt/" + page} target="_blank" rel="noreferrer">
        <img
          src={"/api/reference/avt/" + page}
          alt={"Технологическая схема АВТ, страница " + page}
          style={{ width: "100%", maxHeight: 620, objectFit: "contain" }}
        />
      </a>
    </Disclosure>
  );
}
