const TOKEN_STORAGE_KEY = "spincoater.web.token";
const RECONNECT_MAX_MS = 8000;
const EVENT_LIMIT = 100;

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
};

const state = {
  events: [],
  currentOperation: null,
  operationRequestInFlight: false,
  startedOperationIds: new Set(),
  completedOperationIds: new Set(),
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

function errorMessage(payload, fallback) {
  const detail = payload && typeof payload === "object" ? payload.error : null;
  if (detail && typeof detail.human_message === "string") {
    return detail.human_message;
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

function operationName(operation) {
  return `${operation.device} · ${operation.action}`;
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
    const detail = operation.error && operation.error.human_message
      ? `：${operation.error.human_message}`
      : "";
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
  if (snapshot.last_operation) {
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
