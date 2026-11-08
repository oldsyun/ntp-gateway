const state = {
  token: sessionStorage.getItem("ntp_gateway_token") || "",
  status: null,
  ntp: null,
  ntpBaseline: "",
  network: null,
  serial: [],
  serialBaseline: "",
  schedule: null,
  system: null,
  systemPanel: "info",
  logs: [],
  logFilter: "all",
  logQuery: "",
  mustChange: false,
  clockEpochMs: null,
  clockPerfMs: null,
  clockDisplayOffsetMs: 0,
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4200);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers });
  let payload;
  try { payload = await response.json(); }
  catch { throw new Error(`HTTP ${response.status}`); }
  if (response.status === 401) {
    logout(false);
    throw new Error(payload.message || "登录已失效");
  }
  if (!response.ok || payload.code !== 0) {
    throw new Error(payload.message || `HTTP ${response.status}`);
  }
  return payload.data;
}

function showLogin() {
  $("login-view").hidden = false;
  $("app-view").hidden = true;
  $("password-modal").hidden = true;
}

function showApp() {
  $("login-view").hidden = true;
  $("app-view").hidden = false;
}

function logout(callApi = true) {
  if (callApi && state.token) {
    api("/api/v1/logout", { method: "POST" }).catch(() => {});
  }
  state.token = "";
  sessionStorage.removeItem("ntp_gateway_token");
  showLogin();
}

function ntpClass(ntp) {
  if (!ntp) return "";
  if (ntp.state === "TIME_SYNCED") return "ok";
  if (ntp.state === "TIME_HOLDOVER") return "warn";
  return "bad";
}

function stateText(value) {
  return {
    TIME_SYNCED: "已同步", TIME_HOLDOVER: "保持", TIME_INVALID: "时间无效",
    ok: "正常", error: "故障", timeout: "超时", disabled: "已禁用",
    waiting_ntp: "等待时间", sending: "发送中", starting: "启动中",
  }[value] || value || "--";
}

function updateDisplayClock() {
  if (state.clockEpochMs === null || state.clockPerfMs === null) return;
  const elapsed = performance.now() - state.clockPerfMs;
  const display = new Date(
    state.clockEpochMs + elapsed + state.clockDisplayOffsetMs
  );
  const pad = (value, size = 2) => String(value).padStart(size, "0");
  const text = `${display.getUTCFullYear()}-${pad(display.getUTCMonth() + 1)}-${pad(display.getUTCDate())} `
    + `${pad(display.getUTCHours())}:${pad(display.getUTCMinutes())}:${pad(display.getUTCSeconds())}`
    + `.${pad(display.getUTCMilliseconds(), 3)}`;
  $("header-time").textContent = text;
  $("metric-time").textContent = text;
}

