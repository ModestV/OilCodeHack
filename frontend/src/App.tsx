import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bell,
  BarChart3,
  Beaker,
  Database,
  Filter,
  FlaskConical,
  Gauge,
  Lightbulb,
  Menu,
  Upload,
  X,
} from "lucide-react";
import { api, type Distillation } from "./api";
import type {
  Distribution,
  Formula,
  Manifest,
  Metric,
  Quality,
  SeriesResponse,
  Snapshot,
  Summary,
} from "./types";
import { DataMenu } from "./components/DataMenu";
import { OperatorPanel, type SidePanelMode } from "./components/OperatorPanel";
import { TimeControls, offset, type Mode } from "./components/TimeControls";
import { UploadModal } from "./components/UploadModal";
import { Chart } from "./components/Chart";
import {
  DataQuality,
  ExportButton,
  Kip,
  Overview,
  Statistics,
  Trends,
} from "./views/MonitoringViews";
import {
  buildOperatorAssessment,
  type AttentionTarget,
} from "./operatorStatus";

type Tab = "overview" | "trends" | "kip" | "quality";
type Page = "monitoring" | "recommendations" | "sandbox";
const tabs: [Tab, string][] = [
  ["overview", "Сводка"],
  ["trends", "Анализ"],
  ["kip", "Процесс и КИП"],
  ["quality", "Диагностика"],
];
const MAIN = [
  "Mg.Sulfur",
  "D15",
  "FlashPoint",
  "CFPP",
  "CloudPoint",
  "95%.T",
].map((x) => "lims.ht.2." + x);
const SULFUR = ["pak.ht.Mg.Sulfur", "lims.ht.2.Mg.Sulfur"];
const STORE = "oilcode:monitoring:v2";
function preferences() {
  try {
    const p = JSON.parse(localStorage.getItem(STORE) || "{}");
    return {
      pinned:
        Array.isArray(p.pinned) &&
        p.pinned.every((x: unknown) => typeof x === "string")
          ? (p.pinned as string[]).slice(0, 6)
          : MAIN,
      statistic: ["median", "mean", "min", "max", "p05", "p95", "std"].includes(
        p.statistic,
      )
        ? (p.statistic as string)
        : "median",
    };
  } catch {
    return { pinned: MAIN, statistic: "median" };
  }
}
export function App() {
  const [datasets, setDatasets] = useState<Manifest[]>([]),
    [datasetId, setDatasetId] = useState(""),
    [manifest, setManifest] = useState<Manifest | null>(null),
    [metrics, setMetrics] = useState<Metric[]>([]);
  const [page, setPage] = useState<Page>("monitoring"),
    [tab, setTab] = useState<Tab>("overview"),
    [mode, setModeState] = useState<Mode>("period"),
    [from, setFrom] = useState(""),
    [to, setTo] = useState("");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null),
    [summary, setSummary] = useState<Summary | null>(null),
    [series, setSeries] = useState<SeriesResponse | null>(null),
    [formulas, setFormulas] = useState<Formula[]>([]),
    [quality, setQuality] = useState<Quality | null>(null),
    [distribution, setDistribution] = useState<Distribution | null>(null),
    [distillation, setDistillation] = useState<Distillation | null>(null);
  const [selected, setSelected] = useState<string[]>([]),
    [statMetric, setStatMetric] = useState(""),
    [pinned, setPinned] = useState<string[]>(() => preferences().pinned),
    [cardStatistic, setCardStatistic] = useState(() => preferences().statistic);
  const [exclude, setExclude] = useState(false),
    [upload, setUpload] = useState(false),
    [sidePanel, setSidePanel] = useState<SidePanelMode>("closed"),
    [mobile, setMobile] = useState(false),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true),
    [statsView, setStatsView] = useState(false);
  const [completedKey, setCompletedKey] = useState("");
  const requestKey = JSON.stringify([
    datasetId,
    from,
    to,
    mode,
    tab,
    page,
    selected,
    statsView,
    statMetric,
    exclude,
  ]);
  useEffect(() => {
    try {
      localStorage.setItem(
        STORE,
        JSON.stringify({ version: 2, pinned, statistic: cardStatistic }),
      );
    } catch {
      /* Browser storage may be unavailable. */
    }
  }, [pinned, cardStatistic]);
  useEffect(() => {
    const c = new AbortController();
    api
      .datasets(c.signal)
      .then((r) => {
        setDatasets(r.datasets);
        setDatasetId(r.default_id || r.datasets[0]?.id || "");
        setLoading(false);
      })
      .catch((e) => {
        if (!c.signal.aborted) {
          setError(e.message);
          setLoading(false);
        }
      });
    return () => c.abort();
  }, []);
  useEffect(() => {
    if (!datasetId) return;
    const c = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setManifest(null);
    setMetrics([]);
    setFrom("");
    setTo("");
    setSnapshot(null);
    setSummary(null);
    setSeries(null);
    setError("");
    setLoading(true);
    async function load() {
      try {
        const m = await api.dataset(datasetId, c.signal);
        if (c.signal.aborted) return;
        setManifest(m);
        setDatasets((v) => v.map((x) => (x.id === m.id ? m : x)));
        if (m.status === "importing") {
          setLoading(false);
          timer = setTimeout(load, 1500);
          return;
        }
        if (m.status === "ready") {
          const mm = await api.metrics(datasetId, c.signal);
          if (c.signal.aborted) return;
          const available = mm.metrics.filter((x) => x.available !== false);
          setMetrics(mm.metrics);
          const end = m.telemetry_end || m.end;
          const start = m.telemetry_start || m.start;
          if (end && start) {
            setTo(end);
            const day = offset(end, -1440);
            setFrom(day < start ? start : day);
            if (start === end) setFrom(offset(start, -10));
          }
          const defaults = SULFUR.filter((id) =>
            available.some((m) => m.id === id),
          );
          setSelected(
            defaults.length ? defaults : available.slice(0, 2).map((x) => x.id),
          );
          setStatMetric(
            available.find((x) => x.id === MAIN[0])?.id ||
              available[0]?.id ||
              "",
          );
        }
        setLoading(false);
      } catch (e) {
        if (!c.signal.aborted) {
          setError(e instanceof Error ? e.message : "Ошибка загрузки");
          setLoading(false);
        }
      }
    }
    void load();
    return () => {
      c.abort();
      clearTimeout(timer);
    };
  }, [datasetId]);
  useEffect(() => {
    if (
      manifest?.id !== datasetId ||
      manifest.status !== "ready" ||
      !metrics.length ||
      page !== "monitoring"
    )
      return;
    const c = new AbortController();
    setError("");
    setSnapshot(null);
    setSummary(null);
    setSeries(null);
    setDistribution(null);
    setDistillation(null);
    setFormulas([]);
    setQuality(null);
    if (!from || !to || (mode === "period" && from >= to)) {
      setError(
        !from || !to
          ? "Укажите дату и время."
          : "Конец периода должен быть позже начала.",
      );
      setCompletedKey(requestKey);
      setLoading(false);
      return;
    }
    setLoading(true);
    async function load() {
      try {
        const tasks: Promise<void>[] = [];
        if (
          mode === "moment" &&
          (tab === "overview" || tab === "kip" || tab === "quality")
        )
          tasks.push(
            api.snapshot(datasetId, to, c.signal).then((v) => {
              if (!c.signal.aborted) setSnapshot(v);
            }),
          );
        if (mode === "period" && ["overview", "trends", "kip"].includes(tab))
          tasks.push(
            api
              .summary(
                datasetId,
                from,
                to,
                c.signal,
                tab === "overview" ? false : exclude,
              )
              .then((v) => {
                if (!c.signal.aborted) setSummary(v);
              }),
          );
        if (tab === "overview" || tab === "trends") {
          const ids = (tab === "overview" ? SULFUR : selected).filter((id) =>
            metrics.some((m) => m.id === id),
          );
          tasks.push(
            api
              .series(
                datasetId,
                ids,
                mode === "moment" ? offset(to, -1440) : from,
                mode === "moment" ? offset(to, 1 / 60) : to,
                c.signal,
                tab === "overview" ? false : exclude,
              )
              .then((v) => {
                if (!c.signal.aborted) setSeries(v);
              }),
          );
        }
        if (tab === "overview" && mode === "moment")
          tasks.push(
            api.distillation(datasetId, to, c.signal).then((v) => {
              if (!c.signal.aborted) setDistillation(v);
            }),
          );
        if (tab === "quality" && mode === "moment")
          tasks.push(
            api.formulas(datasetId, to, c.signal).then((v) => {
              if (!c.signal.aborted) setFormulas(v.formulas);
            }),
          );
        if (tab === "quality")
          tasks.push(
            api.quality(datasetId, c.signal).then((v) => {
              if (!c.signal.aborted) setQuality(v);
            }),
          );
        if (tab === "trends" && statsView && mode === "period" && statMetric)
          tasks.push(
            api
              .distribution(datasetId, statMetric, from, to, c.signal, exclude)
              .then((v) => {
                if (!c.signal.aborted) setDistribution(v);
              }),
          );
        await Promise.all(tasks);
      } catch (e) {
        if (!c.signal.aborted)
          setError(e instanceof Error ? e.message : "Ошибка расчёта");
      } finally {
        if (!c.signal.aborted) {
          setCompletedKey(requestKey);
          setLoading(false);
        }
      }
    }
    void load();
    return () => c.abort();
  }, [
    manifest?.id,
    manifest?.status,
    datasetId,
    metrics,
    from,
    to,
    mode,
    tab,
    page,
    selected,
    statsView,
    statMetric,
    exclude,
    requestKey,
  ]);
  const allRange = useMemo<[string, string]>(
    () => [
      manifest?.start || "",
      manifest?.end ? offset(manifest.end, 1 / 60) : "",
    ],
    [manifest],
  );
  const setMode = (m: Mode) => {
    setModeState(m);
    setStatsView(false);
  };
  const openMoment = useCallback((time: string) => {
    const value = /^\d{4}-/.test(time)
      ? time.replace(/Z$/, "")
      : new Date(Number(time)).toISOString().slice(0, 19);
    setTo(value);
    setModeState("moment");
    setTab("overview");
  }, []);
  function range(f: string, t: string) {
    setFrom(f);
    setTo(t);
  }
  function navigate(p: Page) {
    setPage(p);
    setMobile(false);
    setSidePanel("closed");
  }
  const assessment = buildOperatorAssessment({
    mode,
    snapshot,
    summary,
    metrics,
    pinned,
  });
  const activeFilterCount =
    Number(exclude) +
    Number(cardStatistic !== "median") +
    Number(
      selected.join("|") !==
        SULFUR.filter((id) => metrics.some((metric) => metric.id === id)).join(
          "|",
        ),
    );
  const navigateFromFinding = (target: AttentionTarget, metricId?: string) => {
    setSidePanel("closed");
    if (target === "analysis") {
      if (metricId) setSelected([metricId]);
      setStatsView(false);
      setTab("trends");
    } else if (target === "process") {
      setTab("kip");
    } else {
      setTab("quality");
    }
  };
  const view = () => {
    if (!manifest) return null;
    if (tab === "overview")
      return (
        <>
          <Overview
            {...{
              mode,
              manifest,
              metrics,
              snapshot,
              summary,
              series,
              pinned,
            }}
            onSelectTime={openMoment}
            toolbar={
              <div className="overview-tools" aria-label="Инструменты сводки">
                <button
                  className="secondary"
                  onClick={() => setSidePanel("metrics")}
                >
                  <Gauge /> Показатели
                </button>
                <button
                  className="secondary"
                  onClick={() => setSidePanel("filters")}
                >
                  <Filter /> Фильтры
                  {activeFilterCount > 0 && (
                    <span className="control-badge">{activeFilterCount}</span>
                  )}
                </button>
                {assessment.findings.length > 2 && (
                  <button
                    className="secondary"
                    onClick={() => setSidePanel("warnings")}
                  >
                    <Bell /> Все предупреждения
                    <span className="control-badge">
                      {assessment.findings.length}
                    </span>
                  </button>
                )}
              </div>
            }
            onNavigate={navigateFromFinding}
          />
          {mode === "moment" && distillation && (
            <DistillationView data={distillation} />
          )}
        </>
      );
    if (tab === "trends")
      return statsView ? (
        <Statistics
          metrics={metrics}
          summary={summary}
          distribution={distribution}
          selected={statMetric}
          setSelected={setStatMetric}
        />
      ) : (
        <Trends
          mode={mode}
          metrics={metrics}
          series={series}
          onSelectTime={openMoment}
        />
      );
    if (tab === "kip")
      return (
        <Kip
          {...{ metrics, snapshot, mode, summary }}
          selected={[...pinned, ...selected]}
          onTrend={(id) => {
            setSelected([id]);
            setStatsView(false);
            setTab("trends");
          }}
        />
      );
    return (
      <DataQuality
        quality={quality}
        datasetId={datasetId}
        metrics={metrics}
        formulas={formulas}
        mode={mode}
        onOpenMoment={() => setMode("moment")}
      />
    );
  };
  return (
    <div className="app">
      {mobile && (
        <div className="nav-backdrop" onClick={() => setMobile(false)} />
      )}
      <aside className={mobile ? "open" : ""}>
        <button
          className="close-menu"
          aria-label="Закрыть меню"
          onClick={() => setMobile(false)}
        >
          <X />
        </button>
        <div className="brand">
          <span>
            <FlaskConical />
          </span>
          <b>Нефтекод</b>
        </div>
        <nav aria-label="Основные разделы">
          <button
            className={page === "monitoring" ? "active" : ""}
            onClick={() => navigate("monitoring")}
          >
            <BarChart3 /> <span>Мониторинг</span>
          </button>
        </nav>
        <div className="nav-development">
          <small>В разработке</small>
          <button
            className={page === "recommendations" ? "active" : ""}
            onClick={() => navigate("recommendations")}
          >
            <Lightbulb /> <span>Рекомендации</span>
          </button>
          <button
            className={page === "sandbox" ? "active" : ""}
            onClick={() => navigate("sandbox")}
          >
            <Beaker /> <span>Песочница</span>
          </button>
        </div>
        <div className="history">
          <Database />
          <span>Исторические данные</span>
        </div>
      </aside>
      <main>
        <header className="top">
          <button
            className="menu"
            aria-label="Открыть меню"
            onClick={() => setMobile(true)}
          >
            <Menu />
          </button>
          <div>
            <h1>
              {page === "monitoring"
                ? "Мониторинг качества"
                : page === "recommendations"
                  ? "Рекомендации"
                  : "Песочница"}
            </h1>
            <p>Дизель после гидроочистки · Точка отбора 2</p>
          </div>
          {page === "monitoring" && datasets.length > 0 && (
            <DataMenu
              datasets={datasets}
              datasetId={datasetId}
              setDatasetId={setDatasetId}
              onUpload={() => setUpload(true)}
            />
          )}
        </header>
        {error && (
          <div className="banner error" role="alert">
            {error}
            <button
              className="icon-btn"
              aria-label="Скрыть ошибку"
              onClick={() => setError("")}
            >
              <X />
            </button>
          </div>
        )}
        {page !== "monitoring" ? (
          <section className="empty-state compact">
            {page === "recommendations" ? <Lightbulb /> : <Beaker />}
            <h2>В разработке</h2>
            <button
              className="secondary"
              onClick={() => navigate("monitoring")}
            >
              К мониторингу
            </button>
          </section>
        ) : (
          <>
            {manifest?.status === "ready" && from && to && (
              <TimeControls
                mode={mode}
                setMode={setMode}
                from={from}
                to={to}
                setRange={range}
                allRange={allRange}
                scenarios={datasetId === "hackathon"}
              />
            )}
            {manifest?.status === "ready" && (
              <>
                <div className="tabs" role="tablist">
                  {tabs.map(([id, label]) => (
                    <button
                      role="tab"
                      aria-selected={tab === id}
                      key={id}
                      className={tab === id ? "active" : ""}
                      onClick={() => {
                        setTab(id);
                        setStatsView(false);
                      }}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                {(tab === "trends" || (mode === "period" && tab === "kip")) && (
                  <div className="analysis-controls">
                    {tab === "trends" && (
                      <button
                        className="secondary"
                        onClick={() => setSidePanel("filters")}
                      >
                        <Filter /> Фильтры
                        {activeFilterCount > 0 && (
                          <span className="control-badge">
                            {activeFilterCount}
                          </span>
                        )}
                      </button>
                    )}
                    {tab === "kip" && <span />}
                    {tab === "trends" && (
                      <div className="view-switch">
                        <button
                          className={!statsView ? "active" : ""}
                          onClick={() => setStatsView(false)}
                        >
                          Графики
                        </button>
                        {mode === "period" && (
                          <button
                            className={statsView ? "active" : ""}
                            onClick={() => setStatsView(true)}
                          >
                            Статистика
                          </button>
                        )}
                        {selected.length > 0 && (
                          <ExportButton
                            id={datasetId}
                            ids={selected}
                            from={from}
                            to={to}
                            exclude={exclude}
                          />
                        )}
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
            {loading ||
            (manifest?.status === "ready" && completedKey !== requestKey) ? (
              <div className="loading-state" role="status">
                <div className="loading-line" />
                Расчёт показателей…
              </div>
            ) : manifest?.status === "importing" ? (
              <div className="banner" role="status">
                Обработка файлов…
              </div>
            ) : manifest?.status === "error" ? (
              <div className="banner error">
                Ошибка импорта: {manifest.error}
              </div>
            ) : manifest?.status === "ready" ? (
              view()
            ) : (
              <section className="empty-state">
                <Database />
                <h2>Нет загруженных данных</h2>
                <button className="primary" onClick={() => setUpload(true)}>
                  <Upload /> Загрузить данные
                </button>
              </section>
            )}
          </>
        )}
      </main>
      <UploadModal
        open={upload}
        onClose={() => setUpload(false)}
        onStarted={(m) => {
          setUpload(false);
          setDatasets((v) => [m, ...v.filter((d) => d.id !== m.id)]);
          setDatasetId(m.id);
          setPage("monitoring");
        }}
      />
      {sidePanel !== "closed" && (
        <OperatorPanel
          open={sidePanel}
          onClose={() => setSidePanel("closed")}
          metrics={metrics}
          pinned={pinned}
          setPinned={setPinned}
          mode={mode}
          snapshot={snapshot}
          summary={summary}
          statistic={cardStatistic}
          setStatistic={setCardStatistic}
          exclude={exclude}
          setExclude={setExclude}
          selected={selected}
          setSelected={setSelected}
          assessment={assessment}
          onNavigate={navigateFromFinding}
        />
      )}
    </div>
  );
}
function DistillationView({ data }: { data: Distillation }) {
  const option = useMemo(
    () => ({
      grid: { left: 55, right: 30, top: 35, bottom: 40 },
      tooltip: { trigger: "axis" },
      xAxis: { type: "value", name: "% об.", min: 0, max: 100 },
      yAxis: { type: "value", name: "°C", scale: true },
      series: [
        {
          type: "line",
          data: data.points.map((p) => [p.fraction, p.temperature]),
          symbolSize: 7,
          lineStyle: { color: "#0079c2" },
        },
      ],
    }),
    [data],
  );
  return (
    <details className="distillation">
      <summary>Фракционный состав одной пробы</summary>
      {data.timestamp ? (
        <p>
          Проба: {data.timestamp.replace("T", " ")} · возраст{" "}
          {Math.round(data.age_minutes || 0)} мин
        </p>
      ) : null}
      {data.points.length ? (
        <Chart option={option} />
      ) : (
        <p className="muted">
          {data.reason || "Нет совместных фракционных измерений к моменту"}
        </p>
      )}
      {data.points.length > 0 && data.reason && (
        <p className="warn">{data.reason}</p>
      )}
    </details>
  );
}
