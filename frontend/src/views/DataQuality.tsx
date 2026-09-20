import { useEffect, useState } from "react";
import { AlertTriangle, Database, Info, ShieldCheck } from "lucide-react";
import { QualityRanking } from "../components/MonitoringCharts";
import { HelpTooltip } from "../components/HelpTooltip";
import { api } from "../api";
import type { Formula, Issue, Metric, Quality } from "../types";
import { format, stamp } from "./shared";
import { Disclosure } from "../ui/Controls";
import { NumberField } from "../ui/NumberField";

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
          <Disclosure
            key={f.id}
            className="formula"
            summary={
              <span className="formula-title">
                {f.label}
                <span className={`status ${f.status}`}>
                  {{
                    experimental: "Экспериментальный",
                    invalid: "Некорректная формула",
                    unresolved: "Требует уточнения",
                    verified: "Проверен",
                  }[f.status] || f.status}
                </span>
              </span>
            }
            meta={`${f.expression} · версия ${f.version}`}
          >
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
                      <td>{i.timestamp ? stamp(i.timestamp) : "—"}</td>
                      <td>{i.flags.join(", ") || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Disclosure>
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

      <section className="panel diagnostic-block" id="diag-issues">
        <header className="diagnostic-block-head">
          <h2>Выявленные проблемы</h2>
          <p>
            {issueGroups.length
              ? `${issueGroups.length} групп · весь набор`
              : "Зарегистрированных проблем нет"}
          </p>
        </header>
        {issueGroups.length ? (
          <div className="issue-groups">
            {issueGroups.map((group) => (
              <Disclosure
                key={group.code}
                icon={<AlertTriangle className="warn-icon" />}
                summary={
                  <span className="issue-title">
                    {issueNames[group.code] || group.message}
                    <strong className="num">{format(group.count, 0)}</strong>
                  </span>
                }
                meta={group.message}
              >
                <ul>
                  {group.items.map((issue, index) => (
                    <li key={`${issue.metric_id || issue.code}-${index}`}>
                      <span>{issue.metric_id || "Источник целиком"}</span>
                      <b>{format(issue.count, 0)}</b>
                    </li>
                  ))}
                </ul>
              </Disclosure>
            ))}
          </div>
        ) : (
          <p className="empty-line">
            <ShieldCheck /> Зарегистрированных проблем нет. Это не означает
            прохождение всех возможных проверок.
          </p>
        )}
        <Disclosure
          className="assumptions-detail inline"
          summary="Особенности расчёта"
        >
          {quality?.assumptions.map((assumption, index) => (
            <div className="assumption" key={index}>
              <Info /> {assumption}
            </div>
          ))}
        </Disclosure>
      </section>

      <section className="panel diagnostic-block" id="diag-sources">
        <header className="diagnostic-block-head">
          <h2>Источники данных</h2>
          <p>Файлы и количество измерений</p>
        </header>
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
      </section>

      <section className="panel diagnostic-block" id="diag-signals">
        <header className="diagnostic-block-head">
          <h2>Качество сигналов</h2>
          <p>Ранжирование по доле подозрительных отметок</p>
        </header>
        {quality ? (
          <QualityRanking quality={quality} metrics={metrics} />
        ) : null}
        <Disclosure
          className="quality-detail"
          summary="Покрытие и качество по всем показателям"
        >
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
        </Disclosure>
      </section>

      <section className="panel diagnostic-block" id="diag-vak">
        <header className="diagnostic-block-head">
          <h2>Расчёты ВАК</h2>
          <p>
            {mode === "moment"
              ? "Диагностические формулы для выбранного момента"
              : "Доступны для конкретного момента"}
          </p>
        </header>
        {mode === "moment" ? (
          <Formulas formulas={formulas} />
        ) : (
          <div className="diagnostic-prompt">
            <p>Выберите конкретный момент, чтобы подставить значения КИП.</p>
            <button type="button" className="secondary" onClick={onOpenMoment}>
              Открыть момент
            </button>
          </div>
        )}
      </section>

      {datasetId ? (
        <section className="panel diagnostic-block" id="diag-settings">
          <header className="diagnostic-block-head">
            <h2>Настройки диагностики</h2>
            <p>Когда считать данные устаревшими</p>
          </header>
          <FreshnessSettings datasetId={datasetId} onSaved={onSettingsSaved} />
        </section>
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
    <div className="freshness-settings">
      <p className="lead">
        Экспериментальные пороги давности, не производственный регламент.
      </p>
      <div className="freshness-fields">
        {(["kip", "pak", "lims"] as const).map((source) => (
          <NumberField
            key={source}
            label={source.toUpperCase()}
            unit="мин"
            min={1}
            step={source === "lims" ? 60 : 5}
            decimals={0}
            value={values[source]}
            onChange={(value) =>
              setValues((current) => ({
                ...current,
                [source]: Math.max(1, value ?? 1),
              }))
            }
          />
        ))}
      </div>
      <div className="freshness-actions">
        <button type="button" className="primary" onClick={save}>
          Сохранить
        </button>
        {message ? <span role="status">{message}</span> : null}
      </div>
    </div>
  );
}