function renderStatus() {
  const status = state.status;
  if (!status) return;
  if (!status.system || !status.time || !status.ntp || !status.network) {
    throw new Error("状态数据不完整，请刷新页面");
  }
  const localTime = new Date(status.time.local);
  if (!Number.isNaN(localTime.getTime()) && Number.isFinite(Number(status.time.epoch))) {
    const localAsUtc = Date.UTC(
      localTime.getFullYear(),
      localTime.getMonth(),
      localTime.getDate(),
      localTime.getHours(),
      localTime.getMinutes(),
      localTime.getSeconds(),
      localTime.getMilliseconds()
    );
    state.clockEpochMs = Number(status.time.epoch) * 1000;
    state.clockPerfMs = performance.now();
    state.clockDisplayOffsetMs = localAsUtc - state.clockEpochMs;
    updateDisplayClock();
  }
  const pill = $("ntp-pill");
  pill.textContent = stateText(status.ntp.state);
  pill.className = `pill ${ntpClass(status.ntp)}`;
  $("metric-state").textContent = stateText(status.ntp.state);
  $("metric-state").className = ntpClass(status.ntp);
  $("metric-offset").textContent = status.ntp.last_sync
    ? `${status.ntp.offset_sec.toFixed(4)} s`
    : "--";
  $("metric-offset-detail").textContent = status.ntp.last_sync
    ? `网络延迟 ${(status.ntp.delay_sec * 1000).toFixed(1)} ms`
    : "等待同步";
  $("metric-server").textContent = status.ntp.server || "--";
  $("port-overview").innerHTML = (status.ports || []).map((port) => {
    const cfg = port?.config || {};
    const cls = port.state === "ok" ? "ok" : (port.enabled ? "error" : "warn");
    return `<article class="port-card ${port.enabled ? "is-enabled" : ""}">
      <div class="port-head">
        <div class="port-name"><span class="dot ${cls}"></span><h3>${escapeHtml(port.name)}</h3></div>
        <span class="port-state">${escapeHtml(stateText(port.state))}</span>
      </div>
      <div class="port-device">${escapeHtml(cfg.device || "--")}</div>
      <dl>
        <div><dt>模式</dt><dd>全对时（毫秒=0）</dd></div>
        <div><dt>成功 / 失败</dt><dd>${port.success_count} / ${port.error_count}</dd></div>
        <div><dt>下次执行</dt><dd>${escapeHtml(port.next_due || "--")}</dd></div>
      </dl>
    </article>`;
  }).join("");
  const interfaces = (status.network.interfaces || []).filter((iface) => iface.ip);
  $("network-info").innerHTML = interfaces.map((iface) => {
    const state = !iface.up ? "bad" : (iface.carrier ? "ok" : "warn");
    return `<div class="iface-item">
    <span class="iface-state">
      <i class="iface-dot ${state}" role="img" aria-label="${state === "ok" ? "已激活" : state === "warn" ? "无载波" : "未启用"}"></i>
      <span class="iface-name">${escapeHtml(iface.name)}</span>
    </span>
    <span class="iface-address">${escapeHtml(iface.ip)}/${escapeHtml(iface.prefix)}</span>
  </div>`;
  }).join("") || '<div class="empty-state">未获取到网卡地址</div>';
  $("ntp-status").textContent = JSON.stringify(status.ntp, null, 2);
  renderNetworkPending(status.network.pending);
}

async function refreshStatus() {
  if (!state.token) return;
  try {
    state.status = await api("/api/v1/status");
    renderStatus();
  } catch (error) {
    if (state.token) toast(error.message, true);
  }
}

async function loadNtp() {
  const data = await api("/api/v1/ntp");
  if (!data || !data.config) throw new Error("NTP 配置数据无效，请刷新页面");
  state.ntp = data.config;
  $("ntp-servers").value = (data.config.servers || []).join("\n");
  $("ntp-interval").value = data.config.sync_interval_sec;
  $("ntp-timeout").value = data.config.timeout_ms;
  $("ntp-max-age").value = data.config.max_age_sec;
  $("ntp-holdover").value = data.config.holdover_max_sec;
  $("ntp-slew-threshold").value = (Number(data.config.slew_threshold_sec || 0.001) * 1000).toFixed(1);
  $("ntp-step-threshold").value = data.config.step_threshold_sec;
  $("ntp-enabled").checked = Boolean(data.config.enabled);
  $("ntp-auto-compensate").checked = Boolean(data.config.auto_compensate);
  $("ntp-allow-holdover").checked = Boolean(data.config.allow_holdover);
  $("ntp-status").textContent = JSON.stringify(data.status, null, 2);
}

function collectNtpDraft() {
  return {
    enabled: $("ntp-enabled").checked,
    servers: $("ntp-servers").value,
    sync_interval_sec: $("ntp-interval").value,
    timeout_ms: $("ntp-timeout").value,
    max_age_sec: $("ntp-max-age").value,
    holdover_max_sec: $("ntp-holdover").value,
    auto_compensate: $("ntp-auto-compensate").checked,
    slew_threshold_sec: $("ntp-slew-threshold").value,
    step_threshold_sec: $("ntp-step-threshold").value,
    allow_holdover: $("ntp-allow-holdover").checked,
    retry_count: $("schedule-retry").value,
    retry_interval_sec: $("schedule-retry-interval").value,
    ntp_required: $("schedule-ntp-required").checked,
  };
}

function updateNtpDirty() {
  $("ntp-save").disabled = JSON.stringify(collectNtpDraft()) === state.ntpBaseline;
}

function setNtpBaseline() {
  state.ntpBaseline = JSON.stringify(collectNtpDraft());
  updateNtpDirty();
}

