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
import type {
  DecisionResult,
  ScenarioRequest,
  ScenarioResult,
} from "../types";
import { Chart } from "../components/Chart";
import { formatNumber } from "../visualization";
import {
  pipelineStages,
} from "../demo/decisionSupportDemo";

const controls = [
  ["ht.T6", "Температура на входе Р-202", "°C"],
  ["ht.F9", "Массовый расход сырья", "%"],
  ["ht.P13", "Давление на входе Р-202", "МПа"],
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
const forecastReason = (reason: string) => ({
  no_telemetry_evidence: "нет свежей телеметрии",
  too_many_missing_features: "слишком много пропусков",
  outside_training_support: "данные за пределами области обучения",
  model_not_yet_available_at_origin: "момент предшествует завершению обучения и выбора модели (01.01.2026)",
  nonfinite_model_output: "некорректный численный результат",
}[reason] || reason);

export function DecisionSupportView({
  datasetId,
  at,
  sandbox,
  onOpenSandbox,
  datasetName,
  latestAt,
  initialRequest,
}: {
  datasetId: string;
  at: string;
  sandbox: boolean;
  datasetName: string;
  latestAt: string;
  onOpenSandbox?: (request: ScenarioRequest) => void;
  initialRequest?: ScenarioRequest;
}) {
  const [request, setRequest] = useState(() => initialRequest ? structuredClone(initialRequest) : initial(at, sandbox));
  const [result, setResult] = useState<ScenarioResult | null>(null);
  const [decision, setDecision] = useState<DecisionResult | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [sourceMode, setSourceMode] = useState<"latest" | "period">("latest");
  const [pipelineStarted, setPipelineStarted] = useState(false);
  const [selectedRow, setSelectedRow] = useState<"base" | "feed" | "strict">(
    "base",
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
    setDecision(null);
    setError("");
  };
  const update = (path: string, value: number) => {
    setResult(null);
    setDecision(null);
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
      setResult(await api.scenario(datasetId, request));
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
        sourceMode === "period" && periodTo ? periodTo : latestAt;
      const next = {
        ...initial(recommendationAt, false),
        at: recommendationAt,
      };
      const nextDecision = await api.decision(datasetId, next);
      setDecision(nextDecision);
      setResult(nextDecision.scenario);
      setRecommendationPeriod(
        sourceMode === "latest"
          ? `На ${displayTime(latestAt)}`
          : `На ${displayTime(periodTo)}`,
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
      Boolean(periodTo);
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
                Момент в истории
              </button>
            </div>
            <button
              className="primary"
              type="button"
              disabled={!validPeriod || loading}
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
                Момент расчёта
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
                  Выберите момент расчёта.
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
          Прогноз на 3 часа и сравнение сценариев. Коэффициенты изменения режима — допущения для проверки технологом.
        </p>
        {pipelineStarted ? (
          <PipelinePreview
            onOpenSandbox={onOpenSandbox}
            period={recommendationPeriod}
            result={result}
            decision={decision}
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
            <p className="support-notice">На {displayTime(request.at)}{initialRequest ? " · условия из рекомендации" : ""}</p>
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
              <p>Сумма долей компонентов должна быть ровно 100%.</p>
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
            <article className={result.sulfur_target_met && result.hard_sulfur_limit_met ? "ok" : "danger"}>
              <small>Через {result.horizon_minutes} мин</small>
              <strong>{formatNumber(result.predicted_sulfur, 2)}</strong>
              <span>
                {result.sulfur_target_met && result.hard_sulfur_limit_met ? (
                  <>
                    <CheckCircle2 /> цель достигнута
                  </>
                ) : (
                  <>
                    <AlertTriangle /> превышение цели или 10 мг/кг
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
                className={result.blend.meets_targets.sulfur && result.blend.sulfur <= 10 ? "ok" : "danger"}
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
  decision,
}: {
  onOpenSandbox?: (request: ScenarioRequest) => void;
  period: string;
  result: ScenarioResult | null;
  decision: DecisionResult | null;
}) {
  const [copyStatus, setCopyStatus] = useState("");
  const sulfurEvidence = decision?.agents?.quality.evidence.sulfur;
  const predicted = result?.predicted_sulfur;
  const baseline = result?.baseline.sulfur;
  const reduction =
    baseline != null && predicted != null ? baseline - predicted : null;
  const summary = decision?.status === "abstain"
    ? `Рекомендация не сформирована: ${decision.abstain?.reason || "Недостаточно подтверждений."}`
    : result
      ? `Сценарный расчёт: сера ${formatNumber(predicted)} мг/кг после ${result.horizon_minutes} минут; исходное значение ${formatNumber(baseline)} мг/кг. Изменение режима требует проверки технологом; причинные эффекты заданы допущениями.`
      : "Нет данных для расчёта.";
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
          {decision?.status === "abstain"
            ? "Надёжной рекомендации нет"
            : result?.sulfur_target_met
            ? "Выбран сценарий для рассмотрения"
            : "Цель по сере не достигается текущими изменениями"}
        </h2>
        <p className="recommendation-copy">
          {decision?.status === "abstain"
            ? decision.abstain?.reason || "Пайплайн остановлен из-за качества данных."
            : result
            ? `Прогноз: ${formatNumber(predicted)} мг/кг после ${result.horizon_minutes} минут.`
            : "Нет данных для сценарного расчёта."}
        </p>
        {sulfurEvidence && (
          <p className="support-notice">
            Исходная сера: {formatNumber(sulfurEvidence.value)} мг/кг · {sulfurEvidence.source || "источник отсутствует"}
            {sulfurEvidence.timestamp && ` · проба ${displayTime(sulfurEvidence.timestamp)}`}
            {sulfurEvidence.available_at && ` · доступна ${displayTime(sulfurEvidence.available_at)}`}
          </p>
        )}
        <dl className="recommendation-metrics">
          <div>
            <dt>Снижение серы</dt>
            <dd>
              {formatNumber(reduction)} <span>мг/кг</span>
            </dd>
          </div>
          <div>
            <dt>Проверка ограничений</dt>
            <dd>
              {decision?.status === "abstain"
                ? "Отказ"
                : decision?.safety_gate?.passed
                  ? "Пройдена в модели"
                  : "Требует проверки"}
            </dd>
          </div>
          <div>
            <dt>Экономия в деньгах</dt>
            <dd>
              Не оценивалась
            </dd>
          </div>
        </dl>
        {result && (
          <div className="scenario-table-scroll">
            <table className="scenario-comparison" aria-label="Параметры выбранного сценария">
              <thead><tr><th>Параметр</th><th>Сейчас</th><th>Предлагается</th><th>Изменение</th></tr></thead>
              <tbody>{controls.map(([id, label, unit]) => {
                const item = result.controls[id];
                return <tr key={id}><td>{label} · {id}</td><td>{formatNumber(item.current, 2)}</td><td>{formatNumber(item.recommended, 2)}</td><td>{formatNumber(item.change, 2)} {unit}</td></tr>;
              })}</tbody>
            </table>
          </div>
        )}
        <p className="support-notice">
          {decision?.basis === "scenario_only"
            ? "Расчёт по заданным вручную условиям; прогноз по наблюдениям не подтверждает этот сценарий. "
            : "Прогноз по наблюдениям и сценарный эффект рассчитаны разными моделями. "}
          Проверка относится к концу горизонта. Без расчёта смеси влияние режима на T95 и цетановое число не оценено; производственная безопасность не подтверждена.
        </p>
        <div className="recommendation-actions">
          <button type="button" className="primary" onClick={() => {
            if (!decision) return;
            const seed = initial(decision.at, true);
            seed.tanks = [];
            seed.additive_pct = 0;
            if (result) {
              seed.current_sulfur = result.baseline.sulfur;
              seed.horizon_minutes = result.horizon_minutes;
              seed.step_minutes = result.step_minutes;
              seed.changes = { temperature: result.controls["ht.T6"].change,
                feed_rate_pct: result.controls["ht.F9"].change, pressure: result.controls["ht.P13"].change };
            }
            onOpenSandbox?.(seed);
          }}>
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
          <span className="support-notice">Проверенные варианты: {decision?.candidates?.length || 0}</span>
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
                <th scope="col">Сера в конце горизонта</th>
                <th scope="col">Проверка ограничений</th>
                <th scope="col">Масштаб изменения</th>
                <th scope="col">Выпуск, Δ%</th>
                <th scope="col">Энергозатраты, индекс</th>
                <th scope="col">Нагрузка, индекс</th>
              </tr>
            </thead>
            <tbody>
              {(decision?.candidates || []).map((candidate) => (
                <tr
                  key={candidate.id}
                  className={candidate.id === decision?.selected_candidate ? "is-preferred" : ""}
                >
                  <td>
                    {candidate.label}
                    {candidate.id === decision?.selected_candidate && (
                      <span className="support-notice"> · выбрано</span>
                    )}
                  </td>
                  <td>
                    {candidate.predicted_sulfur == null
                      ? "—"
                      : `${formatNumber(candidate.predicted_sulfur)} мг/кг серы`}
                  </td>
                  <td>
                    {candidate.status === "error"
                      ? "Ошибка"
                      : candidate.feasible
                        ? "Пройдена в модели"
                        : "Не пройдена"}
                    {!!candidate.safety_gate?.reasons.length && (
                      <ul>{candidate.safety_gate.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
                    )}
                  </td>
                  <td>
                    {candidate.effort == null ? "—" : formatNumber(candidate.effort, 2)}
                  </td>
                  <td>{formatNumber(candidate.objectives?.throughput_change_pct, 1)}</td>
                  <td>{formatNumber(candidate.objectives?.energy_cost_index, 3)}</td>
                  <td>{formatNumber(candidate.objectives?.regime_severity.index, 3)}</td>
                </tr>
              ))}
              {!decision?.candidates?.length && (
                <tr><td colSpan={7}>Сравнение не выполнено: сначала нужны достоверные исходные данные.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <p className="support-notice">Выпуск предполагает неизменный выход продукта; энергия — условные затраты относительно текущего режима (=1). Нагрузка оценивает высокие T6/P13/F9 относительно обучающей истории, не вероятность отказа. Выбор учитывает все три критерия и масштаб изменений после проверки ограничений.</p>
      </section>
      {decision?.forecast && (
        <section className="decision-band model-forecast" aria-label="Модельный прогноз серы">
          <div className="section-heading">
            <h2>Отдельный прогноз качества</h2>
            <span className="support-notice">
              Ridge · горизонт {decision.forecast.forecast_horizon_minutes / 60} ч
            </span>
          </div>
          <p className="recommendation-copy">
            Расчёт на {displayTime(decision.forecast.target_time)} по данным, доступным на {displayTime(decision.forecast.at)}.
            Результат ЛИМС доступен через {decision.forecast.lims_publication_delay_minutes / 60} ч после отбора пробы.
            Прогноз не оценивает эффект предложенного изменения режима.
          </p>
          {decision.forecast.status === "abstain" && (
            <p className="error" role="status">Прогноз не выдан: {decision.forecast.reasons.map(forecastReason).join("; ")}</p>
          )}
          <dl className="recommendation-metrics">
            <div>
              <dt>Ridge прогноз</dt>
              <dd>{formatNumber(decision.forecast.prediction_ridge, 2)} <span>мг/кг</span></dd>
            </div>
            <div>
              <dt>Консервативная оценка</dt>
              <dd>{formatNumber(decision.forecast.prediction_risk_guard, 2)} <span>мг/кг</span></dd>
            </div>
            <div>
              <dt>Признаки</dt>
              <dd>{decision.forecast.feature_count - decision.forecast.imputed_feature_count}/{decision.forecast.feature_count}</dd>
            </div>
          </dl>
          <p className="support-notice">
            {decision.forecast.status === "abstain"
              ? "Числовой прогноз скрыт, поскольку проверка входных данных не пройдена."
              : decision.forecast.alarm_above_10
                ? "Консервативная оценка выше 10 мг/кг: нужна проверка технологом."
                : "Оценка ниже 10 мг/кг не исключает превышение: модель пропускает часть опасных проб."}
            {decision.forecast.leakage_check.passed ? " Временные границы признаков соблюдены." : " Нарушены временные границы признаков."}
          </p>
          {!!decision.forecast.warnings.length && (
            <details><summary>Ограничения прогноза</summary><ul>{decision.forecast.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></details>
          )}
        </section>
      )}
      <details className="support-details">
        <summary>Как получена рекомендация</summary>
        <p>
          Система проверяет качество и доступность данных, рассчитывает прогноз,
          затем сравнивает варианты режима по ограничениям и масштабу изменений.
        </p>
        <ol>
          {(decision?.trace?.length
            ? decision.trace.map(
                (stage) => `${stage.role}: ${stage.summary} (${stage.status})`,
              )
            : pipelineStages
          ).map((stage) => <li key={stage}>{stage}</li>)}
        </ol>
        <p>
          Результат модели требует проверки перед изменением режима установки.
        </p>
      </details>
    </>
  );
}


