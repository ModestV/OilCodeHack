import { useMemo, useState, type FormEvent } from "react";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clipboard,
  Play,
  RotateCcw,
} from "lucide-react";
import { api } from "../api";
import type { ScenarioRequest, ScenarioResult } from "../types";
import { Chart } from "../components/Chart";
import { offset } from "../components/TimeControls";
import { formatNumber } from "../visualization";
import {
  pipelineStages,
} from "../demo/decisionSupportDemo";

const controls = [
  ["ht.T6", "Температура на входе Р-202", "ед."],
  ["ht.F9", "Массовый расход сырья", "%"],
  ["ht.P13", "Давление на входе Р-202", "ед."],
] as const;

const initial = (at: string, sandbox: boolean): ScenarioRequest => ({
  at,
  horizon_minutes: 180,
  step_minutes: 30,
  baseline_feed_sulfur: 15,
  feed_sulfur: 15,
  targets: { sulfur_max: 10, t95_max: 360, cetane_min: 51 },
  parameters: {
    lag_minutes: 90,
    feed_sulfur_transfer: 0.2,
    temperature_effect: -0.08,
    feed_rate_effect: 0.05,
    pressure_effect: -0.15,
    cetane_gain_per_pct: 4,
  },
  changes: sandbox
    ? { temperature: 0, feed_rate_pct: 0, pressure: 0 }
    : undefined,
  tanks: sandbox
    ? [
        {
          name: "Резервуар 1",
          share: 60,
          sulfur: 8,
          t95: 350,
          cetane: 50,
          cost_index: 1,
        },
        {
          name: "Резервуар 2",
          share: 40,
          sulfur: 12,
          t95: 355,
          cetane: 48,
          cost_index: 0.98,
        },
      ]
    : [],
  additive_pct: sandbox ? 0.5 : 0,
});

const number = (value: string) => Number(value.replace(",", "."));