async function loadSchedule() {
  const data = await api("/api/v1/schedule");
  state.schedule = data;
  $("schedule-retry").value = data.retry_count;
  $("schedule-retry-interval").value = data.retry_interval_sec;
  $("schedule-ntp-required").checked = Boolean(data.ntp_required);
}

function renderNetworkPending(pending) {
  const container = $("network-pending");
  if (!pending) {
    container.innerHTML = "";
    return;
  }
  const oldIp = pending.old.method === "dhcp" ? "DHCP" : `${pending.old.ip}/${pending.old.prefix}`;
  const newIp = pending.new.method === "dhcp" ? "DHCP" : `${pending.new.ip}/${pending.new.prefix}`;
  container.innerHTML = `<div class="pending-card">
    <div>
      <strong>${escapeHtml(pending.interface)} 配置等待确认</strong>
      <p>${escapeHtml(oldIp)} → ${escapeHtml(newIp)}，剩余 <b id="pending-countdown">${pending.remaining_sec}</b> 秒自动回滚。</p>
    </div>
    <div class="button-row">
      <button class="primary" data-network-pending="confirm">确认保留</button>
      <button class="secondary" data-network-pending="rollback">立即回滚</button>
    </div>
  </div>`;
  if (state.pendingTicker) clearInterval(state.pendingTicker);
  state.pendingTicker = setInterval(() => {
    const el = $("pending-countdown");
    if (!el) { clearInterval(state.pendingTicker); return; }
    const value = Math.max(0, Number(el.textContent) - 1);
    el.textContent = String(value);
    if (value === 0) {
      clearInterval(state.pendingTicker);
      refreshStatus().then(loadNetwork).catch(() => {});
    }
  }, 1000);
}

function renderNetwork() {
  const data = state.network;
  if (!data) return;
  const dns = (data.dns || []).join("\n");
  $("network-forms").innerHTML = (data.interfaces || []).map((iface) => {
    const method = iface.method || iface.persistent?.method || "static";
    const ip = iface.ip || iface.persistent?.ip || "";
    const netmask = iface.netmask || iface.persistent?.netmask || "255.255.255.0";
    const gateway = iface.gateway || iface.persistent?.gateway || "";
    return `<article class="serial-card network-card" data-iface="${escapeHtml(iface.name)}">
      <div class="serial-head">
        <div class="serial-title">
          <h3>${escapeHtml(iface.name)} <span class="muted">${escapeHtml(iface.mac || "")}</span></h3>
          <span class="pill ${iface.up ? (iface.carrier ? "ok" : "warn") : "bad"}">${iface.up ? (iface.carrier ? "UP" : "NO CARRIER") : "DOWN"}</span>
          <span class="network-current">当前地址 <b>${escapeHtml(ip || "--")}</b></span>
        </div>
      </div>
      <div class="serial-grid network-fields">
        <label>配置方式<select data-network-field="method">
          <option value="static" ${method === "static" ? "selected" : ""}>静态 IP</option>
          <option value="dhcp" ${method === "dhcp" ? "selected" : ""}>DHCP</option>
        </select></label>
        <label>IP 地址<input data-network-field="ip" data-static-field value="${escapeHtml(ip)}" placeholder="192.168.50.138"></label>
        <label>子网掩码<input data-network-field="netmask" data-static-field value="${escapeHtml(netmask)}" placeholder="255.255.255.0"></label>
        <label>默认网关<input data-network-field="gateway" data-static-field value="${escapeHtml(gateway)}" placeholder="192.168.50.1"></label>
      </div>
      <label class="wide network-dns">DNS 服务器（每行一个）
        <textarea data-network-field="dns" rows="2">${escapeHtml(dns)}</textarea>
      </label>
      <div class="serial-foot">
        <button class="primary" data-network-action="stage" disabled>应用配置</button>
      </div>
    </article>`;
  }).join("");
  toggleNetworkFields();
  document.querySelectorAll(".network-card").forEach((card) => {
    card.dataset.baseline = JSON.stringify(collectNetwork(card));
    updateNetworkDirty(card);
  });
}

function updateNetworkDirty(card) {
  const button = card.querySelector('[data-network-action="stage"]');
  button.disabled = JSON.stringify(collectNetwork(card)) === card.dataset.baseline;
}

