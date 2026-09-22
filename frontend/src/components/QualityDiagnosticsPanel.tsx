import { useEffect, useState } from "react";
import { api } from "../api";
import type { QualityDiagnostics, QualityDiagnosticTarget, QualityObservation } from "../types";
import { formatNumber } from "../visualization";

const time = (value: string | null) => value ? value.replace("T", " ").slice(0, 16) : "нет данных";
const inputName = (metric: string) => ({
  "lims.ht.2.D15": "Плотность D15",
  "lims.ht.2.50%.T": "T50",
  "lims.ht.2.95%.T": "T95",
}[metric] || metric);

function Sample({ row }: { row: QualityObservation }) {
  return <p className="quality-sample">
    Отбор: {time(row.timestamp)} · доступен: {time(row.available_at)}
    <br />Возраст от отбора: {formatNumber(row.age_hours)} ч
    {row.flags.length > 0 && <> · отметки: {row.flags.join(", ")}</>}
  </p>;
}

function Target({ title, estimateTitle, target }: {
  title: string; estimateTitle: string; target: QualityDiagnosticTarget;
}) {
  const { measurement, estimate } = target;
  return <article className="quality-diagnostic-card">
    <h3>{title}</h3>
    <p className="quality-label">Последний опубликованный анализ</p>
    <p className="quality-value">{formatNumber(measurement.value)} <span>{measurement.unit}</span></p>
    <Sample row={measurement} />
    {target.reference_warning && <p className="quality-reasons">{target.reference_warning}</p>}
    {measurement.reasons.length > 0
      ? <ul className="quality-reasons">{measurement.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>
      : <p className="quality-sample">Анализ доступен по возрасту и отметкам; он описывает пробу, а не будущее качество.</p>}
    <div className="quality-estimate">
      <p className="quality-label">{estimateTitle}</p>
      <p className="quality-value">{estimate.status === "available" ? formatNumber(estimate.value) : "Недоступна"}
        {estimate.status === "available" && <span> {estimate.unit}</span>}
      </p>
      {estimate.reasons.length > 0 && <ul className="quality-reasons">{estimate.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>}
      <p className="quality-sample">Не подтверждает соблюдение спецификации.</p>
    </div>
    <details>
      <summary>Основания и ограничения оценки</summary>
      <ul>{estimate.limitations.map(line => <li key={line}>{line}</li>)}</ul>
      <h4>Входы расчёта ({estimate.inputs.length})</h4>
      {estimate.inputs.length === 0 && <p>Опубликованных входов нет.</p>}
      {estimate.inputs.map((row, i) => <div className="quality-input" key={`${row.metric_id}-${row.timestamp}-${i}`}>
        <strong>{inputName(row.metric_id)}: {formatNumber(row.value)} {row.unit}</strong>
        <Sample row={row} />
      </div>)}
    </details>
  </article>;
}

export function QualityDiagnosticsPanel({ datasetId, at }: { datasetId: string; at: string }) {
  const [state, setState] = useState<{ key: string; data?: QualityDiagnostics; error?: string } | null>(null);
  const key = `${datasetId}/${at}`;
  useEffect(() => {
    const controller = new AbortController();
    api.qualityDiagnostics(datasetId, at, controller.signal).then(
      data => { if (!controller.signal.aborted) setState({ key, data }); },
      error => { if (!controller.signal.aborted) setState({ key, error: error instanceof Error ? error.message : "Ошибка запроса" }); },
    );
    return () => controller.abort();
  }, [datasetId, at, key]);
  const current = state?.key === key ? state : null;
  return <section className="decision-band quality-diagnostics" aria-label="Диагностика T95 и цетана">
    <h2>Диагностика T95 и цетана</h2>
    <p className="quality-sample">На {time(at)} · время источника</p>
    {!current && <p role="status">Загружаем анализы и расчётные оценки…</p>}
    {current?.error && <p role="alert">Диагностика недоступна: {current.error}. Результат проверки ограничений приведён отдельно.</p>}
    {current?.data && <>
      <p>{current.data.scope}.</p>
      <p className="support-notice">Только диагностика. Расчётные оценки не заменяют лабораторный анализ, не снимают ограничения рекомендаций и не разрешают выпуск продукта. В историческом режиме это ретроспективный расчёт.</p>
      <p className="quality-sample">Задержка публикации ЛИМС принята равной {current.data.lims_publication_delay_hours} ч.
        Порог возраста измерений в настройках: {current.data.measurement_freshness_hours} ч;
        для новых входов расчётных оценок: {current.data.estimate_max_age_hours} ч.</p>
      <div className="quality-diagnostic-grid">
        <Target title="T95" estimateTitle="Условная оценка · медиана последних проб" target={current.data.t95} />
        <Target title="Цетановое число" estimateTitle="Расчётный цетановый индекс · D15 и T50" target={current.data.cetane} />
      </div>
    </>}
  </section>;
}