export function DecisionSupportView({
  datasetId,
  at,
  sandbox,
  onOpenSandbox,
  datasetName,
  latestAt,
}: {
  datasetId: string;
  at: string;
  sandbox: boolean;
  datasetName: string;
  latestAt: string;
  onOpenSandbox?: () => void;
}) {
  const [request, setRequest] = useState(() => initial(at, sandbox));
  const [result, setResult] = useState<ScenarioResult | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [sourceMode, setSourceMode] = useState<"latest" | "period">("latest");
  const [pipelineStarted, setPipelineStarted] = useState(false);
  const [selectedRow, setSelectedRow] = useState<"base" | "feed" | "strict">(
    "base",
  );
  const [periodFrom, setPeriodFrom] = useState(
    offset(latestAt, -1440).slice(0, 16),
  );
  const [periodTo, setPeriodTo] = useState(latestAt.slice(0, 16));
  const [recommendationPeriod, setRecommendationPeriod] = useState("");
  const [manualMode, setManualMode] = useState(false);
  const applyPreset = (preset: "base" | "feed" | "strict") => {
    const next = initial(at, sandbox);
    if (preset === "feed") next.feed_sulfur = 30;
    if (preset === "strict") {
      next.feed_sulfur = 22;
      next.targets.sulfur_max = 8;
      next.additive_pct = sandbox ? 1 : 0;
    }
    setRequest(next);
    setResult(null);
    setError("");
  };
  const update = (path: string, value: number) => {
    setResult(null);
    setRequest((current) => {
      const next = structuredClone(current);
      const parts = path.split(".");
      let target = next as unknown as Record<string, unknown>;
      for (const part of parts.slice(0, -1))
        target = target[part] as Record<string, unknown>;
      target[parts.at(-1)!] = value;
      return next;
    });
  };
  const run = async (event: FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      setResult(await api.scenario(datasetId, { ...request, at }));
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Ошибка сценарного расчёта",
      );
    } finally {
      setLoading(false);
    }
  };
  const runRecommendation = async () => {
    setLoading(true);
    setError("");
    try {
      const recommendationAt =
        sourceMode === "period" && periodTo ? periodTo : at;
      const next = {
        ...initial(recommendationAt, false),
        at: recommendationAt,
      };
      setResult(await api.scenario(datasetId, next));
      setRecommendationPeriod(
        sourceMode === "latest"
          ? `На ${displayTime(latestAt)}`
          : `${displayTime(periodFrom)} — ${displayTime(periodTo)}`,
      );
      setPipelineStarted(true);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Ошибка сценарного расчёта",
      );
      setPipelineStarted(false);
    } finally {
      setLoading(false);
    }
  };
  const option = useMemo(
    () => ({
      grid: { left: 58, right: 24, top: 32, bottom: 42 },
      tooltip: { trigger: "axis" },
      xAxis: {
        type: "category",
        name: "мин",
        data: result?.trajectory.map((p) => p.minute) || [],
      },
      yAxis: { type: "value", name: "мг/кг", scale: true },
      series: [
        {
          name: "Прогноз серы",
          type: "line",
          smooth: false,
          symbolSize: 7,
          data: result?.trajectory.map((p) => p.sulfur) || [],
          markLine: {
            silent: true,
            data: [{ yAxis: request.targets.sulfur_max, name: "Цель" }],
          },
        },
      ],
    }),
    [request.targets.sulfur_max, result],
  );

  if (!sandbox) {
    const validPeriod =
      sourceMode === "latest" ||
      Boolean(periodFrom && periodTo && periodFrom < periodTo);
    return (
      <section className="decision-page recommendation-page">
        <section className="decision-band recommendation-controls">
          <div className="support-toolbar">
            <div
              className="support-switch"
              role="group"
              aria-label="Данные для рекомендации"
            >
              <button
                type="button"
                aria-pressed={sourceMode === "latest"}
                className={sourceMode === "latest" ? "active" : ""}
                onClick={() => {
                  setSourceMode("latest");
                  setPipelineStarted(false);
                }}
              >
                Последние данные
              </button>
              <button
                type="button"
                aria-pressed={sourceMode === "period"}
                className={sourceMode === "period" ? "active" : ""}
                onClick={() => {
                  setSourceMode("period");
                  setPipelineStarted(false);
                }}
              >
                За период
              </button>
            </div>
            <button
              className="primary"
              type="button"
              disabled={!validPeriod}
              onClick={runRecommendation}
            >
              {loading
                ? "Расчёт…"
                : pipelineStarted
                  ? "Пересчитать"
                  : "Рассчитать сценарий"}
            </button>
          </div>
          {sourceMode === "period" && (
            <div className="support-dates">
              <label>
                С
                <input
                  type="datetime-local"
                  value={periodFrom}
                  onChange={(e) => {
                    setPeriodFrom(e.target.value);
                    setPipelineStarted(false);
                  }}
                />
              </label>
              <label>
                По
                <input
                  type="datetime-local"
                  value={periodTo}
                  onChange={(e) => {
                    setPeriodTo(e.target.value);
                    setPipelineStarted(false);
                  }}
                />
              </label>
              {!validPeriod && (
                <span className="error" role="status">
                  Конец периода должен быть позже начала.
                </span>
              )}
            </div>
          )}
          <div className="support-meta">
            <span>{datasetName}</span>
            <span>Последняя запись · {displayTime(latestAt)}</span>
          </div>
        </section>
        {error && <p className="error" role="alert">{error}</p>}
        <p className="support-notice">
          Локальная сценарная модель · коэффициенты и ограничения являются допущениями
        </p>
        {pipelineStarted ? (
          <PipelinePreview
            onOpenSandbox={onOpenSandbox}
            period={recommendationPeriod}
            result={result}
          />
        ) : (
          <section className="support-empty">
            <h2>Здесь появится рекомендация</h2>
            <p>Изменения режима, ожидаемое качество и риски.</p>
          </section>
        )}
      </section>
    );
  }

  return (
    <form className="decision-page" onSubmit={run}>
      <DatasetRowPicker
        selectedRow={selectedRow}
        onSelect={(value) => {
          setSelectedRow(value);
          applyPreset(value);
        }}
        manualMode={manualMode}
        setManualMode={(value) => {
          setManualMode(value);
          setResult(null);
        }}
      />
      <section className="decision-band">
        <div className="section-heading">
          <div>
            <h2>Условия расчёта</h2>
          </div>
          <button className="primary" type="submit" disabled={loading}>
            <Play /> {loading ? "Расчёт…" : "Рассчитать сценарий"}
          </button>
        </div>
        <div className="scenario-fields">
          <label>
            Сера в сырье до изменения
            <input
              type="number"
              min="0"
              step="0.1"
              value={request.baseline_feed_sulfur}
              onChange={(e) =>
                update("baseline_feed_sulfur", number(e.target.value))
              }
            />
          </label>
          <label>
            Сера в сырье после изменения
            <input
              type="number"
              min="0"
              step="0.1"
              value={request.feed_sulfur}
              onChange={(e) => update("feed_sulfur", number(e.target.value))}
            />
          </label>
          <label>
            Цель по сере, мг/кг
            <input
              type="number"
              min="0.1"
              step="0.1"
              value={request.targets.sulfur_max}
              onChange={(e) =>
                update("targets.sulfur_max", number(e.target.value))
              }
            />
          </label>
          <label>
            Горизонт
            <select
              value={request.horizon_minutes}
              onChange={(e) =>
                update("horizon_minutes", number(e.target.value))
              }
            >
              <option value="60">1 час</option>
              <option value="120">2 часа</option>
              <option value="180">3 часа</option>
            </select>
          </label>
          <label>
            Шаг прогноза
            <select
              value={request.step_minutes}
              onChange={(e) => update("step_minutes", number(e.target.value))}
            >
              <option value="15">15 минут</option>
              <option value="30">30 минут</option>
              <option value="60">1 час</option>
            </select>
          </label>
          <label>
            Задержка отклика, мин
            <input
              type="number"
              min="0"
              max="180"
              step="15"
              value={request.parameters.lag_minutes}
              onChange={(e) =>
                update("parameters.lag_minutes", number(e.target.value))
              }
            />
          </label>
        </div>
      </section>

      {sandbox && request.changes && (
        <section className="decision-band">
          <div className="section-heading">
            <div>
              <h2>Изменения режима</h2>
            </div>
          </div>
          <div className="scenario-fields three">
            <label>
              Изменение температуры · T6
              <input
                type="number"
                min="-10"
                max="10"
                step="0.5"
                value={request.changes.temperature}
                onChange={(e) =>
                  update("changes.temperature", number(e.target.value))
                }
              />
            </label>
            <label>
              Изменение расхода · F9, %
              <input
                type="number"
                min="-10"
                max="10"
                step="0.5"
                value={request.changes.feed_rate_pct}
                onChange={(e) =>
                  update("changes.feed_rate_pct", number(e.target.value))
                }
              />
            </label>
            <label>
              Изменение давления · P13
              <input
                type="number"
                min="-2"
                max="2"
                step="0.1"
                value={request.changes.pressure}
                onChange={(e) =>
                  update("changes.pressure", number(e.target.value))
                }
              />
            </label>
          </div>
        </section>
      )}

      {sandbox && (
        <section className="decision-band">
          <div className="section-heading">
            <div>
              <h2>Состав смеси</h2>
              <p>Доли компонентов пересчитываются до 100%.</p>
            </div>
          </div>
          <div className="blend-table-wrap">
            <table className="blend-table">
              <thead>
                <tr>
                  <th>Компонент</th>
                  <th>Доля, %</th>
                  <th>Сера</th>
                  <th>T95</th>
                  <th>Цетановое число</th>
                </tr>
              </thead>
              <tbody>
                {request.tanks.map((tank, index) => (
                  <tr key={tank.name}>
                    <td>{tank.name}</td>
                    {(["share", "sulfur", "t95", "cetane"] as const).map(
                      (key) => (
                        <td key={key}>
                          <input
                            aria-label={`${tank.name}: ${{ share: "доля", sulfur: "сера", t95: "T95", cetane: "цетановое число" }[key]}`}
                            type="number"
                            min="0"
                            step="0.1"
                            value={tank[key]}
                            onChange={(e) =>
                              update(
                                `tanks.${index}.${key}`,
                                number(e.target.value),
                              )
                            }
                          />
                        </td>
                      ),
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="scenario-fields three">
            <label>
              Присадка, %
              <input
                type="number"
                min="0"
                max="3"
                step="0.1"
                value={request.additive_pct}
                onChange={(e) => update("additive_pct", number(e.target.value))}
              />
            </label>
            <label>
              Цель T95, °C
              <input
                type="number"
                min="1"
                step="1"
                value={request.targets.t95_max}
                onChange={(e) =>
                  update("targets.t95_max", number(e.target.value))
                }
              />
            </label>
            <label>
              Мин. цетановое число
              <input
                type="number"
                min="1"
                step="0.1"
                value={request.targets.cetane_min}
                onChange={(e) =>
                  update("targets.cetane_min", number(e.target.value))
                }
              />
            </label>
          </div>
        </section>
      )}

      {sandbox && (
        <details className="model-parameters">
          <summary>Параметры модели</summary>
          <div className="scenario-fields">
            <label>
              Перенос серы сырья
              <input
                type="number"
                min="0.01"
                step="0.01"
                value={request.parameters.feed_sulfur_transfer}
                onChange={(e) =>
                  update(
                    "parameters.feed_sulfur_transfer",
                    number(e.target.value),
                  )
                }
              />
            </label>
            <label>
              Эффект температуры
              <input
                type="number"
                max="-0.001"
                step="0.01"
                value={request.parameters.temperature_effect}
                onChange={(e) =>
                  update(
                    "parameters.temperature_effect",
                    number(e.target.value),
                  )
                }
              />
            </label>
            <label>
              Эффект расхода
              <input
                type="number"
                min="0.01"
                step="0.01"
                value={request.parameters.feed_rate_effect}
                onChange={(e) =>
                  update("parameters.feed_rate_effect", number(e.target.value))
                }
              />
            </label>
            <label>
              Эффект давления
              <input
                type="number"
                max="-0.001"
                step="0.01"
                value={request.parameters.pressure_effect}
                onChange={(e) =>
                  update("parameters.pressure_effect", number(e.target.value))
                }
              />
            </label>
            <label>
              Прирост цетанового числа на 1% присадки
              <input
                type="number"
                min="0"
                step="0.1"
                value={request.parameters.cetane_gain_per_pct}
                onChange={(e) =>
                  update(
                    "parameters.cetane_gain_per_pct",
                    number(e.target.value),
                  )
                }
              />
            </label>
          </div>
        </details>
      )}

      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      {result && (
        <>
          <section className="decision-results" aria-live="polite">
            <article>
              <small>Сера сейчас</small>
              <strong>{formatNumber(result.baseline.sulfur, 2)}</strong>
              <span>мг/кг</span>
            </article>
            <article className={result.sulfur_target_met ? "ok" : "danger"}>
              <small>Через {result.horizon_minutes} мин</small>
              <strong>{formatNumber(result.predicted_sulfur, 2)}</strong>
              <span>
                {result.sulfur_target_met ? (
                  <>
                    <CheckCircle2 /> цель достигнута
                  </>
                ) : (
                  <>
                    <AlertTriangle /> выше цели
                  </>
                )}
              </span>
            </article>
            {controls.map(([id, label, unit]) => {
              const item = result.controls[id];
              return (
                <article key={id}>
                  <small>{label}</small>
                  <strong>
                    {item.change > 0 ? "+" : ""}
                    {formatNumber(item.change, 2)} {item.relative ? "%" : unit}
                  </strong>
                  <span>
                    {item.recommended === null
                      ? id
                      : `новое: ${formatNumber(item.recommended, 2)}`}
                  </span>
                </article>
              );
            })}
          </section>
          <section className="decision-band">
            <h2>Прогноз серы</h2>
            <Chart option={option} />
          </section>
          {result.blend && (
            <section className="decision-results blend-results">
              <article
                className={result.blend.meets_targets.sulfur ? "ok" : "danger"}
              >
                <small>Сера смеси</small>
                <strong>{formatNumber(result.blend.sulfur, 2)}</strong>
                <span>мг/кг</span>
              </article>
              <article
                className={result.blend.meets_targets.t95 ? "ok" : "danger"}
              >
                <small>T95 смеси</small>
                <strong>{formatNumber(result.blend.t95, 1)}</strong>
                <span>°C</span>
              </article>
              <article
                className={result.blend.meets_targets.cetane ? "ok" : "danger"}
              >
                <small>Цетановое число</small>
                <strong>{formatNumber(result.blend.cetane, 1)}</strong>
                <span>ед.</span>
              </article>
              <article>
                <small>Индекс стоимости</small>
                <strong>{formatNumber(result.blend.cost_index, 3)}</strong>
                <span>ДТ = 1, присадка = 100</span>
              </article>
            </section>
          )}
          <details className="model-assumptions">
            <summary>Допущения модели</summary>
            <ul>
              {result.assumptions.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </details>
        </>
      )}
      <button
        className="secondary reset-scenario"
        type="button"
        onClick={() => {
          applyPreset(selectedRow);
        }}
      >
        <RotateCcw /> Сбросить изменения
      </button>
    </form>
  );
}

function displayTime(value: string) {
  const [date, time] = value.split("T");
  return `${date.split("-").reverse().join(".")} ${time?.slice(0, 5) || ""}`.trim();
}

function DatasetRowPicker({
  selectedRow,
  onSelect,
  manualMode,
  setManualMode,
}: {
  selectedRow: "base" | "feed" | "strict";
  onSelect: (value: "base" | "feed" | "strict") => void;
  manualMode: boolean;
  setManualMode: (value: boolean) => void;
}) {
  return (
    <section className="decision-band dataset-picker">
      <div className="section-heading">
        <h2>Исходные данные</h2>
        <span className="support-notice">Учебный пример</span>
      </div>
      <div className="picker-row">
        {!manualMode ? (
          <label>
            Набор значений
            <select
              value={selectedRow}
              onChange={(e) => onSelect(e.target.value as typeof selectedRow)}
            >
              <option value="base">Базовый режим</option>
              <option value="feed">Рост серы в сырье</option>
              <option value="strict">Сниженная цель по сере</option>
            </select>
          </label>
        ) : (
          <span className="muted">Ручной ввод</span>
        )}
        <button
          type="button"
          className="ghost-button"
          onClick={() => setManualMode(!manualMode)}
        >
          {manualMode ? "Выбрать пример" : "Ввести вручную"}
        </button>
      </div>
    </section>
  );
}

function PipelinePreview({
  onOpenSandbox,
  period,
  result,
}: {
  onOpenSandbox?: () => void;
  period: string;
  result: ScenarioResult | null;
}) {
  const [copyStatus, setCopyStatus] = useState("");
  const predicted = result?.predicted_sulfur;
  const baseline = result?.baseline.sulfur;
  const reduction =
    baseline != null && predicted != null ? baseline - predicted : null;
  const summary = result
    ? `Сценарный расчёт: сера ${formatNumber(predicted)} мг/кг после ${result.horizon_minutes} минут; исходное значение ${formatNumber(baseline)} мг/кг.`
    : "Расчёт не вернул результат.";
  async function copy() {
    try {
      await navigator.clipboard.writeText(`${period}\n${summary}`);
      setCopyStatus("Текст скопирован");
    } catch {
      setCopyStatus("Не удалось скопировать. Выделите текст рекомендации.");
    }
  }
  return (
    <>
      <section className="decision-band recommendation-result">
        <p className="support-meta">{period}</p>
        <h2>
          {result?.sulfur_target_met
            ? "Текущий сценарий укладывается в цель"
            : "Цель по сере не достигается текущими изменениями"}
        </h2>
        <p className="recommendation-copy">
          {result
            ? `Прогноз: ${formatNumber(predicted)} мг/кг после ${result.horizon_minutes} минут.`
            : "Нет данных для сценарного расчёта."}
        </p>
        <dl className="recommendation-metrics">
          <div>
            <dt>Снижение серы</dt>
            <dd>
              {formatNumber(reduction)} <span>мг/кг</span>
            </dd>
          </div>
          <div>
            <dt>Риск</dt>
            <dd>{result?.sulfur_target_met ? "Допустимый" : "Требует проверки"}</dd>
          </div>
          <div>
            <dt>Стоимость к исходной</dt>
            <dd>
              {result ? "Модельная оценка" : "—"}
            </dd>
          </div>
        </dl>
        <div className="recommendation-actions">
          <button type="button" className="primary" onClick={onOpenSandbox}>
            Проверить в песочнице <ArrowRight />
          </button>
          <button type="button" className="ghost-button" onClick={copy}>
            <Clipboard /> Скопировать текст
          </button>
          <span className="support-notice" role="status">
            {copyStatus}
          </span>
        </div>
      </section>
      <section className="decision-band">
        <div className="section-heading">
          <h2>Сравнение сценариев</h2>
          <span className="support-notice">Результат одного сценария</span>
        </div>
        <div
          className="scenario-table-scroll"
          tabIndex={0}
          role="region"
          aria-label="Сравнение сценариев"
        >
          <table className="scenario-comparison">
            <thead>
              <tr>
                <th scope="col">Сценарий</th>
                <th scope="col">Ожидаемый эффект</th>
                <th scope="col">Риск</th>
                <th scope="col">Стоимость</th>
              </tr>
            </thead>
            <tbody>
              <tr className="is-preferred">
                <td>Линейный сценарий изменения режима</td>
                <td>
                  {reduction == null ? "—" : `${formatNumber(reduction)} мг/кг серы`}
                </td>
                <td>{result?.sulfur_target_met ? "Допустимый" : "Требует проверки"}</td>
                <td>Не задана</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
      <details className="support-details">
        <summary>Как получена рекомендация</summary>
        <p>Расчёт выполнен локальным endpoint сценарной модели. LLM в этом шаге не используется.</p>
        <ol>
          {pipelineStages.map((stage) => (
            <li key={stage}>{stage}</li>
          ))}
        </ol>
        <p>
          Результат модели требует проверки перед изменением режима установки.
        </p>
      </details>
    </>
  );
}