function toggleNetworkFields() {
  document.querySelectorAll(".network-card").forEach((card) => {
    const dhcp = card.querySelector('[data-network-field="method"]').value === "dhcp";
    card.querySelectorAll("[data-static-field]").forEach((input) => {
      input.disabled = dhcp;
    });
  });
}

async function loadNetwork() {
  state.network = await api("/api/v1/network");
  renderNetwork();
  renderNetworkPending(state.network.pending);
}

function collectNetwork(card) {
  const value = (field) => card.querySelector(`[data-network-field="${field}"]`).value;
  return {
    interface: card.dataset.iface,
    method: value("method"),
    ip: value("ip").trim(),
    netmask: value("netmask").trim(),
    gateway: value("gateway").trim(),
    dns: value("dns").split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
  };
}

function parityOptions(selected) {
  return [["N", "无校验"], ["E", "偶校验"], ["O", "奇校验"]]
    .map(([value, label]) => `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`)
    .join("");
}

const intervalPresets = [
  [10, "10 秒"], [30, "30 秒"], [60, "1 分钟"], [300, "5 分钟"],
  [600, "10 分钟"], [1800, "30 分钟"], [3600, "1 小时"],
  [7200, "2 小时"], [86400, "24 小时"],
];

function renderSerial(ports) {
  state.serial = ports;
  const enabledCount = ports.filter((port) => port.config?.enabled).length;
  $("serial-summary").textContent = `${ports.length} 个通道 · ${enabledCount} 个启用`;
  $("serial-forms").innerHTML = ports.map((port) => {
    const cfg = port.config;
    if (!cfg) throw new Error("串口配置数据不完整，请刷新 RS485 页面");
    const mode = cfg.schedule_mode || "interval";
    const presetMatched = intervalPresets.some(([value]) => Number(value) === Number(cfg.interval_sec));
    const statusClass = !cfg.enabled ? "neutral" : (port.state === "ok" ? "ok" : "bad");
    return `<article class="serial-card ${cfg.enabled ? "is-enabled" : "is-disabled"}" data-port="${port.id}">
      <div class="serial-head">
        <div class="serial-title">
          <h3>${escapeHtml(cfg.name)} <span class="muted">${escapeHtml(cfg.device)}</span></h3>
          <span class="pill ${statusClass}">${escapeHtml(stateText(port.state))}</span>
        </div>
        <div class="serial-readout">
          <span><small>成功</small><b>${port.success_count}</b></span>
          <span><small>失败</small><b>${port.error_count}</b></span>
          <span><small>下次执行</small><b>${escapeHtml(port.next_due || "--")}</b></span>
        </div>
        <label class="check serial-enable"><input data-field="enabled" type="checkbox" ${cfg.enabled ? "checked" : ""}> 启用</label>
      </div>
      <div class="serial-block">
        <span class="block-title">串口参数</span>
        <div class="serial-fields cols-3">
          <label>波特率<select data-field="baudrate">${[1200,2400,4800,9600,19200,38400,57600,115200].map((v) => `<option ${v === cfg.baudrate ? "selected" : ""}>${v}</option>`).join("")}</select></label>
          <label>校验<select data-field="parity">${parityOptions(cfg.parity)}</select></label>
          <label>从站地址<input data-field="slave_addr" type="number" min="0" max="247" value="${cfg.slave_addr}"></label>
        </div>
      </div>
      <div class="serial-block">
        <span class="block-title">调度设置</span>
        <div class="schedule-fields">
          <label>调度方式<select data-field="schedule_mode">
            <option value="interval" ${mode === "interval" ? "selected" : ""}>按秒间隔</option>
            <option value="daily" ${mode === "daily" ? "selected" : ""}>每天定点</option>
          </select></label>
          <div class="schedule-detail interval-detail" data-schedule-interval>
            <label>常用周期<select data-interval-preset>
              <option value="" ${presetMatched ? "" : "selected"}>自定义</option>
              ${intervalPresets.map(([value, label]) => `<option value="${value}" ${Number(value) === Number(cfg.interval_sec) ? "selected" : ""}>${label}</option>`).join("")}
            </select></label>
            <label data-schedule-custom>自定义周期（秒）<input data-field="interval_sec" type="number" min="1" max="86400" value="${cfg.interval_sec}"></label>
          </div>
          <div class="schedule-detail daily-detail" data-schedule-daily>
            <label>每天发送时间<input data-field="daily_time" type="time" step="1" value="${escapeHtml(cfg.daily_time || "00:00:00")}"></label>
          </div>
        </div>
      </div>
      <div class="serial-block">
        <span class="block-title">通信选项</span>
        <div class="serial-fields cols-3">
          <label class="check"><input data-field="broadcast" type="checkbox" ${cfg.broadcast ? "checked" : ""}> 广播地址 0</label>
          <label class="check"><input data-field="wait_response" type="checkbox" ${cfg.wait_response ? "checked" : ""}> 等待应答</label>
          <label>应答超时（ms）<input data-field="response_timeout_ms" type="number" min="50" max="10000" value="${cfg.response_timeout_ms}"></label>
        </div>
      </div>
      <div class="serial-foot">
        <button class="secondary" data-action="test">测试发送</button>
        <button class="secondary" data-action="sync">手动对时</button>
        <button class="ghost" data-action="status">刷新状态</button>
      </div>
    </article>`;
  }).join("");
  toggleSerialScheduleFields();
  state.serialBaseline = JSON.stringify(collectSerial());
  updateSerialDirty();
}

