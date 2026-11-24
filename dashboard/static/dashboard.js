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
async function fetchStats(resolveNames = false) {
  let data;
  try {
    const suffix = resolveNames ? "?resolve_names=1" : "";
    const resp = await fetch("/api/stats" + suffix, { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    data = await resp.json();
  } catch (err) {
    console.warn("fetchStats error:", err);
    return;
  }

  // Each panel renders in isolation: one panel throwing on unexpected data
  // used to abort fetchStats() and silently freeze every panel after it —
  // the Blocklist is rendered near the end, so it was the usual casualty.
  const panels = [
    ["wan", () => renderWan(data)],
    ["speeds", () => renderSpeeds(data)],
    ["clients", () => renderClients(data.active_clients || [])],
    ["system", () => renderSystem(data.system || {})],
    ["pihole", () => renderPihole(data.pihole || {})],
    ["health", () => renderHealth(data)],
    ["gateway", () => renderGatewayTimeout(data.gateway_timeout || {})],
    ["psad", () => renderPsad(data.psad || {})],
    ["upnp", () => renderUpnp(data.upnp || {})],
    ["suricata", () => renderSuricata(data.suricata || {})],
    ["blocklist", () => renderBlocklist(data.blocklist || {})],
    ["errors", () => renderErrors(data.recent_errors || [])],
  ];
  for (const [name, render] of panels) {
    try { render(); } catch (err) { console.error(`render ${name} failed:`, err); }
  }
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

// ── psad scan detection ──────────────────────────────────────────────────────
const PSAD_TYPE_ICON = {
  auto_block: "🚫", manual_block: "🚫", manual_unblock: "✅",
  scan_detected: "🔍", signature_match: "⚠",
};

let _psadBlockedIps = new Set();
let _psadLastEvents = [];
let _psadExpandedIps = new Set();   // block-type rows currently showing detail
const _psadDetailCache = {};        // ip -> detail response (or "loading"/"error" sentinel)

function renderPsad(psad) {
  const badge = document.getElementById("psad-status-badge");
  const enabled = Boolean(psad.enabled);
  if (badge) {
    badge.className = "psad-status-badge " + (enabled ? "active" : "disabled");
    badge.textContent = enabled ? "Active" : "Disabled";
  }

  // Not shown directly any more (see the Blocklist panel), but still
  // tracked so the event log below knows not to offer "Block" on an IP
  // that's already blocked.
  const blocked = Array.isArray(psad.currently_blocked) ? psad.currently_blocked : [];
  _psadBlockedIps = new Set(blocked.map(b => b.ip));

  const events = Array.isArray(psad.events) ? psad.events : [];
  setText("psad-event-count", String(events.length));
  _psadLastEvents = events;

  _renderPsadLog();
}

function _renderPsadDetailPanel(ip) {
  const cached = _psadDetailCache[ip];
  if (cached === "loading" || cached === undefined) {
    return '<div class="psad-detail-panel"><div class="psad-detail-loading">Loading…</div></div>';
  }
  if (cached === "error") {
    return '<div class="psad-detail-panel"><div class="psad-detail-loading">Failed to load detail.</div></div>';
  }
  if (!cached.tracked) {
    return '<div class="psad-detail-panel"><div class="psad-detail-loading">psad has no tracking history for this IP (blocked preemptively, not from a detected scan).</div></div>';
  }

  let html = '<div class="psad-detail-panel"><div class="psad-detail-grid">';
  if (cached.danger_level !== undefined && cached.danger_level !== null) {
    html += `<div class="psad-detail-item"><span class="psad-detail-label">Danger Level</span><span class="psad-detail-val">DL${escHtml(String(cached.danger_level))}</span></div>`;
  }
  if (cached.whois_summary) {
    html += `<div class="psad-detail-item"><span class="psad-detail-label">Registrant</span><span class="psad-detail-val">${escHtml(cached.whois_summary)}</span></div>`;
  }
  html += "</div>";

  const dests = Array.isArray(cached.destinations) ? cached.destinations : [];
  for (const d of dests) {
    const bits = [];
    if (d.packets !== undefined) bits.push(fmtNum(d.packets) + " packets");
    if (d.start_time) bits.push("since " + fmtTimestamp(d.start_time));
    html += '<div class="psad-detail-dest">';
    html += `<div class="psad-detail-dest-header">Target ${escHtml(d.dest || "")}${bits.length ? " · " + escHtml(bits.join(" · ")) : ""}</div>`;
    if (Array.isArray(d.signatures) && d.signatures.length) {
      html += d.signatures.map(s => `<div class="psad-detail-sig">${escHtml(s)}</div>`).join("");
    }
    html += "</div>";
  }

  html += "</div>";
  return html;
}

function _renderPsadLog() {
  const logEl = document.getElementById("psad-log");
  if (!logEl) return;

  const events = _psadLastEvents;
  if (events.length === 0) {
    logEl.innerHTML = '<div class="psad-log-line empty">No scan activity recorded.</div>';
    return;
  }

  const lines = events.slice(0, 20).map(ev => {
    const icon = PSAD_TYPE_ICON[ev.type] || "•";
    const ts = fmtTimestamp(ev.timestamp);
    const resolvedClass = ev.resolved ? " psad-resolved" : "";
    const ip = ev.ip || "";
    const isBlockType = ev.type === "auto_block" || ev.type === "manual_block";
    const isBlocked = _psadBlockedIps.has(ip);
    // Already-blocked IPs are unblocked from the Blocklist panel now, not here.
    const actionBtn = (ip && !isBlocked)
      ? `<button class="psad-action-btn psad-block-btn" onclick="blockIp('${escHtml(ip)}')">Block</button>`
      : "";
    const isExpanded = isBlockType && _psadExpandedIps.has(ip);
    const detailToggle = isBlockType
      ? `<button class="psad-action-btn psad-detail-toggle" onclick="psadToggleDetail('${escHtml(ip)}')">${isExpanded ? "▲ Why" : "▼ Why"}</button>`
      : "";
    const row = `<div class="psad-log-line psad-type-${escHtml(ev.type || "")}${resolvedClass}">` +
      `<span class="psad-log-icon">${icon}</span>` +
      `<span class="psad-log-ip">${escHtml(ip)}</span>` +
      `<span class="psad-log-detail">${escHtml(ev.detail || "")}</span>` +
      `<span class="psad-log-ts">${escHtml(ts)}</span>` +
      detailToggle +
      actionBtn +
      `</div>`;
    const detailPanel = isExpanded ? _renderPsadDetailPanel(ip) : "";
    return row + detailPanel;
  });
  logEl.innerHTML = lines.join("");
}

async function psadToggleDetail(ip) {
  if (_psadExpandedIps.has(ip)) {
    _psadExpandedIps.delete(ip);
    _renderPsadLog();
    return;
  }
  _psadExpandedIps.add(ip);
  if (_psadDetailCache[ip] === undefined) {
    _psadDetailCache[ip] = "loading";
    _renderPsadLog();
    try {
      const resp = await fetch("/api/psad/detail/" + encodeURIComponent(ip));
      _psadDetailCache[ip] = resp.ok ? await resp.json() : "error";
    } catch (err) {
      _psadDetailCache[ip] = "error";
    }
  }
  _renderPsadLog();
}

function renderSuricata(suricata) {
  const badge = document.getElementById("suricata-status-badge");
  const enabled = Boolean(suricata.enabled);
  if (badge) {
    badge.className = "suricata-status-badge " + (enabled ? "active" : "disabled");
    badge.textContent = enabled ? "Active" : "Disabled";
  }

  setText("suricata-alert-count", String(suricata.alert_count || 0));

  const logEl = document.getElementById("suricata-log");
  if (!logEl) return;
  const alerts = Array.isArray(suricata.alerts) ? suricata.alerts : [];
  if (!alerts.length) {
    logEl.innerHTML = '<div class="suricata-log-line empty">No alerts recorded.</div>';
    return;
  }

  logEl.innerHTML = alerts.map(a => {
    const sev = (a.severity === undefined || a.severity === null) ? 3 : a.severity;
    const dest = a.dest_ip ? (a.dest_ip + (a.dest_port ? ":" + a.dest_port : "")) : "";
    return `<div class="suricata-log-line suricata-sev-${escHtml(String(sev))}">` +
      `<span class="suricata-log-sig">${escHtml(a.signature || "")}</span>` +
      `<span class="suricata-log-flow">${escHtml(a.src_ip || "")} → ${escHtml(dest)}</span>` +
      `<span class="suricata-log-ts">${escHtml(fmtTimestamp(a.timestamp))}</span>` +
      `</div>`;
  }).join("");
}

function renderUpnp(upnp) {
  const badge = document.getElementById("upnp-status-badge");
  const enabled = Boolean(upnp.enabled);
  if (badge) {
    badge.className = "upnp-status-badge " + (enabled ? "active" : "disabled");
    badge.textContent = enabled ? "Active" : "Disabled";
  }

  const bodyEl = document.getElementById("upnp-table-body");
  if (!bodyEl) return;
  const mappings = Array.isArray(upnp.mappings) ? upnp.mappings : [];
  if (!mappings.length) {
    bodyEl.innerHTML = '<tr><td colspan="5" class="upnp-empty">No active port mappings.</td></tr>';
    return;
  }

  bodyEl.innerHTML = mappings.map(m => {
    const portCell = m.ext_port === m.int_port
      ? escHtml(String(m.ext_port))
      : `${escHtml(String(m.ext_port))} → ${escHtml(String(m.int_port))}`;
    return "<tr>" +
      `<td class="upnp-port">${portCell}</td>` +
      `<td>${escHtml(m.proto || "")}</td>` +
      `<td>${escHtml(m.hostname || m.int_ip || "")}</td>` +
      `<td>${escHtml(m.likely_reason || "")}</td>` +
      `<td class="upnp-expires">${escHtml(fmtTimestamp(m.expires))}</td>` +
      "</tr>";
  }).join("");
}

const BLOCKLIST_PAGE_SIZE = 20;
let _blocklistEntries = [];
let _blocklistShown = BLOCKLIST_PAGE_SIZE;

function fmtShortTs(isoTs) {
  if (!isoTs) return "–";
  const dt = new Date(isoTs);
  if (Number.isNaN(dt.getTime())) return "–";
  return dt.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function fmtExpiresIn(secs) {
  if (secs === null || secs === undefined) return "";
  const s = Math.max(0, Number(secs) || 0);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d > 0) return `in ${d}d ${h}h`;
  if (h > 0) return `in ${h}h ${m}m`;
  return `in ${Math.max(m, 1)}m`;
}

const BLOCKLIST_SOURCE_LABEL = {
  suricata: "Suricata",
  auto: "Auto",
  manual: "Manual",
  unknown: "Unknown",
};

function _blocklistWhyCell(e) {
  const src = e.source || "unknown";
  const badge = `<span class="blocklist-badge blocklist-badge-${escHtml(src)}">${escHtml(BLOCKLIST_SOURCE_LABEL[src] || src)}</span>`;
  let html = badge + `<span class="blocklist-reason-text">${escHtml(e.reason || "")}</span>`;
  const meta = [];
  if (e.category) meta.push(escHtml(e.category));
  if (e.severity_label) meta.push(`<span class="blocklist-sev blocklist-sev-${escHtml(String(e.severity))}">${escHtml(e.severity_label)}</span>`);
  if (e.sid) meta.push("SID " + escHtml(String(e.sid)));
  if (e.target) meta.push("→ " + escHtml(e.target));
  if (e.alert_time) meta.push("alert " + escHtml(fmtShortTs(e.alert_time)));
  if (meta.length) html += `<div class="blocklist-meta">${meta.join(" · ")}</div>`;
  return html;
}

function renderBlocklist(blocklist) {
  const entries = Array.isArray(blocklist.entries) ? blocklist.entries : [];
  _blocklistEntries = entries;

  let countText = `${entries.length} blocked`;
  if (blocklist.suricata_count !== undefined) {
    countText += ` · ${blocklist.suricata_count} Suricata (7-day) · ${blocklist.manual_count || 0} permanent`;
  }
  setText("blocklist-count", countText);

  const bodyEl = document.getElementById("blocklist-table-body");
  const moreBtn = document.getElementById("blocklist-load-more");
  if (!bodyEl) return;

  if (!entries.length) {
    bodyEl.innerHTML = '<tr><td colspan="5" class="blocklist-empty">No IPs currently blocked.</td></tr>';
    if (moreBtn) moreBtn.hidden = true;
    return;
  }

  const shown = entries.slice(0, Math.min(_blocklistShown, entries.length));
  bodyEl.innerHTML = shown.map(e => {
    const sourceClass = `blocklist-src-${escHtml(e.source || "unknown")}`;
    const expires = (e.expires_in_sec === null || e.expires_in_sec === undefined)
      ? '<span class="blocklist-permanent">Permanent</span>'
      : `<span title="${escHtml(fmtTimestamp(e.expires_at))}">${escHtml(fmtExpiresIn(e.expires_in_sec))}</span>`;
    return "<tr>" +
      `<td class="blocklist-ip">${escHtml(e.ip || "")}</td>` +
      `<td class="blocklist-reason ${sourceClass}">${_blocklistWhyCell(e)}</td>` +
      `<td class="blocklist-ts" title="${escHtml(fmtTimestamp(e.timestamp))}">${escHtml(fmtShortTs(e.timestamp))}</td>` +
      `<td class="blocklist-ts">${expires}</td>` +
      `<td class="blocklist-action"><button class="blocklist-unblock-btn" onclick="unblockIp('${escHtml(e.ip)}')">Unblock</button></td>` +
      "</tr>";
  }).join("");

  if (moreBtn) {
    const remaining = entries.length - shown.length;
    moreBtn.hidden = remaining <= 0;
    moreBtn.textContent = `Load more (${remaining} more)`;
  }
}

function blocklistLoadMore() {
  _blocklistShown += BLOCKLIST_PAGE_SIZE;
  renderBlocklist({ entries: _blocklistEntries });
}

async function blockIp(ip) {
  try {
    const resp = await fetch("/api/blocklist/block", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      alert("Block failed: " + (body.message || resp.status));
      return;
    }
  } catch (err) {
    alert("Block failed: " + err);
    return;
  }
  fetchStats(false);
}

async function unblockIp(ip) {
  try {
    const resp = await fetch("/api/blocklist/unblock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      alert("Unblock failed: " + (body.message || resp.status));
      return;
    }
  } catch (err) {
    alert("Unblock failed: " + err);
    return;
  }
  fetchStats(false);
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
//
// WAN only: the LAN lines were the same traffic seen from the other side
// (LAN TX ≈ WAN RX), and labelled RX/TX, which reads backwards for the LAN.
// Each point is a 1-minute bucket from the server's background sampler:
// the solid line is the minute's true average, the faint line above it the
// fastest 2-second burst in that minute (so a speed test shows its real
// ~900 Mbit/s even though the minute's average is much lower).
// X is real time (last 60 min), so 5 minutes of history no longer gets
// stretched across the whole width as if it were an hour.

const CHART_COLORS = {
  down: "#4a95e8",  // download — blue
  up: "#d9772f",    // upload — orange (validated vs blue for CVD on the dark surface)
};
let _trendData = null;
let _trendHoverX = null;

async function renderTrends() {
  try {
    const resp = await fetch("/api/trends", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    _trendData = await resp.json();
    drawBandwidthChart();
    renderTrendSummary();
  } catch (err) {
    console.warn("renderTrends error:", err);
  }
}

function fmtMbps(v, digits = 0) {
  if (v >= 1000) return (v / 1000).toFixed(v >= 10000 ? 0 : 2).replace(/\.?0+$/, "") + " Gbit/s";
  if (v > 0 && v < 1) return v.toFixed(2) + " Mbit/s";
  return v.toFixed(digits) + " Mbit/s";
}

function niceCeil(v) {
  // Round up to 1/2/2.5/5 × 10^n so gridlines land on readable values
  // (0/250/500/750/1000 rather than 0/231/462/693/924).
  if (v <= 0) return 10;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * p) return m * p;
  return 10 * p;
}

function renderTrendSummary() {
  const el = document.getElementById("trends-summary");
  if (!el || !_trendData) return;
  const wan = (_trendData.trends || {}).wan || [];
  if (!wan.length) { el.textContent = ""; return; }
  const pd = Math.max(...wan.map(d => d.rx_peak_mbps ?? d.rx_mbps ?? 0));
  const pu = Math.max(...wan.map(d => d.tx_peak_mbps ?? d.tx_mbps ?? 0));
  el.textContent = `Peak ↓ ${fmtMbps(pd)} · ↑ ${fmtMbps(pu)}`;
}

function drawBandwidthChart() {
  const canvas = document.getElementById("bandwidth-chart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  // Size the backing store to the displayed size × devicePixelRatio, so the
  // chart is crisp instead of a 400×150 bitmap stretched to the panel width.
  const cssW = canvas.clientWidth || 400;
  const cssH = 170;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.height = cssH + "px";
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const w = cssW, h = cssH;
  const css = getComputedStyle(document.documentElement);
  const muted = css.getPropertyValue("--text-muted").trim() || "#8b949e";
  const border = css.getPropertyValue("--border").trim() || "#30363d";

  ctx.clearRect(0, 0, w, h);

  const data = _trendData || {};
  const wan = (data.trends || {}).wan || [];
  const now = data.now || Date.now() / 1000;
  const windowSec = data.window_sec || 3600;
  const bucket = data.bucket_sec || 60;

  if (!wan.length) {
    ctx.fillStyle = muted;
    ctx.font = "13px " + (css.getPropertyValue("--font").trim() || "sans-serif");
    ctx.textAlign = "center";
    ctx.fillText("Collecting data — first point after 1 minute…", w / 2, h / 2);
    return;
  }

  const maxV = niceCeil(Math.max(
    10,
    ...wan.map(d => Math.max(d.rx_peak_mbps ?? d.rx_mbps ?? 0, d.tx_peak_mbps ?? d.tx_mbps ?? 0))
  ));

  const pad = { top: 8, right: 10, bottom: 22, left: 78 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;
  const t0 = now - windowSec;
  const xOf = ts => pad.left + ((ts - t0) / windowSec) * plotW;
  const yOf = v => pad.top + plotH - (Math.min(v, maxV) / maxV) * plotH;

  // Recessive grid + y labels
  ctx.font = "11px " + (css.getPropertyValue("--font-mono").trim() || "monospace");
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = (maxV / 4) * i;
    const y = Math.round(yOf(v)) + 0.5;
    ctx.strokeStyle = i === 0 ? muted : border;
    ctx.globalAlpha = i === 0 ? 0.6 : 0.6;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(w - pad.right, y); ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = muted;
    ctx.textAlign = "right";
    ctx.fillText(fmtMbps(v), pad.left - 6, y + 4);
  }
  // X labels (minutes ago)
  ctx.textAlign = "center";
  for (const m of [60, 45, 30, 15, 0]) {
    const x = xOf(now - m * 60);
    ctx.textAlign = m === 60 ? "left" : (m === 0 ? "right" : "center");
    ctx.fillText(m === 0 ? "now" : `−${m}m`, x, h - 6);
  }

  // Split into runs, breaking the line across gaps (dashboard restart, etc.)
  const runs = [];
  let run = [];
  for (const d of wan) {
    if (d.ts < t0 - bucket) continue;
    if (run.length && d.ts - run[run.length - 1].ts > bucket * 2.5) { runs.push(run); run = []; }
    run.push(d);
  }
  if (run.length) runs.push(run);

  function line(key, color, width, alpha) {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.globalAlpha = alpha;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    for (const r of runs) {
      ctx.beginPath();
      r.forEach((d, i) => {
        const x = xOf(d.ts), y = yOf(d[key] ?? 0);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      if (r.length === 1) { // single point: draw a short tick so it's visible
        const x = xOf(r[0].ts), y = yOf(r[0][key] ?? 0);
        ctx.moveTo(x - 2, y); ctx.lineTo(x + 2, y);
      }
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }
  function area(key, color, alpha) {
    ctx.fillStyle = color;
    ctx.globalAlpha = alpha;
    const base = yOf(0);
    for (const r of runs) {
      if (r.length < 2) continue;
      ctx.beginPath();
      ctx.moveTo(xOf(r[0].ts), base);
      r.forEach(d => ctx.lineTo(xOf(d.ts), yOf(d[key] ?? 0)));
      ctx.lineTo(xOf(r[r.length - 1].ts), base);
      ctx.closePath();
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  area("rx_mbps", CHART_COLORS.down, 0.18);
  line("rx_peak_mbps", CHART_COLORS.down, 1, 0.45);
  line("tx_peak_mbps", CHART_COLORS.up, 1, 0.45);
  line("rx_mbps", CHART_COLORS.down, 2, 1);
  line("tx_mbps", CHART_COLORS.up, 2, 1);

  // Hover: crosshair on the nearest minute + tooltip
  const tip = document.getElementById("trends-tooltip");
  if (_trendHoverX === null || !tip) { if (tip) tip.hidden = true; return; }
  let best = null;
  for (const d of wan) {
    const dx = Math.abs(xOf(d.ts) - _trendHoverX);
    if (dx < 20 && (!best || dx < best.dx)) best = { d, dx };
  }
  if (!best) { tip.hidden = true; return; }
  const d = best.d, x = Math.round(xOf(d.ts)) + 0.5;
  ctx.strokeStyle = muted; ctx.lineWidth = 1; ctx.globalAlpha = 0.7;
  ctx.beginPath(); ctx.moveTo(x, pad.top); ctx.lineTo(x, pad.top + plotH); ctx.stroke();
  ctx.globalAlpha = 1;
  for (const [key, color] of [["rx_mbps", CHART_COLORS.down], ["tx_mbps", CHART_COLORS.up]]) {
    ctx.fillStyle = color; ctx.strokeStyle = getComputedStyle(canvas).backgroundColor || "#1c2330";
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(x, yOf(d[key] ?? 0), 4, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
  }
  const time = new Date(d.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  tip.innerHTML =
    `<div class="tt-time">${escHtml(time)}</div>` +
    `<div><span class="legend-swatch" style="background:${CHART_COLORS.down}"></span>↓ ${escHtml(fmtMbps(d.rx_mbps ?? 0, 1))} <span class="tt-peak">peak ${escHtml(fmtMbps(d.rx_peak_mbps ?? d.rx_mbps ?? 0))}</span></div>` +
    `<div><span class="legend-swatch" style="background:${CHART_COLORS.up}"></span>↑ ${escHtml(fmtMbps(d.tx_mbps ?? 0, 1))} <span class="tt-peak">peak ${escHtml(fmtMbps(d.tx_peak_mbps ?? d.tx_mbps ?? 0))}</span></div>`;
  tip.hidden = false;
  const left = x > w / 2 ? x - tip.offsetWidth - 10 : x + 10;
  tip.style.left = Math.max(0, left) + "px";
}

(function initTrendHover() {
  const canvas = document.getElementById("bandwidth-chart");
  if (!canvas) return;
  const move = ev => {
    const r = canvas.getBoundingClientRect();
    const p = ev.touches ? ev.touches[0] : ev;
    _trendHoverX = p.clientX - r.left;
    drawBandwidthChart();
  };
  canvas.addEventListener("mousemove", move);
  canvas.addEventListener("touchstart", move, { passive: true });
  canvas.addEventListener("touchmove", move, { passive: true });
  canvas.addEventListener("mouseleave", () => { _trendHoverX = null; drawBandwidthChart(); });
})();

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
fetchStats(true);
renderTrends();
fetchPortForwards();
setInterval(() => fetchStats(false), REFRESH_MS);
setInterval(renderTrends, 10000);
window.addEventListener("resize", () => drawBandwidthChart());
setInterval(fetchPortForwards, 10000);
