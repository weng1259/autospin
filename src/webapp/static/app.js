const TOKEN_STORAGE_KEY = "spincoater.web.token";
const RELAY_NOTE_STORAGE_PREFIX = "spincoater.web.relay-note.";
const RECONNECT_MAX_MS = 8000;
const EVENT_LIMIT = 100;
const CHART_POINT_LIMIT = 60;
const REQUESTING_OPERATION = "requesting";

const DEVICE_LABELS = {
  gantry: "龙门架",
  heater: "加热台",
  spincoater: "旋涂",
  pipette: "移液",
  linear_stage: "滑台",
  relay: "继电器",
  gripper: "夹爪",
  routine: "程序",
};

const ACTION_LABELS = {
  connect: "连接",
  disconnect: "断开",
  home: "归零",
  move: "移动",
  jog: "点动",
  recover: "Alarm 恢复",
  "set-sv": "设定 SV",
  pv: "读取 PV",
  start: "启动",
  stop: "停止",
  fault: "读取故障",
  aspirate: "吸液",
  dispense: "排液",
  "eject-tip": "退 Tip",
  "ch-on": "通道 ON",
  "ch-off": "通道 OFF",
  open: "张开",
  close: "夹紧",
  replay: "重放",
};

const elements = {
  serviceDot: document.querySelector("#service-dot"),
  serviceStatus: document.querySelector("#service-status"),
  streamDot: document.querySelector("#stream-dot"),
  streamStatus: document.querySelector("#stream-status"),
  statusSeq: document.querySelector("#status-seq"),
  operationDeviceAction: document.querySelector("#operation-device-action"),
  operationElapsed: document.querySelector("#operation-elapsed"),
  tokenInput: document.querySelector("#token-input"),
  authMessage: document.querySelector("#auth-message"),
  estopButton: document.querySelector("#estop-button"),
  estopSummary: document.querySelector("#estop-summary"),
  estopReport: document.querySelector("#estop-report"),
  heaterChart: document.querySelector("#heater-chart"),
  spincoaterChart: document.querySelector("#spincoater-chart"),
  eventCount: document.querySelector("#event-count"),
  eventLog: document.querySelector("#event-log"),
  devicePanels: new Map(
    [
      ...[...document.querySelectorAll("[data-device]")].map((panel) => [
        panel.dataset.device,
        panel,
      ]),
      ["routine", document.querySelector("#routine-panel")],
    ],
  ),
  routineRecordForm: document.querySelector("#routine-record-form"),
  routineNameInput: document.querySelector("#routine-name-input"),
  routineDisarmButton: document.querySelector("#routine-disarm-button"),
  routineRecordDot: document.querySelector("#routine-record-dot"),
  routineRecordState: document.querySelector("#routine-record-state"),
  routineStepCount: document.querySelector("#routine-step-count"),
  routineRecentStep: document.querySelector("#routine-recent-step"),
  routineListBody: document.querySelector("#routine-list-body"),
  routineReplayProgress: document.querySelector("#routine-replay-progress"),
  routineReplayLabel: document.querySelector("#routine-replay-label"),
  routineAbortButton: document.querySelector("#routine-abort-button"),
  routineOperationId: document.querySelector("#routine-operation-id"),
  routineMessage: document.querySelector("#routine-message"),
};

const state = {
  events: [],
  currentOperation: null,
  operationConflict: null,
  operationRequestInFlight: false,
  lastOperationRefreshAt: 0,
  startedOperationIds: new Set(),
  completedOperationIds: new Set(),
  panelPending: new Map(),
  gantryMachineState: null,
  streamController: null,
  streamRevision: 0,
  reconnectDelayMs: 1000,
  reconnectTimer: null,
  tokenChangeTimer: null,
  estopTextTimer: null,
  routineRecordingArmed: false,
  routineRequestInFlight: false,
  routineStatusRequestInFlight: false,
  routineListRequestInFlight: false,
  routineDeleteConfirmName: null,
  routines: [],
  chartHistory: {
    heaterPv: [],
    heaterSv: [],
    spincoaterRpm: [],
  },
};

function appendChartPoint(series, value) {
  series.push(Number.isFinite(Number(value)) ? Number(value) : null);
  if (series.length > CHART_POINT_LIMIT) {
    series.splice(0, series.length - CHART_POINT_LIMIT);
  }
}

function drawLiveChart(canvas, seriesList, { unit, minimum = 0 }) {
  if (!canvas) {
    return;
  }
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(220, Math.round(rect.width));
  const height = 120;
  const scale = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);

  const context = canvas.getContext("2d");
  context.scale(scale, scale);
  const styles = getComputedStyle(document.documentElement);
  const colors = {
    border: styles.getPropertyValue("--border").trim(),
    muted: styles.getPropertyValue("--muted").trim(),
    primary: styles.getPropertyValue("--blue").trim(),
    secondary: styles.getPropertyValue("--green").trim(),
  };
  const padding = { top: 10, right: 8, bottom: 18, left: 34 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const values = seriesList.flatMap((series) => series.values).filter(Number.isFinite);
  const rawMax = values.length ? Math.max(...values, minimum + 1) : minimum + 1;
  const max = Math.max(rawMax * 1.1, minimum + 1);

  context.clearRect(0, 0, width, height);
  context.lineWidth = 1;
  context.strokeStyle = colors.border;
  context.fillStyle = colors.muted;
  context.font = '10px ui-monospace, "SF Mono", Menlo, monospace';
  context.textAlign = "right";
  context.textBaseline = "middle";

  for (let line = 0; line <= 2; line += 1) {
    const ratio = line / 2;
    const y = padding.top + plotHeight * ratio;
    context.beginPath();
    context.moveTo(padding.left, y);
    context.lineTo(width - padding.right, y);
    context.stroke();
    context.fillText(`${Math.round(max * (1 - ratio))}`, padding.left - 5, y);
  }
  context.textAlign = "left";
  context.fillText(unit, 3, height - 7);

  for (const series of seriesList) {
    context.strokeStyle = colors[series.color];
    context.lineWidth = 2;
    context.beginPath();
    let drawing = false;
    series.values.forEach((value, index) => {
      if (!Number.isFinite(value)) {
        drawing = false;
        return;
      }
      const denominator = Math.max(CHART_POINT_LIMIT - 1, 1);
      const slot = CHART_POINT_LIMIT - series.values.length + index;
      const x = padding.left + (slot / denominator) * plotWidth;
      const y = padding.top + plotHeight - ((value - minimum) / (max - minimum)) * plotHeight;
      if (drawing) {
        context.lineTo(x, y);
      } else {
        context.moveTo(x, y);
        drawing = true;
      }
    });
    context.stroke();
  }
}

function renderLiveCharts() {
  drawLiveChart(elements.heaterChart, [
    { values: state.chartHistory.heaterPv, color: "primary" },
    { values: state.chartHistory.heaterSv, color: "secondary" },
  ], { unit: "℃" });
  drawLiveChart(elements.spincoaterChart, [
    { values: state.chartHistory.spincoaterRpm, color: "primary" },
  ], { unit: "RPM" });
}

async function loadRuntimeConfiguration() {
  if (!token()) {
    return;
  }
  const config = await api("/api/config/runtime");
  const limits = config.gantry && config.gantry.soft_limits;
  const ranges = {
    "gantry-x": limits && [limits.x_min_mm, limits.x_max_mm],
    "gantry-y": limits && [limits.y_min_mm, limits.y_max_mm],
    "gantry-z": limits && [limits.z_min_mm, limits.z_max_mm],
    "linear-stage": config.linear_stage && [
      config.linear_stage.min_position_mm,
      config.linear_stage.max_position_mm,
    ],
  };
  for (const input of document.querySelectorAll("[data-config-range]")) {
    const range = ranges[input.dataset.configRange];
    if (range) {
      input.min = String(range[0]);
      input.max = String(range[1]);
    }
  }
  const maxima = {
    heater: config.heater && config.heater.sv_max_c,
    spincoater: config.spincoater && config.spincoater.max_rpm,
    pipette: config.pipette && config.pipette.max_volume_ul,
  };
  for (const input of document.querySelectorAll("[data-config-max]")) {
    const maximum = maxima[input.dataset.configMax];
    if (Number.isFinite(maximum)) {
      input.max = String(maximum);
    }
  }
}

function setBuilderMessage(message, danger = false) {
  const output = document.querySelector("#builder-message");
  output.textContent = message;
  output.classList.toggle("is-danger", danger);
  output.classList.toggle("is-success", !danger);
}

