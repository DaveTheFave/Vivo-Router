/* ═══════════════════════════════════════════════════════════════════════════
   Vivo-Router Dashboard — JavaScript
   Polls /api/stats every 5 seconds and updates the DOM.
   ════════════════════════════════════════════════════════════════════════════ */

"use strict";

const REFRESH_MS = 5000; // poll interval
let errorLines = [];     // cached error log lines

// ── clock ─────────────────────────────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  document.getElementById("clock").textContent = now.toLocaleTimeString();
}
setInterval(updateClock, 1000);
updateClock();

// ── number formatters ─────────────────────────────────────────────────────────
function fmtNum(n) {
  if (n === null || n === undefined) return "–";
  return Number(n).toLocaleString();
}

function fmtPct(n) {
  if (n === null || n === undefined) return "–";
  return n + "%";
}

function fmtLease(secs) {
  if (!secs || secs < 0) return "static";
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h > 0) return h + "h " + m + "m";
  return m + "m";
}

function fmtDuration(totalSecs) {
  const secs = Math.max(0, Math.floor(Number(totalSecs) || 0));
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function fmtTimestamp(isoTs) {
  if (!isoTs) return "—";
  const dt = new Date(isoTs);
  if (Number.isNaN(dt.getTime())) return "—";
  return dt.toLocaleString();
}

// ── DOM helpers ───────────────────────────────────────────────────────────────
function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function setBar(id, pct) {
  const el = document.getElementById(id);
  if (!el) return;
  const p = Math.min(Math.max(pct || 0, 0), 100);
  el.style.width = p + "%";
  el.classList.toggle("warn", p >= 70 && p < 90);
  el.classList.toggle("crit", p >= 90);
}

// ── main fetch ────────────────────────────────────────────────────────────────
async function fetchStats() {
  let data;
  try {
    const resp = await fetch("/api/stats", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    data = await resp.json();
  } catch (err) {
    console.warn("fetchStats error:", err);
    return;
  }

  renderWan(data);
  renderSpeeds(data);
  renderClients(data.active_clients || []);
  renderSystem(data.system || {});
  renderPihole(data.pihole || {});
  renderHealth(data);
  renderGatewayTimeout(data.gateway_timeout || {});
  renderErrors(data.recent_errors || []);
  setText("last-updated", "Last updated: " + new Date().toLocaleTimeString());
}

// ── WAN section ───────────────────────────────────────────────────────────────
function renderWan(data) {
  const ip = data.wan_ip || "–";
  setText("wan-ip", ip);

  const dot = document.getElementById("wan-dot");
  if (dot) {
    if (ip && ip !== "–" && ip !== "unavailable") {
      dot.className = "dot online";
    } else {
      dot.className = "dot offline";
    }
  }

  if (data.system && data.system.uptime) {
    setText("uptime", data.system.uptime);
  }
}

// ── quick stat cards ──────────────────────────────────────────────────────────
function renderSpeeds(data) {
  const wan = data.wan_speed || {};
  const lan = data.lan_speed || {};

  // quick cards
  setText("stat-tx", wan.tx_human || "–");
  setText("stat-rx", wan.rx_human || "–");

  // speed table
  const tbody = document.getElementById("speed-tbody");
  if (!tbody) return;

  const rows = [
    { label: "WAN (" + (data._wan_iface || "wan") + ")", s: wan },
    { label: "LAN (" + (data._lan_iface || "lan") + ")", s: lan },
  ];

  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${escHtml(r.label)}</td>
      <td>${r.s.tx_human || "–"}</td>
      <td>${r.s.rx_human || "–"}</td>
    </tr>
  `).join("");
}

// ── system resources ──────────────────────────────────────────────────────────
function renderSystem(sys) {
  // quick cards
  setText("stat-cpu",  sys.cpu_percent  !== null && sys.cpu_percent !== undefined ? sys.cpu_percent + "%" : "–");
  setText("stat-ram",  sys.ram_percent  !== null && sys.ram_percent !== undefined ? sys.ram_percent + "%" : "–");
  setText("stat-temp", sys.cpu_temp_c   !== null && sys.cpu_temp_c  !== undefined ? sys.cpu_temp_c  + "°C" : "–");

  // bars
  setBar("bar-cpu",  sys.cpu_percent);
  setBar("bar-ram",  sys.ram_percent);
  setBar("bar-disk", sys.disk_percent);

  setText("pct-cpu",  fmtPct(sys.cpu_percent));
  setText("pct-ram",  fmtPct(sys.ram_percent));
  setText("pct-disk", fmtPct(sys.disk_percent));

  // disk detail
  setText("disk-used",  sys.disk_used  || "–");
  setText("disk-total", sys.disk_total || "–");
  setText("disk-avail", sys.disk_avail || "–");
}

// ── active clients ────────────────────────────────────────────────────────────
function renderClients(clients) {
  setText("stat-clients",  String(clients.length));
  setText("client-count",  String(clients.length));

  const tbody = document.getElementById("client-tbody");
  if (!tbody) return;

  if (clients.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-row">No active DHCP leases</td></tr>';
    return;
  }

  tbody.innerHTML = clients.map(c => `
    <tr>
      <td>${escHtml(c.hostname || "(unknown)")}</td>
      <td class="ip">${escHtml(c.ip || "–")}</td>
      <td class="mac">${escHtml(c.mac || "–")}</td>
      <td class="ip">${escHtml(c.tx_human || "–")}</td>
      <td class="ip">${escHtml(c.rx_human || "–")}</td>
      <td><span class="lease-pill">${fmtLease(c.lease_remaining)}</span></td>
    </tr>
  `).join("");
}

// ── Pi-hole ───────────────────────────────────────────────────────────────────
function renderPihole(ph) {
  const badge = document.getElementById("pihole-badge");
  if (badge) {
    badge.textContent = ph.status || "unknown";
    badge.className = "pihole-status-badge " + (ph.status || "unknown");
  }

  setText("ph-blocked-today", fmtNum(ph.ads_blocked_today));
  setText("ph-pct",           (ph.ads_percentage_today ?? "–") + (ph.ads_percentage_today !== undefined ? "%" : ""));
  setText("ph-queries",       fmtNum(ph.queries_today));
  setText("ph-domains",       fmtNum(ph.domains_blocked));
  setText("ph-clients",       fmtNum(ph.unique_clients));

  renderPhDomainList("ph-top-blocked", ph.top_blocked || []);
  renderPhDomainList("ph-top-queried", ph.top_queried || []);
  renderPhQueryTypes("ph-query-types", ph.query_types || {});
}

function renderPhDomainList(id, domains) {
  const el = document.getElementById(id);
  if (!el) return;
  if (domains.length === 0) {
    el.innerHTML = '<div class="ph-row ph-empty">No data</div>';
    return;
  }
  const max = domains[0].count || 1;
  el.innerHTML = domains.map(d => `
    <div class="ph-row">
      <span class="ph-row-label">${escHtml(d.domain)}</span>
      <div class="ph-bar-wrap"><div class="ph-bar" style="width:${Math.round(d.count / max * 100)}%"></div></div>
      <span class="ph-row-count">${fmtNum(d.count)}</span>
    </div>`).join("");
}

function renderPhQueryTypes(id, types) {
  const el = document.getElementById(id);
  if (!el) return;
  const entries = Object.entries(types).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) {
    el.innerHTML = '<div class="ph-row ph-empty">No data</div>';
    return;
  }
  const total = entries.reduce((s, [, v]) => s + v, 0) || 1;
  el.innerHTML = entries.map(([type, count]) => {
    const pct = Math.round(count / total * 100);
    return `
    <div class="ph-row">
      <span class="ph-row-label ph-qt-type">${escHtml(type)}</span>
      <div class="ph-bar-wrap"><div class="ph-bar ph-bar-qt" style="width:${pct}%"></div></div>
      <span class="ph-row-count">${pct}%</span>
    </div>`;
  }).join("");
}

// ── error log ─────────────────────────────────────────────────────────────────
function renderErrors(errors) {
  const container = document.getElementById("error-log");
  if (!container) return;

  if (errors.length === 0) {
    container.innerHTML = '<div class="log-line log-empty">No errors detected.</div>';
    return;
  }

  container.innerHTML = errors.map(e => `
    <div class="log-line log-${escHtml(e.source || "")}">
      <span class="log-source">[${escHtml(e.source || "?")}]</span>${escHtml(e.message || "")}
    </div>
  `).join("");
}

function clearErrors() {
  const container = document.getElementById("error-log");
  if (container) {
    container.innerHTML = '<div class="log-line log-empty">Log cleared.</div>';
  }
}

// ── health checks ─────────────────────────────────────────────────────────────
function renderHealth(data) {
  const health = data.health || {};

  // Header dots (gateway + DNS)
  const gw = health.gateway || {};
  const gwDot = document.getElementById("health-gateway");
  const gwStatusEl = document.getElementById("health-gateway-status");
  if (gwDot && gwStatusEl) {
    gwDot.className = "health-dot " + (gw.status || "unknown");
    gwStatusEl.textContent = gw.status === "up"
      ? (gw.response_time_ms ? gw.response_time_ms + "ms" : "up")
      : (gw.status || "–");
  }

  const dns = health.dns || {};
  const dnsDot = document.getElementById("health-dns");
  const dnsStatusEl = document.getElementById("health-dns-status");
  if (dnsDot && dnsStatusEl) {
    dnsDot.className = "health-dot " + (dns.status || "unknown");
    dnsStatusEl.textContent = dns.status === "ok"
      ? (dns.response_time_ms ? dns.response_time_ms + "ms" : "ok")
      : (dns.status || "–");
  }

  // Full network health panel
  renderNetworkHealth(health);
}

function renderNetworkHealth(health) {
  const gw   = health.gateway  || {};
  const isp  = health.isp      || {};
  const dns  = health.dns      || {};
  const ap   = health.ap       || {};
  const diag = health.diagnosis || {};

  // Overall badge
  const badge = document.getElementById("nh-overall-badge");
  if (badge) {
    const overall = diag.overall || "ok";
    badge.className = "nh-badge nh-" + overall;
    badge.textContent = overall === "ok"       ? "All Systems OK"
                      : overall === "warning"  ? "Warning"
                      :                          "Fault Detected";
  }

  // Issue messages
  const issuesEl = document.getElementById("nh-issues");
  if (issuesEl) {
    const issues  = diag.issues  || [];
    const overall = diag.overall || "ok";
    if (issues.length === 0) {
      issuesEl.innerHTML = "";
    } else {
      issuesEl.innerHTML = issues.map(msg =>
        `<div class="nh-issue${overall === "warning" ? " warn" : ""}">${escHtml(msg)}</div>`
      ).join("");
    }
  }

  // Row helper
  function setRow(dotId, valId, statusClass, valueText) {
    const dot = document.getElementById(dotId);
    const val = document.getElementById(valId);
    if (dot) dot.className = "nh-dot " + statusClass;
    if (val) val.textContent = valueText;
  }

  // Gateway
  setRow("ndot-gateway", "nval-gateway",
    gw.status === "up" ? "up" : (gw.status === "unknown" ? "unknown" : "down"),
    gw.status === "up"
      ? (gw.response_time_ms ? gw.response_time_ms + " ms" : "up")
      : (gw.status === "unknown" ? "checking…" : "Unreachable")
  );

  // ISP
  setRow("ndot-isp", "nval-isp",
    isp.status === "up" ? "up" : (isp.status === "down" ? "down" : "unknown"),
    isp.status === "up"
      ? (isp.response_time_ms ? isp.response_time_ms + " ms" : "up")
      : (isp.status === "down" ? "Down" : "checking…")
  );

  // DNS
  setRow("ndot-dns", "nval-dns",
    dns.status === "ok" ? "up" : (dns.status === "failed" ? "down" : "unknown"),
    dns.status === "ok"
      ? (dns.response_time_ms ? dns.response_time_ms + " ms" : "ok")
      : (dns.status === "failed" ? "Failed" : (dns.status || "–"))
  );

  // AP
  const apRow = document.getElementById("nrow-ap");
  if (ap.status === "disabled" || ap.status === "unconfigured") {
    if (apRow) apRow.style.opacity = "0.4";
    setRow("ndot-ap", "nval-ap", "disabled", "Not configured");
  } else {
    if (apRow) apRow.style.opacity = "1";
    setRow("ndot-ap", "nval-ap",
      ap.status === "up" ? "up" : (ap.status === "down" ? "down" : "unknown"),
      ap.status === "up"
        ? (ap.response_time_ms ? ap.response_time_ms + " ms" : "up")
        : (ap.status === "down" ? "No Connection" : (ap.status || "–"))
    );
  }
}

function renderGatewayTimeout(monitor) {
  const stateEl = document.getElementById("gtw-state");
  const timedOut = Boolean(monitor.is_timed_out);

  if (stateEl) {
    stateEl.className = "gtw-state " + (timedOut ? "down" : "up");
    stateEl.textContent = timedOut ? "Timed Out" : "Connected";
  }

  setText("gtw-current", timedOut ? fmtDuration(monitor.current_timeout_seconds) : "0s");
  setText("gtw-since", timedOut ? fmtTimestamp(monitor.disconnected_since) : "—");
  setText("gtw-total", fmtDuration(monitor.total_timeout_seconds));
  setText("gtw-count", String(monitor.timeout_count || 0));

  const lastDuration = monitor.last_timeout_duration_seconds;
  const lastEnded = monitor.last_timeout_ended_at;
  const lastLine = (lastDuration !== null && lastDuration !== undefined)
    ? `Last timeout: ${fmtDuration(lastDuration)} (ended ${fmtTimestamp(lastEnded)})`
    : "Last timeout: —";
  setText("gtw-last", lastLine);

  const logEl = document.getElementById("gtw-log");
  if (!logEl) return;

  const events = Array.isArray(monitor.recent_timeouts) ? monitor.recent_timeouts : [];
  if (events.length === 0) {
    logEl.innerHTML = '<div class="gtw-log-line empty">No gateway timeouts recorded.</div>';
    return;
  }

  const lines = events.slice(-8).reverse().map(ev => {
    const started = fmtTimestamp(ev.started_at);
    const ended = fmtTimestamp(ev.ended_at);
    const duration = fmtDuration(ev.duration_seconds);
    return `<div class="gtw-log-line">${escHtml(started)} → ${escHtml(ended)} · ${escHtml(duration)}</div>`;
  });

  logEl.innerHTML = lines.join("");
}

// ── power / battery monitoring ─────────────────────────────────────────────────
let _powerAlertDismissed = false;

function _updateBatteryBadge(battery) {
  const badge  = document.getElementById("battery-badge");
  const icon   = document.getElementById("battery-icon");
  const pctEl  = document.getElementById("battery-pct");
  if (!badge) return;

  const pct = Math.round(battery.level * 100);
  badge.style.display = "";  // make it visible
  if (pctEl) pctEl.textContent = pct + "%";

  badge.classList.remove("charging", "low");
  if (battery.charging) {
    badge.classList.add("charging");
    if (icon) icon.textContent = "⚡";
  } else {
    if (pct <= 20) badge.classList.add("low");
    if (icon) icon.textContent = "🔋";
  }
}

function _showPowerAlert(battery) {
  if (_powerAlertDismissed) return;
  const el     = document.getElementById("power-alert");
  const detail = document.getElementById("power-alert-detail");
  if (!el) return;
  if (detail) {
    const pct = Math.round(battery.level * 100);
    detail.textContent = `Running on battery (${pct}%). AC power has been disconnected.`;
  }
  el.classList.add("visible");
}

function _hidePowerAlert() {
  const el = document.getElementById("power-alert");
  if (el) el.classList.remove("visible");
}

function dismissPowerAlert() {
  _powerAlertDismissed = true;
  _hidePowerAlert();
}

async function initBatteryMonitor() {
  if (!("getBattery" in navigator)) return;
  try {
    const battery = await navigator.getBattery();

    // Update the header badge immediately and on every change
    _updateBatteryBadge(battery);
    battery.addEventListener("levelchange",   () => _updateBatteryBadge(battery));
    battery.addEventListener("chargingchange", () => {
      _updateBatteryBadge(battery);
      if (battery.charging) {
        // AC restored — hide alert and allow it to show again next time
        _hidePowerAlert();
        _powerAlertDismissed = false;
      } else {
        // AC just disconnected — show immediately
        _showPowerAlert(battery);
      }
    });

    // Delay the initial on-load check by 10 s so the browser has time to
    // correctly read the hardware charging state (avoids false positives).
    setTimeout(() => {
      if (!battery.charging) _showPowerAlert(battery);
    }, 10000);

  } catch (_) { /* Battery API unavailable */ }
}

initBatteryMonitor();

// ── bandwidth trends chart ────────────────────────────────────────────────────
async function renderTrends() {
  try {
    const resp = await fetch("/api/trends", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    
    const canvas = document.getElementById("bandwidth-chart");
    if (!canvas) return;
    
    drawBandwidthChart(canvas, data.trends || {});
  } catch (err) {
    console.warn("renderTrends error:", err);
  }
}

function drawBandwidthChart(canvas, trends) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  
  // Get dimensions
  const w = canvas.width;
  const h = canvas.height;
  
  // Clear canvas
  ctx.fillStyle = getComputedStyle(canvas).backgroundColor || "#1c2330";
  ctx.fillRect(0, 0, w, h);
  
  const wan = trends.wan || [];
  const lan = trends.lan || [];
  const allData = [...wan, ...lan];
  
  if (allData.length === 0) {
    // No data yet
    ctx.fillStyle = "rgba(139, 148, 158, 0.5)";
    ctx.font = "13px Segoe UI";
    ctx.textAlign = "center";
    ctx.fillText("Collecting trend data...", w / 2, h / 2);
    return;
  }
  
  // Find max value for scaling
  const maxMbps = Math.max(
    ...allData.map(d => Math.max(d.rx_mbps || 0, d.tx_mbps || 0))
  );
  const scale = maxMbps > 0 ? (h - 20) / maxMbps : 1;
  
  // Padding and dimensions
  const padding = { top: 10, right: 10, bottom: 10, left: 40 };
  const plotW = w - padding.left - padding.right;
  const plotH = h - padding.top - padding.bottom;
  const pointSpacing = wan.length > 1 ? plotW / (wan.length - 1) : plotW;
  
  // Draw grid lines
  ctx.strokeStyle = "rgba(48, 54, 61, 0.5)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = padding.top + (plotH / 4) * i;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(w - padding.right, y);
    ctx.stroke();
  }
  
  // Draw Y-axis scale labels
  ctx.fillStyle = "rgba(139, 148, 158, 0.7)";
  ctx.font = "11px monospace";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const mbps = (maxMbps / 4) * i;
    const y = padding.top + plotH - (plotH / 4) * i;
    ctx.fillText(mbps.toFixed(0) + " Mb/s", padding.left - 5, y + 3);
  }
  
  // Draw axes
  ctx.strokeStyle = "rgba(139, 148, 158, 0.8)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padding.left, padding.top);
  ctx.lineTo(padding.left, h - padding.bottom);
  ctx.lineTo(w - padding.right, h - padding.bottom);
  ctx.stroke();
  
  // Draw lines for each data stream
  const colors = {
    wan_rx: "#FF6B6B",  // red
    wan_tx: "#4ECDC4",  // cyan
    lan_rx: "#95E1D3",  // light green
    lan_tx: "#FFE66D",  // yellow
  };
  
  function drawLine(dataArray, color) {
    if (!dataArray || dataArray.length === 0) return;
    
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    
    for (let i = 0; i < dataArray.length; i++) {
      const d = dataArray[i];
      const x = padding.left + i * pointSpacing;
      const y = h - padding.bottom - (d.rx_mbps * scale);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
  
  function drawTxLine(dataArray, color) {
    if (!dataArray || dataArray.length === 0) return;
    
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.setLineDash([5, 5]);
    ctx.beginPath();
    
    for (let i = 0; i < dataArray.length; i++) {
      const d = dataArray[i];
      const x = padding.left + i * pointSpacing;
      const y = h - padding.bottom - (d.tx_mbps * scale);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }
  
  // Draw the lines (solid = RX, dashed = TX)
  drawLine(wan, colors.wan_rx);
  drawTxLine(wan, colors.wan_tx);
  drawLine(lan, colors.lan_rx);
  drawTxLine(lan, colors.lan_tx);
}

// ── Port Forwards ─────────────────────────────────────────────────────────────
async function fetchPortForwards() {
  try {
    const resp = await fetch("/api/port-forwards", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    renderNftRules(data.nft_rules || []);
    renderPortForwards(data.entries || []);
  } catch (err) {
    console.warn("fetchPortForwards error:", err);
  }
}

function renderNftRules(rules) {
  const el = document.getElementById("pf-nft-list");
  if (!el) return;
  if (rules.length === 0) {
    el.innerHTML = '<div class="pf-nft-empty">No DNAT rules detected in nftables</div>';
    return;
  }
  el.innerHTML = rules.map(r => {
    const protoKey = (r.protocol || "tcp").replace("/", "");
    return `
      <div class="pf-nft-row">
        <span class="pf-proto-badge pf-proto-${escHtml(protoKey)}">${escHtml((r.protocol || "tcp").toUpperCase())}</span>
        <span class="pf-port">${escHtml(String(r.external_port))}</span>
        <span class="pf-nft-arrow">→</span>
        <span class="pf-internal">${escHtml(r.internal_ip)}:${escHtml(String(r.internal_port))}</span>
        <span class="pf-nft-chain">${escHtml(r.table)}/${escHtml(r.chain)}</span>
      </div>`;
  }).join("");
}

function renderPortForwards(entries) {
  setText("pf-count", String(entries.length));
  const tbody = document.getElementById("pf-tbody");
  if (!tbody) return;

  if (entries.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-row">No port forwards configured</td></tr>';
    return;
  }

  tbody.innerHTML = entries.map(pf => {
    const protoKey = (pf.protocol || "tcp").replace("/", "");
    const sysBadge = pf.system_detected
      ? '<span class="pf-sys-badge" title="Detected in iptables">sys</span>'
      : '';
    const descHtml = pf.description
      ? `<span title="${escHtml(pf.description)}">${escHtml(pf.description)}</span>`
      : '<span class="pf-no-desc">—</span>';
    return `
      <tr data-pf-id="${escHtml(pf.id)}">
        <td><span class="pf-dot ${pf.active ? 'active' : 'inactive'}" title="${pf.active ? 'Active' : 'Inactive'}"></span>${sysBadge}</td>
        <td><span class="pf-proto-badge pf-proto-${escHtml(protoKey)}">${escHtml((pf.protocol || "tcp").toUpperCase())}</span></td>
        <td class="pf-port">${escHtml(String(pf.external_port))}</td>
        <td class="pf-internal">${escHtml(pf.internal_ip)}:${escHtml(String(pf.internal_port))}</td>
        <td class="pf-desc">${descHtml}</td>
        <td class="pf-actions">
          <button class="pf-btn-toggle" onclick="pfToggleActive('${escHtml(pf.id)}',${!pf.active})" title="${pf.active ? 'Mark inactive' : 'Mark active'}">${pf.active ? '●' : '○'}</button>
          <button class="pf-btn-edit"   onclick="pfStartEdit('${escHtml(pf.id)}')"                  title="Edit">✎</button>
          <button class="pf-btn-del"    onclick="pfDelete('${escHtml(pf.id)}')"                     title="Delete">✕</button>
        </td>
      </tr>`;
  }).join("");
}

function pfToggleAddForm() {
  const form = document.getElementById("pf-add-form");
  if (!form) return;
  const showing = form.style.display !== "none";
  form.style.display = showing ? "none" : "block";
  if (!showing) document.getElementById("pf-form-ext-port").focus();
}

async function pfAddSubmit() {
  const proto   = document.getElementById("pf-form-proto").value;
  const extPort = document.getElementById("pf-form-ext-port").value;
  const intIp   = document.getElementById("pf-form-int-ip").value.trim();
  const intPort = document.getElementById("pf-form-int-port").value;
  const desc    = document.getElementById("pf-form-desc").value.trim();
  const active  = document.getElementById("pf-form-active").checked;

  if (!extPort || !intIp || !intPort) {
    alert("External port, internal IP, and internal port are required.");
    return;
  }
  try {
    const resp = await fetch("/api/port-forwards", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ protocol: proto, external_port: +extPort, internal_ip: intIp, internal_port: +intPort, description: desc, active }),
    });
    if (!resp.ok) { alert("Error: " + ((await resp.json()).error || resp.status)); return; }
    document.getElementById("pf-form-ext-port").value = "";
    document.getElementById("pf-form-int-ip").value   = "";
    document.getElementById("pf-form-int-port").value = "";
    document.getElementById("pf-form-desc").value     = "";
    document.getElementById("pf-form-active").checked  = true;
    document.getElementById("pf-add-form").style.display = "none";
    fetchPortForwards();
  } catch (err) { alert("Network error: " + err.message); }
}

async function pfToggleActive(id, newActive) {
  try {
    await fetch("/api/port-forwards/" + id, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active: newActive }),
    });
    fetchPortForwards();
  } catch (err) { console.warn("pfToggleActive error:", err); }
}

async function pfDelete(id) {
  if (!confirm("Delete this port forward rule?")) return;
  try {
    await fetch("/api/port-forwards/" + id, { method: "DELETE" });
    fetchPortForwards();
  } catch (err) { console.warn("pfDelete error:", err); }
}

function pfStartEdit(id) {
  fetch("/api/port-forwards", { cache: "no-store" })
    .then(r => r.json())
    .then(entries => {
      const pf = entries.find(e => e.id === id);
      if (pf) pfShowEditRow(pf);
    })
    .catch(err => console.warn("pfStartEdit error:", err));
}

function pfShowEditRow(pf) {
  const tbody = document.getElementById("pf-tbody");
  if (!tbody) return;
  const row = tbody.querySelector(`tr[data-pf-id="${CSS.escape(pf.id)}"]`);
  if (!row) return;

  const selOpts = ["tcp", "udp", "tcp/udp"].map(p =>
    `<option value="${p}"${pf.protocol === p ? " selected" : ""}>${p.toUpperCase()}</option>`
  ).join("");

  row.outerHTML = `
    <tr class="pf-edit-row" data-pf-id="${escHtml(pf.id)}">
      <td colspan="6">
        <div class="pf-edit-form">
          <select class="pf-input" id="pfe-proto-${escHtml(pf.id)}">${selOpts}</select>
          <input class="pf-input pf-input-port" id="pfe-ext-${escHtml(pf.id)}"  type="number" min="1" max="65535" value="${escHtml(String(pf.external_port))}">
          <input class="pf-input pf-input-ip"   id="pfe-ip-${escHtml(pf.id)}"   type="text"                       value="${escHtml(pf.internal_ip)}">
          <input class="pf-input pf-input-port" id="pfe-int-${escHtml(pf.id)}"  type="number" min="1" max="65535" value="${escHtml(String(pf.internal_port))}">
          <input class="pf-input pf-form-desc"  id="pfe-desc-${escHtml(pf.id)}" type="text"   placeholder="Description" value="${escHtml(pf.description || '')}">
          <label class="pf-form-active-label">
            <input type="checkbox" id="pfe-active-${escHtml(pf.id)}"${pf.active ? " checked" : ""}> Active
          </label>
          <button class="pf-btn-save"   onclick="pfEditSubmit('${escHtml(pf.id)}')">Save</button>
          <button class="pf-btn-cancel" onclick="fetchPortForwards()">Cancel</button>
        </div>
      </td>
    </tr>`;
}

async function pfEditSubmit(id) {
  const proto   = document.getElementById("pfe-proto-"  + id).value;
  const extPort = +document.getElementById("pfe-ext-"   + id).value;
  const intIp   = document.getElementById("pfe-ip-"     + id).value.trim();
  const intPort = +document.getElementById("pfe-int-"   + id).value;
  const desc    = document.getElementById("pfe-desc-"   + id).value.trim();
  const active  = document.getElementById("pfe-active-" + id).checked;
  try {
    const resp = await fetch("/api/port-forwards/" + id, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ protocol: proto, external_port: extPort, internal_ip: intIp, internal_port: intPort, description: desc, active }),
    });
    if (!resp.ok) { alert("Error: " + ((await resp.json()).error || resp.status)); return; }
    fetchPortForwards();
  } catch (err) { alert("Network error: " + err.message); }
}

// ── XSS safe helper ───────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
}

// ── polling loop ──────────────────────────────────────────────────────────────
fetchStats();
renderTrends();
fetchPortForwards();
setInterval(fetchStats, REFRESH_MS);
setInterval(renderTrends, 10000);
setInterval(fetchPortForwards, 10000);