function updateSerialDirty() {
  if (!state.serial.length) return;
  const dirty = JSON.stringify(collectSerial()) !== state.serialBaseline;
  document.querySelectorAll("[data-serial-save]").forEach((button) => {
    button.disabled = !dirty;
  });
}

function toggleSerialScheduleFields() {
  document.querySelectorAll("#serial-forms .serial-card[data-port]").forEach((card) => {
    const modeField = card.querySelector('[data-field="schedule_mode"]');
    const presetField = card.querySelector("[data-interval-preset]");
    if (!modeField || !presetField) return;
    const mode = modeField.value;
    const preset = presetField.value;
    card.querySelectorAll("[data-schedule-interval]").forEach((field) => {
      field.hidden = mode !== "interval";
    });
    card.querySelectorAll("[data-schedule-daily]").forEach((field) => {
      field.hidden = mode !== "daily";
    });
    card.querySelectorAll("[data-schedule-custom]").forEach((field) => {
      field.hidden = mode !== "interval" || Boolean(preset);
    });
  });
}

async function loadSerial() {
  renderSerial(await api("/api/v1/serial"));
}

function collectSerial() {
  return [...document.querySelectorAll("#serial-forms .serial-card[data-port]")].map((card) => {
    const id = Number(card.dataset.port);
    const entry = state.serial.find((item) => item.id === id);
    if (!entry || !entry.config) {
      throw new Error("串口配置尚未加载完成，请刷新 RS485 页面后重试");
    }
    const base = entry.config;
    const result = JSON.parse(JSON.stringify(base));
    card.querySelectorAll("[data-field]").forEach((input) => {
      const key = input.dataset.field;
      if (input.type === "checkbox") result[key] = input.checked;
      else if (["slave_addr", "interval_sec", "response_timeout_ms", "baudrate"].includes(key)) result[key] = Number(input.value);
      else result[key] = input.value;
    });
    if (result.broadcast) result.slave_addr = 0;
    return result;
  });
}

function parseLogLines(raw) {
  return String(raw || "").split(/\r?\n/).filter(Boolean).map((line) => {
    const match = line.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\S+)\s+(.*)$/);
    if (!match) return { timestamp: "", level: "", className: "raw", severity: "info", message: line };
    const [, timestamp, level, message] = match;
    const name = level.toLowerCase();
    const severity = /^(error|critical|fatal)$/.test(name) ? "error"
      : /^(warn|warning)$/.test(name) ? "warn" : "info";
    return {
      timestamp,
      level,
      className: name.replace(/[^a-z]/g, ""),
      severity,
      message,
    };
  });
}