let builderDefaults = null;
let builderDefaultsLoading = false;
let builderInitialized = false;
let builderDirty = false;
let savedBuilderRecipeName = null;
let builderRealRunArmed = false;

function groupField(card, name) {
  return card.querySelector(`[data-group-field="${name}"]`);
}

function updateBuilderTotals() {
  const cards = [...document.querySelectorAll(".builder-group-card")];
  const total = cards.reduce(
    (sum, card) => sum + Math.max(0, Number(groupField(card, "repeats").value) || 0),
    0,
  );
  document.querySelector("#builder-total-rounds").textContent = String(total);
  cards.forEach((card, index) => {
    const name = groupField(card, "name").value.trim() || `参数组 ${index + 1}`;
    card.querySelector("[data-group-title]").textContent = name;
    card.querySelector("[data-group-repeat-summary]").textContent =
      String(Math.max(0, Number(groupField(card, "repeats").value) || 0));
    card.querySelector(".builder-remove-group").disabled = cards.length === 1;
    const duration = Number(groupField(card, "stage_2_time_s").value);
    const delay = Number(groupField(card, "antisolvent_delay_stage_2_s").value);
    const useAntisolvent = groupField(card, "use_antisolvent").checked;
    const ratio = duration > 0 ? delay / duration : 0;
    const ratioOutput = card.querySelector("[data-group-ratio]");
    ratioOutput.textContent = useAntisolvent && Number.isFinite(ratio)
      ? ratio.toFixed(2)
      : "不使用";
    ratioOutput.classList.toggle(
      "is-danger",
      useAntisolvent && (ratio < 0 || ratio > 1),
    );
    groupField(card, "antisolvent_delay_stage_2_s").max =
      duration > 0 ? String(duration) : "0";
    ["antisolvent_volume_ul", "antisolvent_delay_stage_2_s"].forEach((name) => {
      const input = groupField(card, name);
      input.disabled = !useAntisolvent;
      input.required = useAntisolvent;
    });
  });
}

function addBuilderGroup(values = {}) {
  const container = document.querySelector("#builder-groups");
  const template = document.querySelector("#builder-group-template");
  const card = template.content.firstElementChild.cloneNode(true);
  const index = container.children.length + 1;
  const defaults = {
    ...(builderDefaults ? builderDefaults.default_group : {}),
    name: `参数组 ${index}`,
    ...values,
  };
  card.querySelectorAll("[data-group-field]").forEach((input) => {
    const value = defaults[input.dataset.groupField];
    if (input.type === "checkbox") {
      input.checked = value === undefined ? true : Boolean(value);
    } else {
      input.value = value === undefined ? "" : String(value);
    }
    input.addEventListener("input", () => {
      builderDirty = true;
      updateBuilderTotals();
    });
  });
  card.querySelector(".builder-remove-group").addEventListener("click", () => {
    card.remove();
    updateBuilderTotals();
  });
  container.append(card);
  updateBuilderTotals();
}

async function loadExperimentBuilderDefaults() {
  if (!token() || builderDefaultsLoading || builderInitialized || builderDirty) {
    return;
  }
  builderDefaultsLoading = true;
  try {
    const defaults = await api("/api/experiment-builder/defaults");
    builderDefaults = defaults;
    if (builderDirty) {
      return;
    }
    const form = document.querySelector("#experiment-builder-form");
    form.elements.namedItem("experiment_name").value =
      defaults.default_experiment_name;
    form.elements.namedItem("output_name").value = defaults.default_output;
    document.querySelector("#builder-groups").replaceChildren();
    addBuilderGroup(defaults.default_group);
    builderInitialized = true;
  } catch (error) {
    setBuilderMessage(`构建器默认值读取失败：${error.message}`, true);
  } finally {
    builderDefaultsLoading = false;
  }
}

function builderPayload(save) {
  const form = document.querySelector("#experiment-builder-form");
  const numericFields = [
    "repeats",
    "precursor_volume_ul",
    "antisolvent_volume_ul",
    "annealing_temperature_c",
    "stage_1_speed_rpm",
    "stage_1_time_s",
    "stage_2_speed_rpm",
    "stage_2_time_s",
    "antisolvent_delay_stage_2_s",
    "tip_height_mm",
    "annealing_time_s",
  ];
  const groups = [...document.querySelectorAll(".builder-group-card")].map((card) => {
    const group = {
      name: groupField(card, "name").value.trim(),
      use_antisolvent: groupField(card, "use_antisolvent").checked,
    };
    numericFields.forEach((name) => {
      group[name] = Number(groupField(card, name).value);
    });
    return group;
  });
  return {
    experiment_name: form.elements.namedItem("experiment_name").value.trim(),
    output_name: form.elements.namedItem("output_name").value.trim(),
    groups,
    save,
  };
}

function renderBuilderSummary(payload) {
  const summary = payload.summary || {};
  const human = payload.human_check || {};
  const steps = Array.isArray(summary.preview_steps) ? summary.preview_steps : [];
  const container = document.querySelector("#builder-summary");
  const commandMock = payload.run_commands && payload.run_commands.mock
    ? payload.run_commands.mock
    : "—";
  const commandReal = payload.run_commands && payload.run_commands.real
    ? payload.run_commands.real
    : "—";
  const groups = Array.isArray(payload.groups) ? payload.groups : [];
  const groupRows = groups.map((group) => `
    <tr>
      <td>${escapeHtml(group.name)}</td>
      <td class="numeric-reading">${group.repeats} 次<br>第 ${group.round_start}–${group.round_end} 轮</td>
      <td>${formatMetric(group.precursor_volume_ul, "µL")}</td>
      <td>${group.use_antisolvent
        ? `${formatMetric(group.antisolvent_volume_ul, "µL")}<br>${formatMetric(group.antisolvent_delay_stage_2_s, "s")}（r=${Number(group.antisolvent_timing_ratio).toFixed(2)}）`
        : "不使用"}</td>
      <td>速度：${formatMetric(group.stage_1_speed_rpm, "RPM")}<br>时间：${formatMetric(group.stage_1_time_s, "s")}</td>
      <td>速度：${formatMetric(group.stage_2_speed_rpm, "RPM")}<br>时间：${formatMetric(group.stage_2_time_s, "s")}</td>
      <td>温度：${formatMetric(group.annealing_temperature_c, "°C")}<br>时间：${formatMetric(group.annealing_time_s, "s")}</td>
      <td>${formatMetric(group.tip_height_mm, "mm")}</td>
    </tr>
  `).join("");
  const rows = steps.slice(0, 24).map((step) => `
    <tr>
      <td class="numeric-reading">${step.index}</td>
      <td>${escapeHtml(step.operation)}</td>
      <td><code>${escapeHtml(JSON.stringify(step.params || {}))}</code></td>
    </tr>
  `).join("");
  container.innerHTML = `
    <dl class="builder-metrics">
      <div><dt>状态</dt><dd>${payload.saved ? "已保存" : "dry-run 通过"}</dd></div>
      <div><dt>总轮数</dt><dd class="numeric-reading">${payload.rounds ?? summary.rounds ?? "—"}</dd></div>
      <div><dt>参数组</dt><dd class="numeric-reading">${groups.length}</dd></div>
      <div><dt>每轮步骤</dt><dd class="numeric-reading">${summary.operations_per_round ?? "—"}</dd></div>
      <div><dt>总步骤</dt><dd class="numeric-reading">${summary.operation_count ?? "—"}</dd></div>
      <div><dt>龙门目标</dt><dd class="numeric-reading">${summary.gantry_targets ?? "—"}</dd></div>
      <div><dt>移液动作</dt><dd class="numeric-reading">${summary.pipette_operations ?? "—"}</dd></div>
      <div><dt>旋涂动作</dt><dd class="numeric-reading">${summary.spin_operations ?? "—"}</dd></div>
    </dl>
    <table class="builder-group-summary-table">
      <thead>
        <tr>
          <th scope="col">参数组</th><th scope="col">重复 / 轮次</th>
          <th scope="col">前驱液</th><th scope="col">反溶剂 / 时机</th>
          <th scope="col">阶段 1</th><th scope="col">阶段 2</th>
          <th scope="col">退火</th><th scope="col">Tip 高度</th>
        </tr>
      </thead>
      <tbody>${groupRows || '<tr><td class="routine-empty" colspan="8">无参数组</td></tr>'}</tbody>
    </table>
    <div class="builder-checks">
      ${(payload.groups || []).map((group) => `
        <span>
          <strong>${escapeHtml(group.name)}</strong>
          · 第 ${group.round_start}–${group.round_end} 轮
          · ${group.use_antisolvent
            ? `r=${Number(group.antisolvent_timing_ratio).toFixed(2)}`
            : "不使用反溶剂"}
        </span>
      `).join("")}
      <span>输出 ${escapeHtml(payload.output || "—")}</span>
    </div>
    <div class="builder-command-grid">
      <code>${escapeHtml(commandMock)}</code>
      <code>${escapeHtml(commandReal)}</code>
    </div>
    <table class="routine-table builder-preview-table">
      <thead><tr><th scope="col">#</th><th scope="col">动作</th><th scope="col">参数</th></tr></thead>
      <tbody>${rows || '<tr><td class="routine-empty" colspan="3">无步骤</td></tr>'}</tbody>
    </table>
  `;
}

