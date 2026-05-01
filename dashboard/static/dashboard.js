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
  // Extract health info from /api/stats response
  const health = data.health || {};
  
  // Gateway health
  const gwStatus = health.gateway || {};
  const gwDot = document.getElementById("health-gateway");
  const gwStatusEl = document.getElementById("health-gateway-status");
  if (gwDot && gwStatusEl) {
    gwDot.className = "health-dot " + (gwStatus.status || "unknown");
    let text = gwStatus.status === "up" 
      ? (gwStatus.response_time_ms ? gwStatus.response_time_ms + "ms" : "up")
      : (gwStatus.status || "–");
    gwStatusEl.textContent = text;
  }
  
  // DNS health
  const dnsStatus = health.dns || {};
  const dnsDot = document.getElementById("health-dns");
  const dnsStatusEl = document.getElementById("health-dns-status");
  if (dnsDot && dnsStatusEl) {
    dnsDot.className = "health-dot " + (dnsStatus.status || "unknown");
    let text = dnsStatus.status === "ok"
      ? (dnsStatus.response_time_ms ? dnsStatus.response_time_ms + "ms" : "ok")
      : (dnsStatus.status || "–");
    dnsStatusEl.textContent = text;
  }
}

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
fetchStats(); // first call immediately
renderTrends(); // fetch trends
setInterval(fetchStats, REFRESH_MS);
setInterval(renderTrends, 10000); // fetch trends every 10 seconds
