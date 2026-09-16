import { useMemo, useState, type FormEvent } from "react";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clipboard,
  Database,
  FlaskConical,
  History,
  Info,
  Lightbulb,
  Play,
  RotateCcw,
  SlidersHorizontal,
} from "lucide-react";
import { api } from "../api";
import type { ScenarioRequest, ScenarioResult } from "../types";
import { Chart } from "../components/Chart";
import { formatNumber } from "../visualization";
import {
  pipelineStages,
  recommendationScenarios,
} from "../demo/decisionSupportDemo";

const controls = [
  ["ht.P8", "Температура на входе Р-202", "ед."],
  ["ht.T11", "Массовый расход сырья", "%"],
  ["ht.F19", "Давление на входе Р-202", "ед."],
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
}: {
  datasetId: string;
  at: string;
  sandbox: boolean;
  onOpenSandbox?: () => void;
}) {
  const [request, setRequest] = useState(() => initial(at, sandbox));
  const [result, setResult] = useState<ScenarioResult | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [sourceMode, setSourceMode] = useState<"latest" | "period">("latest");
  const [pipelineStarted, setPipelineStarted] = useState(false);
  const [selectedRow, setSelectedRow] = useState(
    "Последний доступный срез · 14:20",
  );
  const [manualMode, setManualMode] = useState(false);
  const [copied, setCopied] = useState(false);
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
    return (
      <section className="decision-page recommendation-page">
        <section className="decision-band recommendation-hero">
          <div className="section-heading">
            <div>
              <h2>
                <Lightbulb /> Рекомендации по качеству
              </h2>
              <p>
                Соберите рекомендацию по последним данным или выбранному
                периоду.
              </p>
              <small className="model-context">
                Демонстрационный pipeline: данные → агенты → 10 сценариев → 6
                сценариев → текст рекомендации
              </small>
            </div>
            <button
              className="primary"
              type="button"
              onClick={() => setPipelineStarted(true)}
            >
              <Play />{" "}
              {pipelineStarted ? "Обновить расчёт" : "Получить рекомендацию"}
            </button>
          </div>
          <div
            className="support-switch"
            role="group"
            aria-label="Период рекомендации"
          >
            <button
              type="button"
              className={sourceMode === "latest" ? "active" : ""}
              onClick={() => setSourceMode("latest")}
            >
              Последние данные
            </button>
            <button
              type="button"
              className={sourceMode === "period" ? "active" : ""}
              onClick={() => setSourceMode("period")}
            >
              Выбранный период
            </button>
          </div>
          {sourceMode === "period" && (
            <div className="scenario-fields three">
              <label>
                Начало периода
                <input type="datetime-local" defaultValue={at.slice(0, 16)} />
              </label>
              <label>
                Конец периода
                <input type="datetime-local" defaultValue={at.slice(0, 16)} />
              </label>
              <label>
                Горизонт
                <select defaultValue="180">
                  <option value="60">1 час</option>
                  <option value="120">2 часа</option>
                  <option value="180">3 часа</option>
                </select>
              </label>
            </div>
          )}
          <div className="support-meta">
            <span>
              <Database /> Набор данных: {datasetId}
            </span>
            <span>
              <History /> Момент: {at.replace("T", " ")}
            </span>
            <span className="status-chip ok">
              <CheckCircle2 /> Входные данные готовы
            </span>
          </div>
        </section>
        {pipelineStarted && (
          <PipelinePreview
            onOpenSandbox={onOpenSandbox}
            onCopy={() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1600);
            }}
            copied={copied}
          />
        )}
        {!pipelineStarted && (
          <section className="empty-state compact">
            <Lightbulb />
            <h2>Рекомендация ещё не рассчитана</h2>
            <p>
              Выберите область данных и запустите демонстрационный pipeline.
            </p>
          </section>
        )}
      </section>
    );
  }

  return (
    <form className="decision-page" onSubmit={run}>
      <DatasetRowPicker
        selectedRow={selectedRow}
        setSelectedRow={setSelectedRow}
        manualMode={manualMode}
        setManualMode={setManualMode}
      />
      {selectedRow && !manualMode && (
        <div className="context-banner">
          <Info /> Загружена строка «{selectedRow}». Изменения применяются
          только к этому сценарию.
        </div>
      )}
      <section className="decision-band">
        <div className="section-heading">
          <div>
            <h2>
              {sandbox
                ? "Сценарий режима и блендинга"
                : "Рекомендация по гидроочистке"}
            </h2>
            <p>
              {sandbox
                ? "Измените исходное качество, воздействия и состав смеси."
                : "Расчёт по простой линейной модели с явным лагом отклика."}
            </p>
            <small className="model-context">
              Локальный расчёт для закрытой сети · LLM не требуется · качество и
              безопасность имеют приоритет над стоимостью
            </small>
          </div>
          <button className="primary" type="submit" disabled={loading}>
            <Play /> {loading ? "Расчёт…" : "Рассчитать сценарий"}
          </button>
        </div>
        <div className="scenario-fields">
          <label>
            Сера в сырье, исходная
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
            Сера в сырье, сценарий
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
            Шаг рекомендаций
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
            Лаг отклика, минут
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
        <div className="scenario-presets" aria-label="Тестовые сценарии">
          <span>Тестовые сценарии</span>
          <button type="button" onClick={() => applyPreset("base")}>
            Базовый режим
          </button>
          <button type="button" onClick={() => applyPreset("feed")}>
            Рост серы сырья
          </button>
          <button type="button" onClick={() => applyPreset("strict")}>
            Строгая цель
          </button>
        </div>
      </section>

      {sandbox && request.changes && (
        <section className="decision-band">
          <div className="section-heading">
            <div>
              <h2>Воздействия</h2>
              <p>Три переменные подтверждены экспертом.</p>
            </div>
            <SlidersHorizontal />
          </div>
          <div className="scenario-fields three">
            <label>
              Δ температуры P8
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
              Δ расхода T11, %
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
              Δ давления F19
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
        <details className="model-parameters">
          <summary>Коэффициенты простой модели</summary>
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
              Прирост цетана на 1%
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

      {sandbox && (
        <section className="decision-band">
          <div className="section-heading">
            <div>
              <h2>Блендинг</h2>
              <p>
                Доли нормализуются; контролируются сера, T95 и цетановое число.
              </p>
            </div>
            <FlaskConical />
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
                            aria-label={`${tank.name} ${key}`}
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
            <h2>Отклик по горизонту</h2>
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
          setRequest(initial(at, true));
          setResult(null);
          setError("");
        }}
      >
        <RotateCcw /> Сбросить к исходным
      </button>
    </form>
  );
}

function DatasetRowPicker({
  selectedRow,
  setSelectedRow,
  manualMode,
  setManualMode,
}: {
  selectedRow: string;
  setSelectedRow: (value: string) => void;
  manualMode: boolean;
  setManualMode: (value: boolean) => void;
}) {
  return (
    <section className="decision-band dataset-picker">
      <div className="section-heading">
        <div>
          <h2>
            <Database /> Исходные данные
          </h2>
          <p>
            Начните с готового среза датасета или переключитесь на ручной ввод.
          </p>
        </div>
        <span className="subtle-badge">Рекомендуемый путь</span>
      </div>
      <div className="picker-row">
        <label>
          Строка датасета
          <select
            disabled={manualMode}
            value={selectedRow}
            onChange={(e) => setSelectedRow(e.target.value)}
          >
            <option>Последний доступный срез · 14:20</option>
            <option>Проба ЛИМС · 12:00</option>
            <option>Срез перед отклонением · 08:30</option>
          </select>
        </label>
        <button
          type="button"
          className={manualMode ? "secondary" : "ghost-button"}
          onClick={() => setManualMode(!manualMode)}
        >
          {manualMode ? "Выбрать строку" : "Ввести вручную"}
        </button>
      </div>
    </section>
  );
}

function PipelinePreview({
  onOpenSandbox,
  onCopy,
  copied,
}: {
  onOpenSandbox?: () => void;
  onCopy: () => void;
  copied: boolean;
}) {
  return (
    <>
      <section className="decision-band pipeline-card">
        <div className="section-heading">
          <div>
            <h2>Состояние pipeline</h2>
            <p>
              Статусы ниже демонстрационные и готовы к замене реальным adapter.
            </p>
          </div>
          <span className="status-chip ok">
            <CheckCircle2 /> Готово
          </span>
        </div>
        <ol className="pipeline-list">
          {pipelineStages.map((stage, index) => (
            <li key={stage} className="complete">
              <span>{index + 1}</span>
              <div>
                <b>{stage}</b>
                <small>{index < 3 ? "Подтверждено" : "Demo-результат"}</small>
              </div>
            </li>
          ))}
        </ol>
      </section>
      <section className="decision-band recommendation-result">
        <div className="section-heading">
          <div>
            <h2>Рекомендация для оператора</h2>
            <p>
              Модель предлагает удерживать качество в целевом диапазоне и
              проверить комбинацию воздействий.
            </p>
          </div>
          <span className="confidence">Уверенность 86%</span>
        </div>
        <div className="recommendation-grid">
          <article>
            <small>Ожидаемый эффект</small>
            <strong>−2,6 мг/кг серы</strong>
            <span>за 90 минут</span>
          </article>
          <article>
            <small>Риск</small>
            <strong>Средний</strong>
            <span>нужна проверка отклика</span>
          </article>
          <article>
            <small>Стоимость</small>
            <strong>+1%</strong>
            <span>к базовому режиму</span>
          </article>
        </div>
        <div className="recommendation-actions">
          <button type="button" className="primary" onClick={onOpenSandbox}>
            <FlaskConical /> Открыть в песочнице <ArrowRight />
          </button>
          <button type="button" className="secondary" onClick={onCopy}>
            <Clipboard /> {copied ? "Скопировано" : "Скопировать"}
          </button>
          <button type="button" className="secondary">
            <History /> Сравнить сценарии
          </button>
        </div>
      </section>
      <section className="decision-band">
        <div className="section-heading">
          <div>
            <h2>Отобранные сценарии</h2>
            <p>
              Из 10 вариантов оставлены 6 с лучшим балансом качества, риска и
              стоимости.
            </p>
          </div>
        </div>
        <div className="scenario-list">
          {recommendationScenarios.map((scenario) => (
            <article key={scenario.id}>
              <div>
                <b>{scenario.title}</b>
                <small>
                  {scenario.effect} · риск: {scenario.risk}
                </small>
              </div>
              <span>
                {scenario.cost}
                <small>{scenario.confidence}%</small>
              </span>
            </article>
          ))}
        </div>
        <details className="model-assumptions">
          <summary>Источники и допущения</summary>
          <ul>
            <li>Использован выбранный датасет и последний доступный момент.</li>
            <li>
              Статусы агентов и текст рекомендации являются демонстрационными.
            </li>
            <li>Текущая линейная модель не заменяет промышленную валидацию.</li>
          </ul>
        </details>
      </section>
    </>
  );
}
