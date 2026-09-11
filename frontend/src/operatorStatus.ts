import type { Metric, Snapshot, Summary } from "./types";

export type OperatorLevel = "danger" | "warning" | "unknown" | "ok";
export type AttentionTarget = "analysis" | "process" | "diagnostics";

export interface OperatorFinding {
  code: string;
  level: Exclude<OperatorLevel, "ok">;
  title: string;
  description: string;
  action: string;
  target: AttentionTarget;
  metricId?: string;
}

export interface OperatorAssessment {
  level: OperatorLevel;
  title: string;
  description: string;
  source: string;
  findings: OperatorFinding[];
}

const SULFUR = ["lims.ht.2.Mg.Sulfur", "pak.ht.Mg.Sulfur"];
const priority: Record<OperatorLevel, number> = {
  danger: 0,
  warning: 1,
  unknown: 2,
  ok: 3,
};

const hours = (minutes: number) =>
  new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(
    minutes / 60,
  );

export function buildOperatorAssessment({
  mode,
  snapshot,
  summary,
  metrics,
  pinned,
}: {
  mode: "period" | "moment";
  snapshot: Snapshot | null;
  summary: Summary | null;
  metrics: Metric[];
  pinned: string[];
}): OperatorAssessment {
  const findings: OperatorFinding[] = [];
  const metricById = new Map(metrics.map((metric) => [metric.id, metric]));

  if (mode === "moment") {
    const values = SULFUR.map((id) =>
      snapshot?.values.find((value) => value.metric_id === id),
    );
    const present = values.filter((value) => value?.value != null);
    const reliable = present.filter(
      (value) => value?.freshness === "fresh" && !value.flags.length,
    );
    const exceeded = reliable.filter((value) => (value?.value ?? 0) > 10);

    if (exceeded.length) {
      findings.push({
        code: "sulfur-exceeded",
        level: "danger",
        title: "Зафиксировано превышение серы",
        description: `${exceeded.map((value) => (value?.metric_id.startsWith("lims") ? "ЛИМС" : "ПАК")).join(" и ")} показывает выше 10 мг/кг.`,
        action: "Открыть график",
        target: "analysis",
        metricId: exceeded[0]?.metric_id,
      });
    }

    const unreliable = present.filter(
      (value) => value?.freshness !== "fresh" || value.flags.length,
    );
    if (unreliable.length) {
      findings.push({
        code: "sulfur-unreliable",
        level: "warning",
        title: "Часть данных серы требует проверки",
        description: `${unreliable.map((value) => (value?.metric_id.startsWith("lims") ? "ЛИМС" : "ПАК")).join(" и ")}: устаревшее или подозрительное измерение.`,
        action: "Проверить историю",
        target: "analysis",
        metricId: unreliable[0]?.metric_id,
      });
    }
    if (!present.length) {
      findings.push({
        code: "sulfur-missing",
        level: "unknown",
        title: "Нет измерений серы к выбранному моменту",
        description: "ЛИМС и ПАК не дают значения, пригодного для вывода.",
        action: "Проверить данные",
        target: "diagnostics",
      });
    } else if (!reliable.length && !unreliable.length) {
      findings.push({
        code: "sulfur-insufficient",
        level: "unknown",
        title: "Недостаточно достоверных данных",
        description: "Нет свежего измерения серы без диагностических флагов.",
        action: "Проверить данные",
        target: "diagnostics",
      });
    }

    const valuesById = new Map(
      snapshot?.values.map((value) => [value.metric_id, value]),
    );
    for (const id of pinned) {
      const metric = metricById.get(id);
      const value = valuesById.get(id);
      if (metric?.source !== "kip" || !value?.flags.length) continue;
      findings.push({
        code: `kip-${id}`,
        level: "warning",
        title: `${metric.label} требует проверки`,
        description: value.flags.includes("flatline")
          ? "Возможное длительно неизменное показание КИП."
          : "Измерение КИП отмечено диагностическим правилом.",
        action: "Открыть сигнал",
        target: "analysis",
        metricId: id,
      });
    }
  } else if (summary) {
    const sulfur = summary.sulfur;
    if (sulfur.lab_exceed_count > 0 || sulfur.pak_exceed_minutes > 0) {
      const sources = [
        sulfur.lab_exceed_count > 0
          ? `ЛИМС: ${sulfur.lab_exceed_count} проб выше порога`
          : "",
        sulfur.pak_exceed_minutes > 0
          ? `ПАК: ${hours(sulfur.pak_exceed_minutes)} ч выше порога`
          : "",
      ].filter(Boolean);
      findings.push({
        code: "sulfur-exceeded",
        level: "danger",
        title: "В периоде есть превышение серы",
        description: sources.join(" · "),
        action: "Разобрать тренд",
        target: "analysis",
        metricId: "pak.ht.Mg.Sulfur",
      });
    }

    const hasSulfur = sulfur.lab_count > 0 || sulfur.pak_observed_minutes > 0;
    if (!hasSulfur) {
      findings.push({
        code: "sulfur-missing",
        level: "unknown",
        title: "Недостаточно данных о сере",
        description: "В выбранном периоде нет пригодных наблюдений ЛИМС и ПАК.",
        action: "Проверить данные",
        target: "diagnostics",
      });
    } else if (
      sulfur.pak_coverage_fraction == null ||
      sulfur.pak_coverage_fraction < 0.9999
    ) {
      findings.push({
        code: "pak-incomplete",
        level: "warning",
        title: "Покрытие ПАК неполное",
        description: `Достоверными наблюдениями покрыто ${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format((sulfur.pak_coverage_fraction ?? 0) * 100)}% периода.`,
        action: "Проверить покрытие",
        target: "diagnostics",
      });
    }
    if (sulfur.pak_suspect_minutes > 0) {
      findings.push({
        code: "pak-suspect",
        level: "warning",
        title: "Есть подозрительные интервалы ПАК",
        description: `${hours(sulfur.pak_suspect_minutes)} ч не используются как достоверное покрытие.`,
        action: "Открыть диагностику",
        target: "diagnostics",
      });
    }

    const stats = new Map(
      summary.metrics.map((stat) => [stat.metric_id, stat]),
    );
    for (const id of pinned) {
      const metric = metricById.get(id);
      const stat = stats.get(id);
      if (metric?.source !== "kip" || !stat?.suspect_count) continue;
      findings.push({
        code: `kip-${id}`,
        level: "warning",
        title: `${metric.label} требует проверки`,
        description: `Подозрительных измерений за период: ${stat.suspect_count}.`,
        action: "Открыть сигнал",
        target: "analysis",
        metricId: id,
      });
    }
  } else {
    findings.push({
      code: "summary-missing",
      level: "unknown",
      title: "Недостаточно данных для вывода",
      description: "Сводка выбранного интервала недоступна.",
      action: "Проверить данные",
      target: "diagnostics",
    });
  }

  findings.sort((a, b) => priority[a.level] - priority[b.level]);
  const level = findings[0]?.level ?? "ok";
  const copy: Record<OperatorLevel, [string, string, string]> = {
    danger: [
      "Требуется внимание оператора",
      findings[0]?.description ?? "Зафиксировано отклонение.",
      "ЛИМС / ПАК",
    ],
    warning: [
      "Данные требуют проверки",
      findings[0]?.description ?? "Достоверность данных требует проверки.",
      "Контроль достоверности",
    ],
    unknown: [
      "Недостаточно данных для вывода",
      findings[0]?.description ?? "Нет данных, пригодных для вывода.",
      "Доступность измерений",
    ],
    ok: [
      "Известных превышений не обнаружено",
      "Доступные достоверные измерения серы не превышают 10 мг/кг.",
      "ЛИМС / ПАК",
    ],
  };
  return {
    level,
    title: copy[level][0],
    description: copy[level][1],
    source: copy[level][2],
    findings,
  };
}