function renderLogs() {
  const entries = state.logs || [];
  const query = state.logQuery.trim().toLowerCase();
  const visible = entries.filter((entry) => {
    if (state.logFilter === "warn" && entry.severity !== "warn") return false;
    if (state.logFilter === "error" && entry.severity !== "error") return false;
    return !query || entry.message.toLowerCase().includes(query) || entry.level.toLowerCase().includes(query);
  });
  const counts = entries.reduce((acc, entry) => {
    if (entry.severity === "warn") acc.warn += 1;
    if (entry.severity === "error") acc.error += 1;
    return acc;
  }, { warn: 0, error: 0 });
  document.querySelectorAll("[data-log-count]").forEach((el) => {
    el.textContent = String(counts[el.dataset.logCount] || 0);
  });
  const total = entries.filter((entry) => entry.className !== "raw").length;
  $("log-count").textContent = `${visible.length} / ${total} 行`;
  $("log-output").innerHTML = visible.map((entry) => {
    if (entry.className === "raw") {
      return `<div class="log-row raw"><span class="log-message">${escapeHtml(entry.message)}</span></div>`;
    }
    return `<div class="log-row sev-${entry.severity}">
      <time class="log-time">${escapeHtml(entry.timestamp)}</time>
      <span class="log-level ${escapeHtml(entry.className)}">${escapeHtml(entry.level)}</span>
      <span class="log-message">${escapeHtml(entry.message)}</span>
    </div>`;
  }).join("") || '<div class="log-row raw"><span class="log-message">没有匹配的日志</span></div>';
}

async function loadLogs() {
  const data = await api("/api/v1/logs?lines=220");
  state.logs = parseLogLines(data.lines);
  renderLogs();
  if (state.logFilter === "all" && !state.logQuery) {
    $("log-output").scrollTop = $("log-output").scrollHeight;
  }
}

async function loadSystem() {
  state.system = await api("/api/v1/system/info");
  const info = state.system || {};
  const duration = (seconds) => {
    const total = Math.max(0, Number(seconds) || 0);
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    return `${days} 天 ${hours} 小时 ${minutes} 分`;
  };
  const rows = [
    { key: "version", label: "应用版本", value: info.app_version },
    { key: "hostname", label: "主机名", value: info.hostname },
    { key: "platform", label: "系统平台", value: info.platform },
    { key: "machine", label: "硬件架构", value: info.machine },
    { key: "python", label: "Python 版本", value: info.python },
    { key: "http", label: "HTTP 端口", value: info.http_port },
    { key: "pid", label: "进程 PID", value: info.pid },
    { key: "uid", label: "运行用户 UID", value: info.uid },
    { key: "uptime", label: "系统运行时长", value: duration(info.uptime_sec) },
    { key: "service", label: "服务运行时长", value: duration(info.service_uptime_sec) },
    { key: "password", label: "初始密码文件", value: info.initial_password_file },
  ];
  $("system-info").innerHTML = `<div class="system-grid">${rows.map((row) => `
    <div class="system-item system-${row.key}">
      <span>${escapeHtml(row.label)}</span>
      <strong>${escapeHtml(row.value ?? "--")}</strong>
    </div>`).join("")}</div>`;
}

