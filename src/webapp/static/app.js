const TOKEN_STORAGE_KEY = "spincoater.web.token";
const RELAY_NOTE_STORAGE_PREFIX = "spincoater.web.relay-note.";
const RECONNECT_MAX_MS = 8000;
const EVENT_LIMIT = 100;
const REQUESTING_OPERATION = "requesting";

const DEVICE_LABELS = {
  gantry: "龙门架",
  heater: "加热台",
  spincoater: "旋涂",
  pipette: "移液",
  linear_stage: "滑台",
  relay: "继电器",
  gripper: "夹爪",
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
  eventCount: document.querySelector("#event-count"),
  eventLog: document.querySelector("#event-log"),
  devicePanels: new Map(
    [...document.querySelectorAll("[data-device]")].map((panel) => [
      panel.dataset.device,
      panel,
    ]),
  ),
};

const state = {
  events: [],
  currentOperation: null,
  operationConflict: null,
  operationRequestInFlight: false,
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
};

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
  return fallback;
}

async function api(path, body) {
  const hasBody = body !== undefined;
  let response;
  try {
    response = await fetch(path, {
      method: hasBody ? "POST" : "GET",
      headers: authorizationHeaders({ json: hasBody }),
      body: hasBody ? JSON.stringify(body) : undefined,
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
  const message = panel ? panel.querySelector('[data-role="message"]') : null;
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
  copy.classList.toggle("is-danger", mode === "danger");
  copy.textContent = text;
}

function renderGantrySnapshot(snapshot) {
  setDeviceConnection("gantry", snapshot);
  const position = snapshotRecord(snapshot && snapshot.position);
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
    danger: machineState === "alarm",
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

  state.gantryMachineState = machineState;
  updatePanelControls("gantry");
}

function renderHeaterSnapshot(snapshot) {
  setDeviceConnection("heater", snapshot, "connected");
  setReadout("heater-pv", formatNumber(snapshot && snapshot.pv_c, 1, "℃"));
  setReadout("heater-sv", formatNumber(snapshot && snapshot.sv_c, 1, "℃"));
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
}

function updatePanelControls(device) {
  const panel = panelForDevice(device);
  if (!panel) {
    return;
  }
  const pending = state.panelPending.has(device);
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
}

function setPanelPending(device, operationId) {
  const panel = panelForDevice(device);
  if (!panel) {
    return;
  }
  state.panelPending.set(device, operationId);
  panel.dataset.pending = "true";
  const output = panel.querySelector('[data-role="operation-id"]');
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
  const output = panel.querySelector('[data-role="operation-id"]');
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
  clearPanelPending(operation.device);
  const elapsed = Number(operation.elapsed) || 0;
  if (operation.status === "failed") {
    setPanelMessage(
      operation.device,
      structuredErrorMessage(operation.error, `${operationName(operation)} 失败`),
      { danger: true },
    );
    return;
  }
  setPanelMessage(
    operation.device,
    `${operationName(operation)} 已完成 · ${elapsed.toFixed(1)} s`,
    { success: true },
  );
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

async function runImmediatePanelAction(device, label, path, body) {
  setPanelMessage(device, `${label}请求已直达`);
  try {
    await api(path, body);
    const pendingSuffix = state.panelPending.has(device)
      ? "；原 operation 等待 SSE 收尾"
      : "";
    setPanelMessage(device, `${label}命令已完成${pendingSuffix}`, { success: true });
    logEvent("立即停止", `${DEVICE_LABELS[device] || device} · ${label}`);
  } catch (error) {
    handlePanelError(device, label, error);
  }
}

function formNumber(form, name) {
  const input = form.elements.namedItem(name);
  return input instanceof HTMLInputElement ? input.valueAsNumber : Number.NaN;
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
  if (state.operationRequestInFlight) {
    return;
  }
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

  document.querySelector("#spincoater-stop-button").addEventListener("click", () => {
    void runPanelOperation("spincoater", "停止", "/api/spincoater/stop", {
      use_brake: document.querySelector("#spincoater-brake").checked,
    });
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
    restartStatusStream();
  }, 300);
});
elements.estopButton.addEventListener("click", () => {
  void sendEstop();
});

bindDeviceControls();
bindRelayNotes();
for (const device of elements.devicePanels.keys()) {
  updatePanelControls(device);
}

window.setInterval(renderCurrentOperation, 250);
window.addEventListener("beforeunload", () => {
  clearReconnectTimer();
  if (state.streamController) {
    state.streamController.abort();
  }
});

void checkServiceHealth();
restartStatusStream();

export { api, apiEstop };