function formatMetric(value, unit) {
  return Number.isFinite(Number(value)) ? `${Number(value).toLocaleString()} ${unit}` : `— ${unit}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function bindBuilderControls() {
  const builderForm = document.querySelector("#experiment-builder-form");
  builderForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!builderForm.reportValidity()) {
      return;
    }
    try {
      const result = await api("/api/experiment-builder/recipe", builderPayload(true));
      renderBuilderSummary(result);
      savedBuilderRecipeName = result.recipe_name || null;
      builderRealRunArmed = false;
      document.querySelector("#builder-run-dry-button").disabled = !savedBuilderRecipeName;
      const realRunButton = document.querySelector("#builder-run-real-button");
      realRunButton.disabled = !savedBuilderRecipeName;
      realRunButton.textContent = "正式运行已保存 recipe";
      setBuilderMessage(`已保存 recipe：${result.output}`);
    } catch (error) {
      setBuilderMessage(`保存失败：${error.message}`, true);
    }
  });
  document.querySelector("#builder-preview-button").addEventListener("click", async () => {
    if (!builderForm.reportValidity()) {
      return;
    }
    try {
      const result = await api("/api/experiment-builder/recipe", builderPayload(false));
      renderBuilderSummary(result);
      setBuilderMessage("Dry-run 预览通过");
    } catch (error) {
      setBuilderMessage(`预览失败：${error.message}`, true);
    }
  });
  builderForm.elements.namedItem("experiment_name").addEventListener("input", () => {
    builderDirty = true;
    const name = builderForm.elements.namedItem("experiment_name").value.trim();
    if (name) {
      builderForm.elements.namedItem("output_name").value =
        `${name.replace(/[^a-zA-Z0-9_.-]+/g, "_")}.json`;
    }
  });
  document.querySelector("#builder-add-group").addEventListener(
    "click",
    () => {
      builderDirty = true;
      addBuilderGroup();
    },
  );
  document.querySelector("#builder-run-dry-button").addEventListener("click", () => {
    void runSavedBuilderRecipe(true);
  });
  document.querySelector("#builder-run-real-button").addEventListener("click", () => {
    const button = document.querySelector("#builder-run-real-button");
    if (!builderRealRunArmed) {
      builderRealRunArmed = true;
      button.textContent = "再次点击确认正式运行";
      setBuilderMessage(
        "正式运行待确认：请核对轮数、参数、耗材和设备状态后再次点击",
        true,
      );
      return;
    }
    builderRealRunArmed = false;
    button.textContent = "正式运行已保存 recipe";
    void runSavedBuilderRecipe(false);
  });
}

async function runSavedBuilderRecipe(dryRun) {
  if (!savedBuilderRecipeName) {
    setBuilderMessage("请先生成并保存多轮 recipe", true);
    return;
  }
  const mode = dryRun ? "Dry-run" : "正式运行";
  try {
    const accepted = await api(
      "/api/experiments/multi-round/execute",
      {
        recipe_name: savedBuilderRecipeName,
        dry_run: dryRun,
      },
    );
    setBuilderMessage(
      `${mode}已提交：${accepted.rounds} 轮，operation ${accepted.operation_id}`,
    );
    logEvent(
      "多轮实验",
      `${mode} ${savedBuilderRecipeName} · ${accepted.rounds} 轮`,
    );
  } catch (error) {
    setBuilderMessage(`${mode}提交失败：${error.message}`, true);
  }
}

function token() {
  return elements.tokenInput.value.trim();
}

function authorizationHeaders({ json = false, eventStream = false } = {}) {
  const headers = {};
  const currentToken = token();
  if (currentToken) {
    headers.Authorization = `Bearer ${currentToken}`;
  }
  if (json) {
    headers["Content-Type"] = "application/json";
  }
  if (eventStream) {
    headers.Accept = "text/event-stream";
  }
  return headers;
}

function setConnectionState(dot, copy, mode, text) {
  dot.classList.toggle("is-online", mode === "online");
  dot.classList.toggle("is-danger", mode === "danger");
  copy.classList.toggle("is-online", mode === "online");
  copy.classList.toggle("is-danger", mode === "danger");
  copy.textContent = text;
}

function setServiceState(mode, text) {
  setConnectionState(elements.serviceDot, elements.serviceStatus, mode, text);
}

function setStreamState(mode, text) {
  setConnectionState(elements.streamDot, elements.streamStatus, mode, text);
}

function setAuthMessage(text, { danger = false } = {}) {
  elements.authMessage.textContent = text;
  elements.authMessage.classList.toggle("is-danger", danger);
  elements.authMessage.classList.toggle(
    "is-success",
    !danger && text === "Token 已验证",
  );
}

function handleUnauthorized() {
  setServiceState("danger", "服务 鉴权失败");
  setAuthMessage("Token 无效或已失效", { danger: true });
}

async function responsePayload(response) {
  const text = await response.text();
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

function structuredErrorMessage(detail, fallback = "") {
  if (!detail || typeof detail !== "object") {
    return fallback;
  }
  const human = typeof detail.human_message === "string"
    ? detail.human_message.trim()
    : "";
  const action = typeof detail.suggested_action_zh === "string"
    ? detail.suggested_action_zh.trim()
    : "";
  if (human && action) {
    return `${human} 建议：${action}`;
  }
  return human || action || fallback;
}

function errorMessage(payload, fallback) {
  const detail = payload && typeof payload === "object" ? payload.error : null;
  const structured = structuredErrorMessage(detail);
  if (structured) {
    return structured;
  }
  if (payload && typeof payload.detail === "string") {
    return payload.detail;
  }
  if (payload && payload.detail && typeof payload.detail === "object") {
    const detail = payload.detail;
    const issues = detail.safety && Array.isArray(detail.safety.issues)
      ? detail.safety.issues
      : [];
    if (issues.length) {
      const prefix = Number.isFinite(Number(detail.round))
        ? `第 ${Number(detail.round)} 轮：`
        : "";
      return `${prefix}${issues.map((issue) => (
        `${issue.code || "SAFETY"}：${issue.message || "安全校验失败"}`
      )).join("；")}`;
    }
    if (Array.isArray(detail)) {
      return detail.map((item) => item.msg || JSON.stringify(item)).join("；");
    }
    return JSON.stringify(detail);
  }
  return fallback;
}

async function api(path, body, { method = null } = {}) {
  const hasBody = body !== undefined;
  const requestMethod = method || (hasBody ? "POST" : "GET");
  const sendJson = hasBody && requestMethod !== "GET";
  let response;
  try {
    response = await fetch(path, {
      method: requestMethod,
      headers: authorizationHeaders({ json: sendJson }),
      body: sendJson ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    setServiceState("danger", "服务 无法连接");
    throw error;
  }

  const payload = await responsePayload(response);
  if (!response.ok) {
    if (response.status === 401) {
      handleUnauthorized();
    } else {
      setServiceState("online", "服务 在线");
    }
    const error = new Error(
      errorMessage(payload, `API 请求失败（HTTP ${response.status}）`),
    );
    error.status = response.status;
    error.payload = payload;
    throw error;
  }

  setServiceState("online", "服务 在线");
  if (path !== "/api/health" && token()) {
    setAuthMessage("Token 已验证");
  }
  return payload;
}

async function apiEstop() {
  let response;
  try {
    response = await fetch("/api/estop", {
      method: "POST",
      headers: authorizationHeaders(),
    });
  } catch (error) {
    setServiceState("danger", "服务 无法连接");
    throw error;
  }

  const payload = await responsePayload(response);
  if (!response.ok) {
    if (response.status === 401) {
      handleUnauthorized();
    } else {
      setServiceState("online", "服务 在线");
    }
    const error = new Error(
      errorMessage(payload, `急停请求失败（HTTP ${response.status}）`),
    );
    error.status = response.status;
    error.payload = payload;
    throw error;
  }

  setServiceState("online", "服务 在线");
  if (token()) {
    setAuthMessage("Token 已验证");
  }
  return payload;
}

function formatEventTime(date) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function renderEventLog() {
  elements.eventLog.replaceChildren();
  elements.eventCount.textContent = `${state.events.length} 条`;

  if (state.events.length === 0) {
    const empty = document.createElement("li");
    empty.className = "event-empty";
    empty.textContent = "尚无事件";
    elements.eventLog.append(empty);
    return;
  }

  for (const event of state.events) {
    const row = document.createElement("li");
    row.className = "event-row";

    const time = document.createElement("time");
    time.className = "event-time";
    time.dateTime = event.at.toISOString();
    time.textContent = formatEventTime(event.at);

    const kind = document.createElement("span");
    kind.className = "event-kind";
    kind.classList.toggle("is-danger", event.danger);
    kind.textContent = event.kind;

    const message = document.createElement("span");
    message.className = "event-message";
    message.textContent = event.message;

    row.append(time, kind, message);
    elements.eventLog.append(row);
  }
}

function logEvent(kind, message, { danger = false } = {}) {
  state.events.unshift({ at: new Date(), kind, message, danger });
  state.events = state.events.slice(0, EVENT_LIMIT);
  renderEventLog();
}

function panelForDevice(device) {
  return elements.devicePanels.get(device) || null;
}

function setPanelMessage(device, text, { danger = false, success = false } = {}) {
  const panel = panelForDevice(device);
  const message = device === "routine"
    ? elements.routineMessage
    : panel ? panel.querySelector('[data-role="message"]') : null;
  if (!message) {
    return;
  }
  message.textContent = text;
  message.classList.toggle("is-danger", danger);
  message.classList.toggle("is-success", success && !danger);
}

function setReadout(name, text, { danger = false, online = false } = {}) {
  const output = document.querySelector(`[data-readout="${name}"]`);
  if (!output) {
    return;
  }
  output.textContent = text;
  output.classList.toggle("is-danger", danger);
  output.classList.toggle("is-online", online && !danger);
}

function snapshotRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function formatNumber(value, digits, unit) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return `— ${unit}`;
  }
  return `${value.toFixed(digits)} ${unit}`;
}

function formatBoolean(value, trueText, falseText) {
  if (value === true) {
    return trueText;
  }
  if (value === false) {
    return falseText;
  }
  return "—";
}

function setDeviceConnection(device, snapshot, connectedField = null) {
  const panel = panelForDevice(device);
  if (!panel) {
    return;
  }
  const dot = panel.querySelector('[data-role="connection-dot"]');
  const copy = panel.querySelector('[data-role="connection-text"]');
  if (!dot || !copy) {
    return;
  }

  let mode = "neutral";
  let text = "—";
  if (snapshot && typeof snapshot.error === "string") {
    mode = "danger";
    text = "异常";
    dot.title = snapshot.error;
  } else if (connectedField && snapshot && snapshot[connectedField] === true) {
    mode = "online";
    text = "已连接";
    dot.removeAttribute("title");
  } else if (connectedField && snapshot && snapshot[connectedField] === false) {
    text = "未连接";
    dot.removeAttribute("title");
  } else {
    dot.removeAttribute("title");
  }

  dot.classList.toggle("is-online", mode === "online");
  dot.classList.toggle("is-danger", mode === "danger");
  copy.classList.toggle("is-online", mode === "online");
  copy.classList.toggle("is-danger", mode === "danger");
  copy.textContent = text;
}

function renderGantrySnapshot(snapshot) {
  setDeviceConnection("gantry", snapshot);
  const position = snapshotRecord(snapshot && snapshot.position);
  const positionValid = snapshot && snapshot.position_valid !== false;
  const machineState = snapshot && typeof snapshot.state === "string"
    ? snapshot.state
    : null;
  const limits = snapshot && Array.isArray(snapshot.limit_pins)
    ? snapshot.limit_pins
    : null;

  setReadout("gantry-x", formatNumber(position && position.x_mm, 2, "mm"));
  setReadout("gantry-y", formatNumber(position && position.y_mm, 2, "mm"));
  setReadout("gantry-z", formatNumber(position && position.z_mm, 2, "mm"));
  setReadout("gantry-state", machineState || "—", {
    danger: machineState === "alarm" || !positionValid,
  });
  setReadout(
    "gantry-homed",
    formatBoolean(snapshot && snapshot.is_homed, "是", "否"),
  );
  setReadout(
    "gantry-limits",
    limits === null ? "—" : limits.length === 0 ? "无" : limits.join(" "),
    { danger: Boolean(limits && limits.length) },
  );
  if (!positionValid) {
    setPanelMessage(
      "gantry",
      "控制器返回异常坐标，已禁止继续运动。请立即停止，检查限位/接线干扰并重新归零。",
      { danger: true },
    );
  }

  state.gantryMachineState = machineState;
  updatePanelControls("gantry");
}

function renderHeaterSnapshot(snapshot) {
  setDeviceConnection("heater", snapshot, "connected");
  setReadout("heater-pv", formatNumber(snapshot && snapshot.pv_c, 1, "℃"));
  setReadout("heater-sv", formatNumber(snapshot && snapshot.sv_c, 1, "℃"));
  appendChartPoint(state.chartHistory.heaterPv, snapshot && snapshot.pv_c);
  appendChartPoint(state.chartHistory.heaterSv, snapshot && snapshot.sv_c);
  elements.heaterChart.setAttribute(
    "aria-label",
    `加热台实时温度趋势，PV ${formatNumber(snapshot && snapshot.pv_c, 1, "℃")}，SV ${formatNumber(snapshot && snapshot.sv_c, 1, "℃")}`,
  );
}

function renderSpincoaterSnapshot(snapshot) {
  setDeviceConnection("spincoater", snapshot, "connected");
  const faultBits = snapshot && Array.isArray(snapshot.fault_bits)
    ? snapshot.fault_bits
    : null;
  setReadout(
    "spincoater-rpm",
    formatNumber(snapshot && snapshot.target_rpm, 0, "RPM"),
  );
  setReadout(
    "spincoater-acceleration",
    formatNumber(snapshot && snapshot.acceleration_rpm_per_s, 0, "RPM/s"),
  );
  setReadout(
    "spincoater-deceleration",
    formatNumber(snapshot && snapshot.deceleration_rpm_per_s, 0, "RPM/s"),
  );
  appendChartPoint(state.chartHistory.spincoaterRpm, snapshot && snapshot.target_rpm);
  elements.spincoaterChart.setAttribute(
    "aria-label",
    `旋涂仪实时指令转速趋势，当前 ${formatNumber(snapshot && snapshot.target_rpm, 0, "RPM")}`,
  );
  setReadout(
    "spincoater-running",
    formatBoolean(snapshot && snapshot.running, "运行", "停止"),
    { online: snapshot && snapshot.running === true },
  );
  setReadout(
    "spincoater-faults",
    faultBits === null ? "—" : faultBits.length === 0 ? "无" : faultBits.join("、"),
    { danger: Boolean(faultBits && faultBits.length) },
  );
}

function renderPipetteSnapshot(snapshot) {
  setDeviceConnection("pipette", snapshot, "connected");
  setReadout(
    "pipette-position",
    formatNumber(snapshot && snapshot.position_steps, 0, "step"),
  );
  setReadout(
    "pipette-homed",
    formatBoolean(snapshot && snapshot.homed, "是", "否"),
  );
  setReadout(
    "pipette-tip",
    formatBoolean(snapshot && snapshot.tip_present, "有", "无"),
  );
}

function formatStageFlags(snapshot) {
  const raw = snapshot && snapshot.flags_raw;
  if (!Number.isInteger(raw)) {
    return "—";
  }
  const binary = (value) => value === true ? "1" : value === false ? "0" : "—";
  const hex = raw.toString(16).toUpperCase().padStart(2, "0");
  return `0x${hex} · 使能${binary(snapshot.enabled)} 到位${binary(snapshot.in_position)} 堵转${binary(snapshot.stalled)} 保护${binary(snapshot.stall_protection_active)}`;
}

function renderLinearStageSnapshot(snapshot) {
  setDeviceConnection("linear_stage", snapshot, "connected");
  setReadout(
    "linear-stage-position",
    formatNumber(snapshot && snapshot.position_mm, 2, "mm"),
  );
  setReadout(
    "linear-stage-homed",
    formatBoolean(snapshot && snapshot.homed, "是", "否"),
  );
  setReadout("linear-stage-flags", formatStageFlags(snapshot), {
    danger: Boolean(
      snapshot
      && (snapshot.stalled === true || snapshot.stall_protection_active === true),
    ),
  });
}

function renderRelaySnapshot(snapshot) {
  setDeviceConnection("relay", snapshot);
  const channels = snapshotRecord(snapshot && snapshot.channels);
  for (let channel = 3; channel <= 8; channel += 1) {
    const value = channels ? channels[String(channel)] : undefined;
    setReadout(
      `relay-${channel}`,
      value === true ? "ON" : value === false ? "OFF" : "—",
      { online: value === true },
    );
  }
  const vacuumOn = channels ? channels["3"] : undefined;
  setReadout(
    "spincoater-vacuum",
    vacuumOn === true ? "ON" : vacuumOn === false ? "OFF" : "—",
    { online: vacuumOn === true },
  );
}

function renderGripperSnapshot(snapshot) {
  setDeviceConnection("gripper", snapshot);
  const commanded = snapshot && typeof snapshot.commanded_state === "string"
    ? snapshot.commanded_state
    : null;
  const labels = { open: "张开", closed: "夹紧", unknown: "未知" };
  setReadout("gripper-state", commanded && labels[commanded] ? labels[commanded] : "—");
}

function renderDeviceSnapshots(devices) {
  const values = snapshotRecord(devices) || {};
  renderGantrySnapshot(snapshotRecord(values.gantry));
  renderHeaterSnapshot(snapshotRecord(values.heater));
  renderSpincoaterSnapshot(snapshotRecord(values.spincoater));
  renderPipetteSnapshot(snapshotRecord(values.pipette));
  renderLinearStageSnapshot(snapshotRecord(values.linear_stage));
  renderRelaySnapshot(snapshotRecord(values.relay));
  renderGripperSnapshot(snapshotRecord(values.gripper));
  renderLiveCharts();
}

function updatePanelControls(device) {
  const panel = panelForDevice(device);
  if (!panel) {
    return;
  }
  const pending = state.panelPending.has(device)
    || (device === "routine" && state.routineRequestInFlight);
  for (const control of panel.querySelectorAll("button, input, select")) {
    if (control.dataset.alwaysEnabled === "true") {
      control.disabled = false;
      continue;
    }
    const requiresAlarm = control.dataset.requiresAlarm === "true";
    const alarmReady = state.gantryMachineState === "alarm";
    control.disabled = pending || (requiresAlarm && !alarmReady);
    if (requiresAlarm) {
      control.classList.toggle("is-active", alarmReady && !pending);
    }
  }

  if (device === "routine") {
    const armButton = panel.querySelector('[data-routine-action="arm"]');
    if (armButton) {
      armButton.disabled = pending || state.routineRecordingArmed;
    }
    elements.routineNameInput.disabled = pending || state.routineRecordingArmed;
    elements.routineDisarmButton.disabled = pending || !state.routineRecordingArmed;
  }
}

function setPanelPending(device, operationId) {
  const panel = panelForDevice(device);
  if (!panel) {
    return;
  }
  state.panelPending.set(device, operationId);
  panel.dataset.pending = "true";
  const output = device === "routine"
    ? elements.routineOperationId
    : panel.querySelector('[data-role="operation-id"]');
  if (output) {
    output.hidden = false;
    output.textContent = operationId === REQUESTING_OPERATION
      ? "operation 等待接纳"
      : `operation ${operationId}`;
  }
  updatePanelControls(device);
}

function clearPanelPending(device) {
  const panel = panelForDevice(device);
  state.panelPending.delete(device);
  if (!panel) {
    return;
  }
  delete panel.dataset.pending;
  const output = device === "routine"
    ? elements.routineOperationId
    : panel.querySelector('[data-role="operation-id"]');
  if (output) {
    output.hidden = true;
    output.textContent = "";
  }
  updatePanelControls(device);
}

function completePanelOperation(operation) {
  if (!operation || typeof operation !== "object") {
    return;
  }
  const pendingId = state.panelPending.get(operation.device);
  if (pendingId !== operation.id) {
    return;
  }
  if (operation.device === "routine") {
    renderRoutineReplayOperation(operation);
  }
  clearPanelPending(operation.device);
  const elapsed = Number(operation.elapsed) || 0;
  if (operation.status === "failed") {
    setPanelMessage(
      operation.device,
      structuredErrorMessage(operation.error, `${operationName(operation)} 失败`),
      { danger: true },
    );
    if (operation.device === "routine") {
      void refreshRoutineList();
    }
    return;
  }
  setPanelMessage(
    operation.device,
    `${operationName(operation)} 已完成 · ${elapsed.toFixed(1)} s`,
    { success: true },
  );
  if (operation.device === "routine") {
    void refreshRoutineList();
  }
}

function showOperationConflict(error) {
  const payload = error && error.payload;
  const operation = snapshotRecord(payload && payload.current_operation);
  if (!operation) {
    return;
  }
  state.operationConflict = operation;
  state.currentOperation = operation;
  renderCurrentOperation();
}

function handlePanelError(device, label, error) {
  if (error && error.status === 409) {
    showOperationConflict(error);
  }
  const message = error && error.message ? error.message : `${label}请求失败`;
  setPanelMessage(device, message, { danger: true });
  logEvent(`${DEVICE_LABELS[device] || device} 请求失败`, message, { danger: true });
}

async function runPanelOperation(device, label, path, body) {
  if (state.panelPending.has(device)) {
    return;
  }
  setPanelPending(device, REQUESTING_OPERATION);
  setPanelMessage(device, `${label}请求发送中`);
  try {
    const accepted = await api(path, body);
    if (
      !accepted
      || accepted.accepted !== true
      || typeof accepted.operation_id !== "string"
    ) {
      throw new Error("服务未返回 operation id。建议：检查服务端响应后再重试。");
    }
    state.operationConflict = null;
    setPanelPending(device, accepted.operation_id);
    setPanelMessage(device, `${label}已接纳，等待 SSE 完成事件`);
    logEvent("operation 接纳", `${DEVICE_LABELS[device] || device} · ${label} · ${accepted.operation_id}`);
    void refreshCurrentOperation();
  } catch (error) {
    clearPanelPending(device);
    handlePanelError(device, label, error);
  }
}

async function runImmediatePanelAction(
  device,
  label,
  path,
  body,
  { eventKind = "立即停止" } = {},
) {
  setPanelMessage(device, `${label}请求已直达`);
  try {
    await api(path, body);
    const pendingSuffix = state.panelPending.has(device)
      ? "；原 operation 等待 SSE 收尾"
      : "";
    setPanelMessage(device, `${label}命令已完成${pendingSuffix}`, { success: true });
    logEvent(eventKind, `${DEVICE_LABELS[device] || device} · ${label}`);
  } catch (error) {
    handlePanelError(device, label, error);
  }
}

function formNumber(form, name) {
  const input = form.elements.namedItem(name);
  return input instanceof HTMLInputElement ? input.valueAsNumber : Number.NaN;
}

function routineEndpoint(name, suffix = "") {
  return `/api/routines/${encodeURIComponent(name)}${suffix}`;
}

function renderRoutineRecordingStatus(recording) {
  if (!recording || typeof recording !== "object") {
    return;
  }
  state.routineRecordingArmed = recording.armed === true;
  elements.routineRecordDot.classList.toggle(
    "is-danger",
    state.routineRecordingArmed,
  );
  elements.routineRecordState.classList.toggle(
    "is-danger",
    state.routineRecordingArmed,
  );
  elements.routineRecordState.textContent = state.routineRecordingArmed
    ? "录制中"
    : "未录制";
  const stepCount = Number(recording.step_count);
  elements.routineStepCount.textContent = `${Number.isFinite(stepCount) ? stepCount : 0} 步`;

  const recent = Array.isArray(recording.recent_steps)
    ? recording.recent_steps
    : [];
  const last = recent.length > 0 ? recent[recent.length - 1] : null;
  elements.routineRecentStep.textContent = last && typeof last.label === "string"
    ? `最近步骤：${last.label}`
    : "最近步骤：—";
  updatePanelControls("routine");
}

async function refreshRoutineRecordingStatus() {
  if (!token() || state.routineStatusRequestInFlight) {
    return;
  }
  state.routineStatusRequestInFlight = true;
  try {
    const recording = await api("/api/routines/record");
    renderRoutineRecordingStatus(recording);
  } catch (error) {
    const message = error && error.message
      ? error.message
      : "录制状态读取失败";
    setPanelMessage("routine", message, { danger: true });
  } finally {
    state.routineStatusRequestInFlight = false;
  }
}

function setRoutineRequestInFlight(inFlight) {
  state.routineRequestInFlight = inFlight;
  updatePanelControls("routine");
}

async function runRoutineRecordingRequest(label, path, body, onSuccess) {
  if (state.routineRequestInFlight || state.panelPending.has("routine")) {
    return;
  }
  setRoutineRequestInFlight(true);
  setPanelMessage("routine", `${label}请求发送中`);
  try {
    const payload = await api(path, body);
    onSuccess(payload);
    setPanelMessage("routine", `${label}已完成`, { success: true });
    logEvent("程序录制", label);
  } catch (error) {
    handlePanelError("routine", label, error);
  } finally {
    setRoutineRequestInFlight(false);
  }
}

function renderRoutineList(routines) {
  elements.routineListBody.replaceChildren();
  if (!Array.isArray(routines) || routines.length === 0) {
    const row = document.createElement("tr");
    const empty = document.createElement("td");
    empty.className = "routine-empty";
    empty.colSpan = 5;
    empty.textContent = "暂无程序";
    row.append(empty);
    elements.routineListBody.append(row);
    updatePanelControls("routine");
    return;
  }

  for (const routine of routines) {
    const name = typeof routine.name === "string" ? routine.name : "未命名程序";
    const row = document.createElement("tr");
    row.className = "routine-row";
    const confirming = state.routineDeleteConfirmName === name;
    row.classList.toggle("is-delete-confirm", confirming);

    const nameCell = document.createElement("td");
    nameCell.textContent = name;
    nameCell.title = name;

    const stepsCell = document.createElement("td");
    stepsCell.className = "numeric-reading";
    const stepCount = Number(routine.step_count);
    stepsCell.textContent = Number.isFinite(stepCount) ? String(stepCount) : "—";

    const durationCell = document.createElement("td");
    durationCell.className = "numeric-reading";
    const duration = Number(routine.duration_s);
    durationCell.textContent = Number.isFinite(duration)
      ? duration.toFixed(1)
      : "—";

    const motionCell = document.createElement("td");
    motionCell.className = "routine-motion";
    motionCell.textContent = routine.has_motion === true ? "有" : "无";

    const actionsCell = document.createElement("td");
    actionsCell.className = "routine-actions";
    const replayButton = document.createElement("button");
    replayButton.type = "button";
    replayButton.className = "primary-button";
    replayButton.textContent = "重放";
    replayButton.addEventListener("click", () => {
      state.routineDeleteConfirmName = null;
      elements.routineReplayProgress.textContent = `0/${Number.isFinite(stepCount) ? stepCount : 0}`;
      elements.routineReplayLabel.textContent = "当前步骤：等待开始";
      void runPanelOperation(
        "routine",
        `重放 ${name}`,
        routineEndpoint(name, "/replay"),
        {},
      );
    });

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.textContent = confirming ? "确认删除？" : "删除";
    deleteButton.classList.toggle("routine-delete-confirm", confirming);
    deleteButton.addEventListener("click", () => {
      void requestRoutineDelete(name);
    });

    actionsCell.append(replayButton, deleteButton);
    row.append(nameCell, stepsCell, durationCell, motionCell, actionsCell);
    elements.routineListBody.append(row);
  }
  updatePanelControls("routine");
}

async function refreshRoutineList() {
  if (!token() || state.routineListRequestInFlight) {
    return;
  }
  state.routineListRequestInFlight = true;
  try {
    const routines = await api("/api/routines");
    state.routines = Array.isArray(routines) ? routines : [];
    renderRoutineList(state.routines);
  } catch (error) {
    const message = error && error.message ? error.message : "程序列表读取失败";
    setPanelMessage("routine", message, { danger: true });
  } finally {
    state.routineListRequestInFlight = false;
  }
}

async function requestRoutineDelete(name) {
  if (state.routineDeleteConfirmName !== name) {
    state.routineDeleteConfirmName = name;
    renderRoutineList(state.routines);
    setPanelMessage("routine", `再次点击“确认删除？”以删除 ${name}`, {
      danger: true,
    });
    return;
  }
  if (state.routineRequestInFlight || state.panelPending.has("routine")) {
    return;
  }

  setRoutineRequestInFlight(true);
  setPanelMessage("routine", `正在删除 ${name}`);
  try {
    await api(routineEndpoint(name), undefined, { method: "DELETE" });
    state.routineDeleteConfirmName = null;
    setPanelMessage("routine", `${name} 已删除`, { success: true });
    logEvent("程序删除", name);
    await refreshRoutineList();
  } catch (error) {
    handlePanelError("routine", `删除 ${name}`, error);
  } finally {
    setRoutineRequestInFlight(false);
  }
}

function renderRoutineReplayOperation(operation) {
  if (!operation || operation.device !== "routine") {
    return;
  }
  const progress = snapshotRecord(operation.result);
  if (!progress) {
    return;
  }
  const completed = Number(progress.steps_completed);
  const total = Number(progress.steps_total);
  if (Number.isFinite(completed) && Number.isFinite(total)) {
    elements.routineReplayProgress.textContent = `${completed}/${total}`;
  }
  if (typeof progress.step_label === "string" && progress.step_label) {
    elements.routineReplayLabel.textContent = `当前步骤：${progress.step_label}`;
  }
}

function operationName(operation) {
  const device = DEVICE_LABELS[operation.device] || operation.device;
  const action = ACTION_LABELS[operation.action] || operation.action;
  return `${device} · ${action}`;
}

function operationElapsedSeconds(operation) {
  const reported = Number(operation.elapsed) || 0;
  const startedAtMs = Date.parse(operation.started_at);
  if (!Number.isFinite(startedAtMs)) {
    return reported;
  }
  return Math.max(reported, (Date.now() - startedAtMs) / 1000);
}

function renderCurrentOperation() {
  const operation = state.currentOperation;
  if (!operation) {
    elements.operationDeviceAction.textContent = "空闲";
    elements.operationDeviceAction.classList.add("is-idle");
    elements.operationElapsed.hidden = true;
    return;
  }

  if (state.operationConflict && state.operationConflict.id === operation.id) {
    const elapsed = operationElapsedSeconds(operation).toFixed(1);
    elements.operationDeviceAction.textContent = `被 ${operationName(operation)} 占用（已运行 ${elapsed}s）`;
    elements.operationDeviceAction.classList.remove("is-idle");
    elements.operationElapsed.hidden = true;
    return;
  }

  elements.operationDeviceAction.textContent = operationName(operation);
  elements.operationDeviceAction.classList.remove("is-idle");
  elements.operationElapsed.textContent = `${operationElapsedSeconds(operation).toFixed(1)} s`;
  elements.operationElapsed.hidden = false;
}

function recordOperationStart(operation, { recovered = false } = {}) {
  if (state.startedOperationIds.has(operation.id)) {
    return;
  }
  state.startedOperationIds.add(operation.id);
  const suffix = recovered ? "（由完成记录补录起点）" : "";
  logEvent("operation 开始", `${operationName(operation)}${suffix}`);
}

function recordOperationCompletion(operation) {
  if (!operation || state.completedOperationIds.has(operation.id)) {
    return;
  }
  if (!state.startedOperationIds.has(operation.id)) {
    recordOperationStart(operation, { recovered: true });
  }
  state.completedOperationIds.add(operation.id);

  const elapsed = Number(operation.elapsed) || 0;
  if (operation.status === "failed") {
    const detailMessage = structuredErrorMessage(operation.error);
    const detail = detailMessage ? `：${detailMessage}` : "";
    logEvent(
      "operation 结束",
      `${operationName(operation)} 失败 · ${elapsed.toFixed(1)} s${detail}`,
      { danger: true },
    );
    return;
  }

  logEvent(
    "operation 结束",
    `${operationName(operation)} 成功 · ${elapsed.toFixed(1)} s`,
  );
}

async function refreshCurrentOperation() {
  const now = Date.now();
  if (
    state.operationRequestInFlight
    || now - state.lastOperationRefreshAt < 1000
  ) {
    return;
  }
  state.lastOperationRefreshAt = now;
  state.operationRequestInFlight = true;
  try {
    const operation = await api("/api/operations/current");
    if (operation && (!state.currentOperation || state.currentOperation.id !== operation.id)) {
      recordOperationStart(operation);
    }
    if (!operation || !state.operationConflict || state.operationConflict.id !== operation.id) {
      state.operationConflict = null;
    }
    state.currentOperation = operation;
    renderRoutineReplayOperation(operation);
    renderCurrentOperation();
  } catch {
    // api() 已把鉴权或网络状态映射到顶栏。
  } finally {
    state.operationRequestInFlight = false;
  }
}

function handleStatusSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== "object") {
    return;
  }

  const seq = Number(snapshot.seq);
  if (Number.isFinite(seq)) {
    elements.statusSeq.textContent = `${seq} 帧`;
  }
  renderDeviceSnapshots(snapshot.devices);
  if (snapshot.last_operation) {
    completePanelOperation(snapshot.last_operation);
    recordOperationCompletion(snapshot.last_operation);
  }
  if (state.routineRecordingArmed) {
    void refreshRoutineRecordingStatus();
  }
  void refreshCurrentOperation();
}

function parseSseFrame(frame) {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) {
    return;
  }
  try {
    handleStatusSnapshot(JSON.parse(data));
  } catch {
    logEvent("状态流", "收到无法解析的 SSE 数据", { danger: true });
  }
}

function consumeSseFrames(buffer) {
  let remaining = buffer;
  let boundary = remaining.match(/\r?\n\r?\n/);
  while (boundary && boundary.index !== undefined) {
    const frame = remaining.slice(0, boundary.index);
    remaining = remaining.slice(boundary.index + boundary[0].length);
    parseSseFrame(frame);
    boundary = remaining.match(/\r?\n\r?\n/);
  }
  return remaining;
}

function clearReconnectTimer() {
  if (state.reconnectTimer !== null) {
    window.clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
}

function scheduleReconnect(revision, reason) {
  if (revision !== state.streamRevision || !token()) {
    return;
  }
  clearReconnectTimer();
  const delayMs = state.reconnectDelayMs;
  state.reconnectDelayMs = Math.min(delayMs * 2, RECONNECT_MAX_MS);
  setStreamState("danger", "状态流 等待重连");
  logEvent(
    "SSE 断连",
    `${reason}；${(delayMs / 1000).toFixed(0)} s 后重连`,
    { danger: true },
  );
  state.reconnectTimer = window.setTimeout(() => {
    state.reconnectTimer = null;
    void connectStatusStream(revision);
  }, delayMs);
}

async function connectStatusStream(revision) {
  if (revision !== state.streamRevision || !token()) {
    return;
  }

  const controller = new AbortController();
  state.streamController = controller;
  setStreamState("neutral", "状态流 连接中");

  let response;
  try {
    response = await fetch("/api/status/stream", {
      method: "GET",
      headers: authorizationHeaders({ eventStream: true }),
      signal: controller.signal,
      cache: "no-store",
    });
  } catch (error) {
    if (error.name === "AbortError" || revision !== state.streamRevision) {
      return;
    }
    setServiceState("danger", "服务 无法连接");
    scheduleReconnect(revision, "网络连接失败");
    return;
  }

  if (!response.ok) {
    if (response.status === 401) {
      handleUnauthorized();
    } else {
      setServiceState("online", "服务 在线");
    }
    scheduleReconnect(revision, `HTTP ${response.status}`);
    return;
  }

  if (!response.body) {
    scheduleReconnect(revision, "浏览器未提供可读状态流");
    return;
  }

  setServiceState("online", "服务 在线");
  setStreamState("online", "状态流 已连接");
  setAuthMessage("Token 已验证");
  state.reconnectDelayMs = 1000;

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (revision === state.streamRevision) {
      const { value, done } = await reader.read();
      if (done) {
        throw new Error("状态流已结束");
      }
      buffer += decoder.decode(value, { stream: true });
      buffer = consumeSseFrames(buffer);
    }
  } catch (error) {
    if (error.name === "AbortError" || revision !== state.streamRevision) {
      return;
    }
    scheduleReconnect(revision, error.message || "状态流中断");
  } finally {
    reader.releaseLock();
  }
}

function restartStatusStream() {
  state.streamRevision += 1;
  const revision = state.streamRevision;
  clearReconnectTimer();
  state.reconnectDelayMs = 1000;
  if (state.streamController) {
    state.streamController.abort();
    state.streamController = null;
  }

  if (!token()) {
    setStreamState("neutral", "状态流 未连接");
    setAuthMessage("未设置");
    state.currentOperation = null;
    renderCurrentOperation();
    return;
  }

  setAuthMessage("等待验证");
  void connectStatusStream(revision);
  void refreshRoutineRecordingStatus();
  void refreshRoutineList();
}

function persistToken(value) {
  try {
    if (value) {
      window.localStorage.setItem(TOKEN_STORAGE_KEY, value);
    } else {
      window.localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  } catch {
    setAuthMessage("浏览器拒绝本地保存", { danger: true });
  }
}

function loadStoredToken() {
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function renderEstopReport(report) {
  const steps = Array.isArray(report.steps) ? report.steps : [];
  const durationMs = Number(report.duration_ms) || 0;
  elements.estopSummary.textContent = `${report.ok ? "完成" : "需人工复核"} · ${steps.length} 步 · ${durationMs.toFixed(1)} ms`;
  elements.estopReport.replaceChildren();

  const list = document.createElement("ul");
  list.className = "estop-steps";
  for (const step of steps) {
    const item = document.createElement("li");
    item.className = "estop-step";

    const device = document.createElement("span");
    device.className = "step-device";
    device.textContent = step.device;

    const action = document.createElement("span");
    action.className = "step-action";
    action.textContent = step.action;

    const result = document.createElement("span");
    result.className = "step-result";
    result.classList.toggle("is-skipped", Boolean(step.skipped));
    result.classList.toggle("is-danger", !step.ok);
    result.textContent = step.skipped ? "跳过" : step.ok ? "成功" : "失败";

    item.append(device, action, result);
    if (step.error) {
      const detail = document.createElement("span");
      detail.className = "step-error";
      detail.textContent = step.error;
      item.append(detail);
    }
    list.append(item);
  }
  elements.estopReport.append(list);
}

function flashEstopButton() {
  elements.estopButton.textContent = "已发送…";
  if (state.estopTextTimer !== null) {
    window.clearTimeout(state.estopTextTimer);
  }
  state.estopTextTimer = window.setTimeout(() => {
    elements.estopButton.textContent = "紧急停止";
    state.estopTextTimer = null;
  }, 1000);
}

async function sendEstop() {
  flashEstopButton();
  logEvent("estop", "急停请求已发送", { danger: true });
  try {
    const report = await apiEstop();
    renderEstopReport(report);
    // 服务端会在系统急停完成后终止当前 operation。立即同步本地状态，避免
    // SSE 暂时断线时面板仍保持禁用、顶部计时器继续增加。
    for (const device of [...state.panelPending.keys()]) {
      clearPanelPending(device);
    }
    state.currentOperation = null;
    state.operationConflict = null;
    renderCurrentOperation();
    for (const step of report.steps || []) {
      const result = step.skipped ? "跳过" : step.ok ? "成功" : `失败：${step.error || "未知错误"}`;
      logEvent(
        "estop 结果",
        `${step.device} · ${step.action} · ${result}`,
        { danger: !step.ok },
      );
    }
  } catch (error) {
    logEvent("estop 失败", error.message || "急停请求失败", { danger: true });
  }
}

async function checkServiceHealth() {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    setServiceState("online", "服务 在线");
  } catch {
    setServiceState("danger", "服务 无法连接");
  }
}

function bindDeviceControls() {
  for (const button of document.querySelectorAll("[data-operation-path]")) {
    button.addEventListener("click", () => {
      const panel = button.closest("[data-device]");
      if (!panel) {
        return;
      }
      const body = button.dataset.operationMethod === "GET" ? undefined : {};
      void runPanelOperation(
        panel.dataset.device,
        button.dataset.operationLabel || button.textContent.trim(),
        button.dataset.operationPath,
        body,
      );
    });
  }

  for (const button of document.querySelectorAll("[data-jog-axis]")) {
    button.addEventListener("click", () => {
      const step = Number(document.querySelector("#gantry-step").value);
      const feed = Number(document.querySelector("#gantry-feed").value);
      const direction = Number(button.dataset.jogDirection);
      void runPanelOperation("gantry", `点动 ${button.textContent.trim()}`, "/api/gantry/jog", {
        axis: button.dataset.jogAxis,
        distance: step * direction,
        feed,
      });
    });
  }

  const gantryMoveForm = document.querySelector("#gantry-move-form");
  gantryMoveForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!gantryMoveForm.reportValidity()) {
      return;
    }
    void runPanelOperation("gantry", "绝对移动", "/api/gantry/move", {
      x: formNumber(gantryMoveForm, "x"),
      y: formNumber(gantryMoveForm, "y"),
      z: formNumber(gantryMoveForm, "z"),
      feed: Number(document.querySelector("#gantry-feed").value),
    });
  });
  document.querySelector("#gantry-dry-run-button").addEventListener("click", () => {
    if (!gantryMoveForm.reportValidity()) {
      return;
    }
    void runPanelOperation("gantry", "仅校验目标", "/api/gantry/dry-run", {
      x: formNumber(gantryMoveForm, "x"),
      y: formNumber(gantryMoveForm, "y"),
      z: formNumber(gantryMoveForm, "z"),
      feed: Number(document.querySelector("#gantry-feed").value),
    });
  });
  document.querySelector("#gantry-z-release-button").addEventListener("click", () => {
    void runPanelOperation("gantry", "释放 Z 制动", "/api/gantry/z-brake", {
      released: true,
    });
  });
  document.querySelector("#gantry-z-hold-button").addEventListener("click", () => {
    void runPanelOperation("gantry", "闭合 Z 制动", "/api/gantry/z-brake", {
      released: false,
    });
  });
  document.querySelector("#gantry-halt-button").addEventListener("click", () => {
    void runImmediatePanelAction("gantry", "立即停止", "/api/gantry/halt", {});
  });

  const heaterForm = document.querySelector("#heater-sv-form");
  heaterForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!heaterForm.reportValidity()) {
      return;
    }
    void runPanelOperation("heater", "设定 SV", "/api/heater/set-sv", {
      sv_c: formNumber(heaterForm, "sv_c"),
    });
  });

  const spincoaterForm = document.querySelector("#spincoater-start-form");
  spincoaterForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!spincoaterForm.reportValidity()) {
      return;
    }
    void runPanelOperation("spincoater", "启动", "/api/spincoater/start", {
      rpm: formNumber(spincoaterForm, "rpm"),
    });
  });

  const spincoaterAccelerationForm = document.querySelector("#spincoater-acceleration-form");
  spincoaterAccelerationForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!spincoaterAccelerationForm.reportValidity()) {
      return;
    }
    void runPanelOperation(
      "spincoater",
      "设置加速度",
      "/api/spincoater/acceleration",
      { rpm_per_s: formNumber(spincoaterAccelerationForm, "rpm_per_s") },
    );
  });

  const spincoaterDecelerationForm = document.querySelector("#spincoater-deceleration-form");
  spincoaterDecelerationForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!spincoaterDecelerationForm.reportValidity()) {
      return;
    }
    void runPanelOperation(
      "spincoater",
      "设置减速度",
      "/api/spincoater/deceleration",
      { rpm_per_s: formNumber(spincoaterDecelerationForm, "rpm_per_s") },
    );
  });

  document.querySelector("#spincoater-stop-button").addEventListener("click", () => {
    void runPanelOperation("spincoater", "停止", "/api/spincoater/stop", {
      use_brake: document.querySelector("#spincoater-brake").checked,
    });
  });
  document.querySelector("#spincoater-vacuum-on-button").addEventListener("click", () => {
    setPanelMessage("spincoater", "真空阀 CH3 ON 请求已提交");
    void runPanelOperation(
      "relay",
      "真空阀 CH3 ON",
      "/api/relay/ch",
      { channel: 3, on: true, force: true },
    );
  });
  document.querySelector("#spincoater-vacuum-off-button").addEventListener("click", () => {
    setPanelMessage("spincoater", "真空阀 CH3 OFF 请求已提交");
    void runPanelOperation(
      "relay",
      "真空阀 CH3 OFF",
      "/api/relay/ch",
      { channel: 3, on: false, force: true },
    );
  });

  const pipetteForm = document.querySelector("#pipette-volume-form");
  pipetteForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!pipetteForm.reportValidity()) {
      return;
    }
    const action = event.submitter && event.submitter.dataset.volumeAction
      ? event.submitter.dataset.volumeAction
      : "aspirate";
    const labels = { aspirate: "吸液", dispense: "排液" };
    void runPanelOperation(
      "pipette",
      labels[action] || action,
      `/api/pipette/${action}`,
      { volume_ul: formNumber(pipetteForm, "volume_ul") },
    );
  });

  const linearStageForm = document.querySelector("#linear-stage-move-form");
  linearStageForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!linearStageForm.reportValidity()) {
      return;
    }
    void runPanelOperation("linear_stage", "绝对移动", "/api/linearstage/move", {
      position_mm: formNumber(linearStageForm, "position_mm"),
    });
  });

  document.querySelector("#linear-stage-stop-button").addEventListener("click", () => {
    void runImmediatePanelAction(
      "linear_stage",
      "立即停止",
      "/api/linearstage/stop",
      {},
    );
  });

  for (const button of document.querySelectorAll("[data-relay-channel]")) {
    button.addEventListener("click", () => {
      const channel = Number(button.dataset.relayChannel);
      const on = button.dataset.relayOn === "true";
      void runPanelOperation(
        "relay",
        `CH${channel} ${on ? "ON" : "OFF"}`,
        "/api/relay/ch",
        { channel, on },
      );
    });
  }
}

function bindRoutineControls() {
  elements.routineRecordForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!elements.routineRecordForm.reportValidity()) {
      return;
    }
    const name = elements.routineNameInput.value.trim();
    if (!name) {
      return;
    }
    void runRoutineRecordingRequest(
      "开始录制",
      "/api/routines/record/arm",
      { name },
      (recording) => {
        renderRoutineRecordingStatus(recording);
      },
    );
  });

  elements.routineDisarmButton.addEventListener("click", () => {
    void runRoutineRecordingRequest(
      "停止录制并保存",
      "/api/routines/record/disarm",
      {},
      (payload) => {
        renderRoutineRecordingStatus(payload.recording);
        state.routineDeleteConfirmName = null;
        void refreshRoutineList();
      },
    );
  });

  elements.routineAbortButton.addEventListener("click", () => {
    void runImmediatePanelAction(
      "routine",
      "中止重放",
      "/api/routines/replay/abort",
      {},
      { eventKind: "重放中止" },
    );
  });
}

function bindRelayNotes() {
  for (const input of document.querySelectorAll("[data-relay-note]")) {
    const key = `${RELAY_NOTE_STORAGE_PREFIX}${input.dataset.relayNote}`;
    try {
      input.value = window.localStorage.getItem(key) || "";
    } catch {
      setPanelMessage("relay", "用途备注未读取。建议：检查浏览器本地存储权限。", {
        danger: true,
      });
    }
    input.addEventListener("input", () => {
      try {
        window.localStorage.setItem(key, input.value);
      } catch {
        setPanelMessage("relay", "用途备注未保存。建议：检查浏览器本地存储权限。", {
          danger: true,
        });
      }
    });
  }
}

elements.tokenInput.value = loadStoredToken();
elements.tokenInput.addEventListener("input", () => {
  persistToken(elements.tokenInput.value);
  if (state.tokenChangeTimer !== null) {
    window.clearTimeout(state.tokenChangeTimer);
  }
  state.tokenChangeTimer = window.setTimeout(() => {
    state.tokenChangeTimer = null;
    void loadRuntimeConfiguration().catch((error) => {
      setBuilderMessage(`运行配置读取失败：${error.message}`, true);
    });
    void loadExperimentBuilderDefaults();
    restartStatusStream();
  }, 300);
});
elements.estopButton.addEventListener("click", () => {
  void sendEstop();
});

bindDeviceControls();
bindRoutineControls();
bindRelayNotes();
bindBuilderControls();
for (const device of elements.devicePanels.keys()) {
  updatePanelControls(device);
}

window.setInterval(renderCurrentOperation, 250);
window.addEventListener("resize", renderLiveCharts);
window.addEventListener("beforeunload", () => {
  clearReconnectTimer();
  if (state.streamController) {
    state.streamController.abort();
  }
});

void checkServiceHealth();
void loadRuntimeConfiguration().catch((error) => {
  setBuilderMessage(`运行配置读取失败：${error.message}`, true);
});
void loadExperimentBuilderDefaults();
restartStatusStream();

export { api, apiEstop };
