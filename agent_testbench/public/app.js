const snapshotSelect = document.querySelector("#snapshotSelect");
const runButton = document.querySelector("#runButton");
const traceEl = document.querySelector("#trace");
const tableListEl = document.querySelector("#tableList");
const tablePreviewEl = document.querySelector("#tablePreview");
const tableTitleEl = document.querySelector("#tableTitle");
const explanationContentEl = document.querySelector("#explanationContent");
const explanationTabs = [...document.querySelectorAll(".explanation-tab")];

let lastRunResult = null;

const setText = (id, value) => {
  document.querySelector(id).textContent = value ?? "-";
};

const pretty = (value) => JSON.stringify(value ?? {}, null, 2);

const escapeHtml = (value) => String(value ?? "-")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const list = (items, empty = "Нет.") => {
  if (!items?.length) return `<p class="empty-state">${empty}</p>`;
  return `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
};

function renderExplanation(view) {
  if (!lastRunResult) return;

  const packet = lastRunResult.recommendation_packet || {};
  const agents = lastRunResult.final_state?.agent_results || {};
  const dq = agents.data_quality || {};
  const quality = agents.quality_state || {};
  const reliability = agents.reliability_state || {};
  const safety = agents.safety_gate || {};
  const rejected = safety.rejected_scenarios || packet.rejected_scenarios || [];
  const dqForbidden = dq.forbidden_changes || [];
  const reliabilityForbidden = reliability.forbidden_changes || [];

  if (view === "recommendation") {
    explanationContentEl.innerHTML = `
      <div class="explanation-grid">
        <article><span class="label">Решение</span><strong>${escapeHtml(packet.decision)}</strong></article>
        <article><span class="label">Действие</span><strong>${escapeHtml(packet.action || "Управляющее действие не выдано")}</strong></article>
        <article><span class="label">Прогноз серы</span><strong>${escapeHtml(packet.expected_quality?.sulfur_mg_kg)} mg/kg</strong></article>
        <article><span class="label">Уверенность</span><strong>${escapeHtml(packet.confidence)} (${escapeHtml(packet.confidence_value)})</strong></article>
      </div>
      <p class="explanation-message">${escapeHtml(lastRunResult.operator_message)}</p>
    `;
    return;
  }

  if (view === "why") {
    explanationContentEl.innerHTML = `
      <div class="reason-grid">
        <article><h3>Качество данных</h3><p>Статус: <strong>${escapeHtml(dq.data_status)}</strong>; confidence: <strong>${escapeHtml(dq.confidence)}</strong>.</p>${list(dq.anomalies, "Аномалии не найдены.")}</article>
        <article><h3>Качество продукта</h3><p>Источник серы: <strong>${escapeHtml(dq.source_priority_used?.sulfur)}</strong>; прогноз: <strong>${escapeHtml(quality.forecast_quality?.sulfur_mg_kg)} mg/kg</strong>.</p>${list(quality.main_drivers)}</article>
        <article><h3>Режим оборудования</h3><p>Риск: <strong>${escapeHtml(reliability.risk_class)}</strong> (${escapeHtml(reliability.risk_score)}).</p>${list(reliability.limiting_factors)}</article>
      </div>
    `;
    return;
  }

  if (view === "constraints") {
    const constraints = [
      ...dqForbidden.map((item) => `DQ: ${item.parameter} ${item.direction} — ${item.reason}`),
      ...reliabilityForbidden.map((item) => `Reliability: ${item.parameter} ${item.direction} — ${item.reason}`),
    ];
    explanationContentEl.innerHTML = `<h3>Запрещённые действия</h3>${list(constraints, "Система не добавила запретов на изменения.")}`;
    return;
  }

  if (view === "rejections") {
    if (!rejected.length) {
      explanationContentEl.innerHTML = '<p class="empty-state">Отклонённых сценариев нет.</p>';
      return;
    }
    explanationContentEl.innerHTML = rejected.map((item) => `
      <article class="rejection-card">
        <h3>${escapeHtml(item.scenario_id)}</h3>
        ${list(item.reasons, "Причина не зафиксирована.")}
      </article>
    `).join("");
    return;
  }

  explanationContentEl.innerHTML = `<pre class="trace-json">${escapeHtml(pretty(lastRunResult.trace || []))}</pre>`;
}

function selectExplanationView(view) {
  explanationTabs.forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  renderExplanation(view);
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

function renderTrace(trace) {
  traceEl.innerHTML = "";
  trace.forEach((step, index) => {
    const item = document.createElement("article");
    item.className = "step";
    item.innerHTML = `
      <div class="step-header">
        <strong>${index + 1}. ${step.title || step.agent}</strong>
        <span class="pill status">${step.status || "completed"}</span>
      </div>
      <div class="step-body">
        <div class="pane">
          <h4>Вход</h4>
          <pre>${pretty(step.input_summary)}</pre>
        </div>
        <div class="pane">
          <h4>Что происходит</h4>
          <ol class="steps">${(step.analysis_steps || []).map((text) => `<li>${text}</li>`).join("")}</ol>
        </div>
        <div class="pane">
          <h4>Выход</h4>
          <pre>${pretty(step.output)}</pre>
        </div>
      </div>
    `;
    traceEl.appendChild(item);
  });
}

function renderTable(rows) {
  if (!rows.length) {
    tablePreviewEl.textContent = "Нет строк для показа.";
    return;
  }
  const columns = Object.keys(rows[0]);
  tablePreviewEl.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>${columns.map((column) => `<th>${column}</th>`).join("")}</tr>
        </thead>
        <tbody>
          ${rows
            .map(
              (row) =>
                `<tr>${columns
                  .map((column) => `<td title="${row[column] ?? ""}">${row[column] ?? ""}</td>`)
                  .join("")}</tr>`
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

async function loadTables() {
  const { tables } = await getJson("/api/tables");
  tableListEl.innerHTML = "";
  tables.forEach((table, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = table;
    button.addEventListener("click", async () => {
      [...tableListEl.querySelectorAll("button")].forEach((node) => node.classList.remove("active"));
      button.classList.add("active");
      const result = await getJson(`/api/table?name=${encodeURIComponent(table)}`);
      tableTitleEl.textContent = table;
      renderTable(result.rows);
    });
    tableListEl.appendChild(button);
    if (index === 0) button.click();
  });
}

async function loadSnapshots() {
  const { snapshots } = await getJson("/api/snapshots");
  snapshotSelect.innerHTML = snapshots
    .map((item) => `<option value="${item.snapshot_id}">${item.snapshot_id} · ${item.timestamp}</option>`)
    .join("");
}

async function runSelectedSnapshot() {
  runButton.disabled = true;
  runButton.textContent = "Запуск...";
  try {
    const result = await getJson(`/api/run?snapshot_id=${encodeURIComponent(snapshotSelect.value)}`);
    lastRunResult = result;
    const packet = result.recommendation_packet || {};
    setText("#decision", packet.decision);
    setText("#scenario", packet.selected_scenario);
    setText("#confidence", packet.confidence ? `${packet.confidence} (${packet.confidence_value})` : "-");
    setText("#operatorMessage", result.operator_message);
    setText("#timestamp", result.timestamp);
    renderTrace(result.trace || []);
    selectExplanationView("recommendation");
  } finally {
    runButton.disabled = false;
    runButton.textContent = "Запустить";
  }
}

runButton.addEventListener("click", runSelectedSnapshot);
explanationTabs.forEach((button) => {
  button.addEventListener("click", () => selectExplanationView(button.dataset.view));
});

async function init() {
  await Promise.all([loadSnapshots(), loadTables()]);
  await runSelectedSnapshot();
}

init().catch((error) => {
  traceEl.innerHTML = `<pre>${error.message}</pre>`;
});
