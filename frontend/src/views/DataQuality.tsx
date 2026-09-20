import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  ArrowRight,
  CircleHelp,
  ChevronDown,
  Database,
  Download,
  Info,
  ShieldCheck,
  ChartNoAxesCombined,
  Search,
} from "lucide-react";
import { Chart } from "../components/Chart";
import {
  SulfurAtMoment,
  MedianComparison,
  QualityRanking,
} from "../components/MonitoringCharts";
import { HelpTooltip } from "../components/HelpTooltip";
import { chartTimeLabel, sourceEpoch } from "../visualization";
import { useChartTheme } from "../ui/useChartTheme";
import {
  buildOperatorAssessment,
  type AttentionTarget,
  type OperatorAssessment,
} from "../operatorStatus";
import { api, exportUrl } from "../api";
import type {
  Distribution,
  Formula,
  Issue,
  Manifest,
  Metric,
  Quality,
  SeriesResponse,
  Snapshot,
  Stat,
  Summary,
} from "../types";
import {
  Empty,
  FlagLine,
  epoch,
  format,
  freshness,
  metricMap,
  stamp,
} from "./shared";

export function Formulas({ formulas }: { formulas: Formula[] }) {
  return (
    <section className="panel">
      <h2>
        Расчёты ВАК{" "}
        <HelpTooltip label="ВАК">
          Диагностические расчёты по значениям КИП. Они помогают анализу, но не
          заменяют лабораторную оценку.
        </HelpTooltip>
      </h2>
      <div className="formula-list">
        {formulas.map((f) => (
          <details key={f.id}>
            <summary>
              <span>
                <b>{f.label}</b>
                <small>
                  {f.expression} · версия {f.version}
                </small>
              </span>
              <span className={`status ${f.status}`} title={f.status}>
                {{
                  experimental: "Экспериментальный",
                  invalid: "Некорректная формула",
                  unresolved: "Требует уточнения",
                  verified: "Проверен",
                }[f.status] || f.status}
              </span>
              <ChevronDown />
            </summary>
            <div>
              <p>
                <strong>Подстановка:</strong> {f.substitution || "недоступна"}
              </p>
              <p>
                <strong>Результат:</strong> {format(f.result)} {f.unit || ""}
              </p>
              <p>
                <strong>Причина:</strong> {f.reason || "не указано"}
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Вход</th>
                    <th>Значение</th>
                    <th>Время</th>
                    <th>Флаги</th>
                  </tr>
                </thead>
                <tbody>
                  {f.inputs.map((i) => (
                    <tr key={i.tag}>
                      <td>{i.tag}</td>
                      <td>{format(i.value)}</td>
                      <td>{i.timestamp?.replace("T", " ") || "—"}</td>
                      <td>{i.flags.join(", ") || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        ))}
      </div>
    </section>
  );
}
export function DataQuality({
  quality,
  datasetId,
  metrics = [],
  formulas = [],
  mode = "period",
  onOpenMoment,
  onSettingsSaved,
}: {
  quality: Quality | null;
  datasetId?: string;
  metrics?: Metric[];
  formulas?: Formula[];
  mode?: "period" | "moment";
  onOpenMoment?: () => void;
  onSettingsSaved?: () => void;
}) {
  const metricCount = quality?.metrics.length ?? 0;
  const coveredCount =
    quality?.metrics.filter((metric) => metric.count > 0).length ?? 0;
  const invalidCount =
    quality?.metrics.reduce((sum, metric) => sum + metric.invalid_count, 0) ??
    0;
  const suspectCount =
    quality?.metrics.reduce((sum, metric) => sum + metric.suspect_count, 0) ??
    0;
  const signalsToCheck =
    quality?.metrics.filter(
      (metric) => metric.invalid_count > 0 || metric.suspect_count > 0,
    ).length ?? 0;
  const issueGroups = Array.from(
    (quality?.issues ?? [])
      .reduce(
        (groups, issue) => {
          const group = groups.get(issue.code) ?? {
            code: issue.code,
            message: issue.message,
            count: 0,
            items: [] as Issue[],
          };
          group.count += issue.count;
          group.items.push(issue);
          groups.set(issue.code, group);
          return groups;
        },
        new Map<
          string,
          {
            code: string;
            message: string;
            count: number;
            items: Issue[];
          }
        >(),
      )
      .values(),
  ).sort((a, b) => b.count - a.count);
  const issueNames: Record<string, string> = {
    flatline: "Возможные зависания сигналов",
    duplicate: "Повторяющиеся записи",
    conflict: "Конфликтующие значения",
    suspect: "Значения вне контрольного диапазона",
    invalid: "Некорректные значения",
    invalid_timestamp: "Некорректные временные метки",
  };
  return (
    <>
      <section className="panel diagnostic-overview">
        <header>
          <h2>Диагностика набора данных</h2>
        </header>
        <div className="diagnostic-stats">
          <article>
            <small>Пригодность данных</small>
            <strong>
              {coveredCount} <i>из {metricCount}</i>
            </strong>
            <span>Метрик содержат наблюдения</span>
          </article>
          <article className={invalidCount ? "danger" : "ok"}>
            <small>Некорректных</small>
            <strong>{format(invalidCount, 0)}</strong>
            <span>Не участвуют в статистике</span>
          </article>
          <article className={signalsToCheck ? "warning" : "ok"}>
            <small>
              Сигналов требуют проверки{" "}
              <HelpTooltip label="Сигналы требуют проверки">
                Есть некорректные или подозрительные отметки. Подозрение не
                доказывает неисправность прибора.
              </HelpTooltip>
            </small>
            <strong>{format(signalsToCheck, 0)}</strong>
            <span>{format(suspectCount, 0)} подозрительных отметок</span>
          </article>
        </div>
      </section>

      <details className="panel diagnostic-section">
        <summary>
          <span>
            <b>Выявленные проблемы</b>
            <small>
              {issueGroups.length
                ? `${issueGroups.length} групп · весь набор`
                : "Зарегистрированных проблем нет"}
            </small>
          </span>
          <ChevronDown />
        </summary>
        {issueGroups.length ? (
          <div className="issue-groups">
            {issueGroups.map((group) => (
              <details key={group.code}>
                <summary>
                  <AlertTriangle />
                  <span>
                    <b>{issueNames[group.code] || group.message}</b>
                    <small>{group.message}</small>
                  </span>
                  <strong>{format(group.count, 0)}</strong>
                  <ChevronDown />
                </summary>
                <ul>
                  {group.items.map((issue, index) => (
                    <li key={`${issue.metric_id || issue.code}-${index}`}>
                      <span>{issue.metric_id || "Источник целиком"}</span>
                      <b>{format(issue.count, 0)}</b>
                    </li>
                  ))}
                </ul>
              </details>
            ))}
          </div>
        ) : (
          <p className="empty-line">
            <ShieldCheck /> Зарегистрированных проблем нет. Это не означает
            прохождение всех возможных проверок.
          </p>
        )}
        <details className="assumptions-detail">
          <summary>Особенности расчёта</summary>
          {quality?.assumptions.map((assumption, index) => (
            <div className="assumption" key={index}>
              <Info /> {assumption}
            </div>
          ))}
        </details>
      </details>

      <details className="panel diagnostic-section">
        <summary>
          <span>
            <b>Источники данных</b>
            <small>Файлы и количество измерений</small>
          </span>
          <ChevronDown />
        </summary>
        <div className="quality-grid">
          {quality?.sources.map((source) => (
            <article key={`${source.kind}-${source.filename}`}>
              <Database />
              <h3>{source.kind.toUpperCase()}</h3>
              <p>{source.filename}</p>
              <dl>
                <div>
                  <dt>Наблюдений</dt>
                  <dd>{format(source.rows, 0)}</dd>
                </div>
                <div>
                  <dt>Фреймов</dt>
                  <dd>
                    {format(
                      typeof source.frame_count === "number"
                        ? source.frame_count
                        : null,
                      0,
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Метрик</dt>
                  <dd>{source.metrics}</dd>
                </div>
                <div>
                  <dt>Некорректных</dt>
                  <dd>{source.invalid_count}</dd>
                </div>
                <div>
                  <dt>Подозрительных</dt>
                  <dd>{source.suspect_count}</dd>
                </div>
              </dl>
            </article>
          ))}
        </div>
      </details>

      <details className="panel diagnostic-section">
        <summary>
          <span>
            <b>Качество сигналов</b>
          </span>
          <ChevronDown />
        </summary>
        {quality ? (
          <QualityRanking quality={quality} metrics={metrics} />
        ) : null}
        <details className="quality-detail">
          <summary>Покрытие и качество по всем показателям</summary>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Показатель</th>
                  <th>Измерений</th>
                  <th>Некорректных</th>
                  <th>Подозрительных</th>
                  <th>Зависших</th>
                  <th>Начало / конец</th>
                </tr>
              </thead>
              <tbody>
                {quality?.metrics.map((m) => (
                  <tr key={m.metric_id}>
                    <td>{m.metric_id}</td>
                    <td>{format(m.count, 0)}</td>
                    <td>{format(m.invalid_count, 0)}</td>
                    <td>{format(m.suspect_count, 0)}</td>
                    <td>{format(m.flatline_count, 0)}</td>
                    <td>
                      {stamp(m.start)}
                      <small>{stamp(m.end)}</small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      </details>

      <details className="panel diagnostic-section">
        <summary>
          <span>
            <b>Расчёты ВАК</b>
            <small>
              {mode === "moment"
                ? "Диагностические формулы для выбранного момента"
                : "Доступны для конкретного момента"}
            </small>
          </span>
          <ChevronDown />
        </summary>
        {mode === "moment" ? (
          <Formulas formulas={formulas} />
        ) : (
          <div className="diagnostic-prompt">
            <p>Выберите конкретный момент, чтобы подставить значения КИП.</p>
            <button className="secondary" onClick={onOpenMoment}>
              Открыть момент
            </button>
          </div>
        )}
      </details>

      {datasetId ? (
        <details className="panel diagnostic-section">
          <summary>
            <span>
              <b>Настройки диагностики</b>
              <small>Когда считать данные устаревшими</small>
            </span>
            <ChevronDown />
          </summary>
          <FreshnessSettings datasetId={datasetId} onSaved={onSettingsSaved} />
        </details>
      ) : null}
    </>
  );
}
function FreshnessSettings({
  datasetId,
  onSaved,
}: {
  datasetId: string;
  onSaved?: () => void;
}) {
  const [values, setValues] = useState({ kip: 10, pak: 30, lims: 2880 }),
    [message, setMessage] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    api
      .settings(datasetId, controller.signal)
      .then((s) => setValues(s.freshness_minutes))
      .catch(() => {
        if (!controller.signal.aborted)
          setMessage("Не удалось загрузить настройки");
      });
    return () => controller.abort();
  }, [datasetId]);
  async function save() {
    setMessage("");
    try {
      await api.saveSettings(datasetId, { freshness_minutes: values });
      setMessage("Настройки сохранены");
      onSaved?.();
    } catch {
      setMessage("Не удалось сохранить настройки");
    }
  }
  return (
    <section className="panel">
      <h2>Срок актуальности данных</h2>
      <p className="lead">
        Экспериментальные пороги давности, не производственный регламент.
      </p>
      <div className="filters">
        {(["kip", "pak", "lims"] as const).map((source) => (
          <label className="field" key={source}>
            <span>{source.toUpperCase()}, минут</span>
            <input
              type="number"
              min="1"
              value={values[source]}
              onChange={(e) =>
                setValues((current) => ({
                  ...current,
                  [source]: Math.max(1, Number(e.target.value) || 1),
                }))
              }
            />
          </label>
        ))}
      </div>
      <button className="primary" onClick={save}>
        Сохранить
      </button>
      {message ? <p>{message}</p> : null}
    </section>
  );
}