async function activateTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${name}`));
  try {
    if (name === "ntp") { await loadNtp(); await loadSchedule(); setNtpBaseline(); }
    if (name === "network") await loadNetwork();
    if (name === "serial") await loadSerial();
    if (name === "system") { await loadSystem(); await activateSystemPanel(state.systemPanel); }
    if (name === "overview") await refreshStatus();
  } catch (error) { toast(error.message, true); }
}

async function activateSystemPanel(name) {
  state.systemPanel = name;
  document.querySelectorAll("[data-system-panel]").forEach((button) => {
    const active = button.dataset.systemPanel === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".system-panel").forEach((panel) => {
    const active = panel.id === `system-panel-${name}`;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
  try {
    if (name === "logs" && !state.logs.length) await loadLogs();
  } catch (error) { toast(error.message, true); }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-error").textContent = "";
  $("login-button").disabled = true;
  try {
    const data = await api("/api/v1/login", {
      method: "POST",
      body: { username: $("login-user").value, password: $("login-password").value },
    });
    state.token = data.token;
    sessionStorage.setItem("ntp_gateway_token", state.token);
    state.mustChange = Boolean(data.must_change_password);
    showApp();
    await refreshStatus();
    if (state.mustChange) $("password-modal").hidden = false;
  } catch (error) {
    $("login-error").textContent = error.message;
  } finally {
    $("login-button").disabled = false;
  }
});

$("logout-button").addEventListener("click", () => logout(true));
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => activateTab(tab.dataset.tab)));
document.querySelectorAll("[data-system-panel]").forEach((button) => {
  button.addEventListener("click", () => activateSystemPanel(button.dataset.systemPanel));
});

$("ntp-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("ntp-save");
  button.disabled = true;
  try {
    const servers = $("ntp-servers").value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean);
    await api("/api/v1/ntp", { method: "POST", body: {
      enabled: $("ntp-enabled").checked,
      servers,
      sync_interval_sec: Number($("ntp-interval").value),
      timeout_ms: Number($("ntp-timeout").value),
      max_age_sec: Number($("ntp-max-age").value),
      holdover_max_sec: Number($("ntp-holdover").value),
      auto_compensate: $("ntp-auto-compensate").checked,
      slew_threshold_sec: Number($("ntp-slew-threshold").value) / 1000,
      step_threshold_sec: Number($("ntp-step-threshold").value),
      allow_holdover: $("ntp-allow-holdover").checked,
    }});
    await api("/api/v1/schedule", { method: "POST", body: {
      retry_count: Number($("schedule-retry").value),
      retry_interval_sec: Number($("schedule-retry-interval").value),
      ntp_required: $("schedule-ntp-required").checked,
    }});
    toast("NTP 配置与调度策略已保存");
    await loadNtp();
    await loadSchedule();
    setNtpBaseline();
  } catch (error) {
    toast(error.message, true);
    updateNtpDirty();
  }
});

$("ntp-form").addEventListener("input", () => updateNtpDirty());
$("ntp-form").addEventListener("change", () => updateNtpDirty());

$("ntp-sync-button").addEventListener("click", async () => {
  try {
    await api("/api/v1/ntp/sync", { method: "POST" });
    toast("NTP 手动同步完成");
    await loadNtp();
    await refreshStatus();
  } catch (error) { toast(error.message, true); }
});

$("network-forms").addEventListener("input", (event) => {
  const card = event.target.closest(".network-card");
  if (card) updateNetworkDirty(card);
});

$("network-forms").addEventListener("change", (event) => {
  if (event.target.matches('[data-network-field="method"]')) toggleNetworkFields();
  const card = event.target.closest(".network-card");
  if (card) updateNetworkDirty(card);
});

$("network-forms").addEventListener("click", async (event) => {
  const button = event.target.closest('[data-network-action="stage"]');
  if (!button) return;
  const card = button.closest(".network-card");
  const payload = collectNetwork(card);
  const description = payload.method === "dhcp"
    ? `${payload.interface} 切换到 DHCP`
    : `${payload.interface} 临时添加 ${payload.ip}/${payload.netmask}`;
  if (!confirm(`确认${description}？新配置需在 180 秒内确认，否则自动回滚。`)) return;
  button.disabled = true;
  try {
    await api("/api/v1/network/stage", { method: "POST", body: payload });
    toast("网络配置已临时应用，请在倒计时结束前确认");
    await loadNetwork();
    await refreshStatus();
  } catch (error) {
    toast(error.message, true);
  } finally {
    updateNetworkDirty(card);
  }
});

$("network-pending").addEventListener("click", async (event) => {
  const action = event.target.closest("[data-network-pending]")?.dataset.networkPending;
  if (!action) return;
  const pending = state.network?.pending || state.status?.network?.pending;
  if (!pending) return;
  if (action === "confirm" && !confirm("确认保留新网络配置？旧地址将随后移除。")) return;
  if (action === "rollback" && !confirm("确认立即回滚网络配置？")) return;
  try {
    await api(`/api/v1/network/${action}`, {
      method: "POST",
      body: { change_id: pending.change_id },
    });
    toast(action === "confirm" ? "网络配置已确认" : "网络配置已回滚");
    await loadNetwork();
    await refreshStatus();
  } catch (error) {
    toast(error.message, true);
  }
});

document.querySelectorAll("[data-serial-save]").forEach((button) => {
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api("/api/v1/serial", { method: "POST", body: { ports: collectSerial() } });
      toast("串口配置已保存");
      await loadSerial();
    } catch (error) {
      toast(error.message, true);
      updateSerialDirty();
    }
  });
});

$("serial-forms").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const card = button.closest(".serial-card");
  const port = card.dataset.port;
  const action = button.dataset.action;
  if (action === "status") { await loadSerial().catch((error) => toast(error.message, true)); return; }
  if (action === "test" && !confirm(`确认向 COM${port} 现场总线发送测试对时报文？`)) return;
  button.disabled = true;
  try {
    const result = await api(`/api/v1/serial/${port}/${action}`, { method: "POST" });
    if (result.ok) toast(`COM${port} ${action === "test" ? "测试发送" : "手动对时"}成功`);
    else toast(`COM${port} 失败：${result.error}`, true);
    await loadSerial();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

$("serial-forms").addEventListener("change", (event) => {
  if (event.target.matches('[data-field="schedule_mode"]')) {
    toggleSerialScheduleFields();
  }
  if (event.target.matches("[data-interval-preset]") && event.target.value) {
    const card = event.target.closest(".serial-card");
    card.querySelector('[data-field="interval_sec"]').value = event.target.value;
  }
  if (event.target.matches("[data-interval-preset]")) {
    toggleSerialScheduleFields();
  }
  updateSerialDirty();
});

$("serial-forms").addEventListener("input", () => updateSerialDirty());

document.querySelectorAll("[data-log-level]").forEach((button) => {
  button.addEventListener("click", () => {
    state.logFilter = button.dataset.logLevel;
    document.querySelectorAll("[data-log-level]").forEach((item) => {
      item.classList.toggle("active", item === button);
      item.setAttribute("aria-pressed", item === button ? "true" : "false");
    });
    renderLogs();
  });
});
$("log-search").addEventListener("input", (event) => {
  state.logQuery = event.target.value;
  renderLogs();
});
$("logs-refresh").addEventListener("click", () => loadLogs().catch((error) => toast(error.message, true)));
$("logs-export").addEventListener("click", async () => {
  try {
    const data = await api("/api/v1/logs?lines=500");
    const blob = new Blob([data.lines], { type: "text/plain;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `ntp-gateway-${new Date().toISOString().slice(0, 10)}.log`;
    link.click();
    URL.revokeObjectURL(link.href);
  } catch (error) { toast(error.message, true); }
});

async function changePassword(oldPassword, newPassword, confirmPassword) {
  if (newPassword !== confirmPassword) throw new Error("两次输入的新密码不一致");
  await api("/api/v1/account/password", {
    method: "POST",
    body: { old_password: oldPassword, new_password: newPassword },
  });
}

$("password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await changePassword($("password-old").value, $("password-new").value, $("password-confirm").value);
    toast("密码已修改，请重新登录");
    setTimeout(() => logout(false), 700);
  } catch (error) { toast(error.message, true); }
});

$("forced-password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await changePassword($("forced-old").value, $("forced-new").value, $("forced-confirm").value);
    toast("密码已修改，请使用新密码登录");
    state.mustChange = false;
    setTimeout(() => logout(false), 700);
  } catch (error) { toast(error.message, true); }
});

$("config-export").addEventListener("click", async () => {
  try {
    const data = await api("/api/v1/config/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "ntp-gateway-config.json";
    link.click();
    URL.revokeObjectURL(link.href);
  } catch (error) { toast(error.message, true); }
});

$("config-import").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    const config = JSON.parse(await file.text());
    await api("/api/v1/config/import", { method: "POST", body: { config } });
    toast("配置已导入");
    await refreshStatus();
  } catch (error) { toast(error.message, true); }
  event.target.value = "";
});

$("reboot-button").addEventListener("click", async () => {
  if (!confirm("确认重启设备？Web 和串口服务会短暂中断。")) return;
  if (!confirm("再次确认：现在重启 NTP 对时网关？")) return;
  try {
    await api("/api/v1/system/reboot", { method: "POST", body: { confirm: true } });
    toast("设备正在重启");
  } catch (error) { toast(error.message, true); }
});

async function boot() {
  if (!state.token) { showLogin(); return; }
  try {
    showApp();
    await refreshStatus();
    setInterval(refreshStatus, 3000);
  } catch {
    showLogin();
  }
}

window.addEventListener("unhandledrejection", (event) => {
  const message = event.reason?.message || "页面操作失败";
  toast(message, true);
});

window.addEventListener("error", (event) => {
  if (event.message) toast(event.message, true);
});

setInterval(updateDisplayClock, 100);
boot();
