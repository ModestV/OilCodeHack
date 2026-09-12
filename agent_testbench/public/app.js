const snapshotSelect = document.querySelector("#snapshotSelect");
const runButton = document.querySelector("#runButton");
const traceEl = document.querySelector("#trace");
const tableListEl = document.querySelector("#tableList");
const tablePreviewEl = document.querySelector("#tablePreview");
const tableTitleEl = document.querySelector("#tableTitle");

const setText = (id, value) => {
  document.querySelector(id).textContent = value ?? "-";
};

const pretty = (value) => JSON.stringify(value ?? {}, null, 2);

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
    const packet = result.recommendation_packet || {};
    setText("#decision", packet.decision);
    setText("#scenario", packet.selected_scenario);
    setText("#confidence", packet.confidence ? `${packet.confidence} (${packet.confidence_value})` : "-");
    setText("#operatorMessage", result.operator_message);
    setText("#timestamp", result.timestamp);
    renderTrace(result.trace || []);
  } finally {
    runButton.disabled = false;
    runButton.textContent = "Запустить";
  }
}

runButton.addEventListener("click", runSelectedSnapshot);

async function init() {
  await Promise.all([loadSnapshots(), loadTables()]);
  await runSelectedSnapshot();
}

init().catch((error) => {
  traceEl.innerHTML = `<pre>${error.message}</pre>`;
});
