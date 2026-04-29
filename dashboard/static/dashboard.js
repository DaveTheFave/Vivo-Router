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
  renderErrors(data.recent_errors || []);
  renderFirewall(data.firewall || {});
  renderPortForwards(data.port_forwards || []);
  renderUpnp(data.upnp_leases || [], data.firewall);
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
    tbody.innerHTML = '<tr><td colspan="4" class="empty-row">No active DHCP leases</td></tr>';
    return;
  }

  tbody.innerHTML = clients.map(c => `
    <tr>
      <td>${escHtml(c.hostname || "(unknown)")}</td>
      <td class="ip">${escHtml(c.ip)}</td>
      <td class="mac">${escHtml(c.mac)}</td>
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

// ── XSS safe helper ───────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
}

// ── firewall status ───────────────────────────────────────────────────────────
function renderFirewall(fw) {
  const badge = document.getElementById("fw-badge");
  if (badge) {
    const state = fw.active ? "active" : (fw.service || "unknown");
    badge.textContent = fw.service || "unknown";
    badge.className = "fw-status-badge " + (fw.active ? "active" : (fw.service === "inactive" ? "inactive" : "unknown"));
  }

  const summary = fw.rules_summary || [];
  const ids = { "Tables": "fw-tables", "Chains": "fw-chains", "Rules": "fw-rules" };
  for (const item of summary) {
    const el = document.getElementById(ids[item.key]);
    if (el) el.textContent = item.value;
  }
}

// ── static port forwards ──────────────────────────────────────────────────────
function renderPortForwards(forwards) {
  setText("pf-count", String(forwards.length));
  const tbody = document.getElementById("pf-tbody");
  if (!tbody) return;

  if (forwards.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-row">No static rules configured</td></tr>';
    return;
  }

  tbody.innerHTML = forwards.map(r => `
    <tr>
      <td class="proto ${escHtml(r.proto.toLowerCase())}">${escHtml(r.proto)}</td>
      <td class="port">${escHtml(r.ext_port)}</td>
      <td style="color:var(--text-muted)">→</td>
      <td class="ip">${escHtml(r.int_ip)}</td>
      <td class="port">${escHtml(r.int_port)}</td>
    </tr>
  `).join("");
}

// ── UPnP leases ───────────────────────────────────────────────────────────────
function renderUpnp(leases, fw) {
  // derive enabled state from firewall data (upnp_enabled not in api/stats root yet)
  const badge = document.getElementById("upnp-badge");
  if (badge) {
    // if there are leases, UPnP is clearly active
    const enabled = leases.length > 0;
    badge.textContent = enabled ? "active" : "–";
    badge.className = "upnp-status-badge " + (enabled ? "enabled" : "disabled");
  }

  setText("upnp-count", String(leases.length));
  const tbody = document.getElementById("upnp-tbody");
  if (!tbody) return;

  if (leases.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-row">No active UPnP mappings</td></tr>';
    return;
  }

  tbody.innerHTML = leases.map(r => `
    <tr>
      <td class="proto ${escHtml(r.proto.toLowerCase())}">${escHtml(r.proto)}</td>
      <td class="port">${escHtml(r.ext_port)}</td>
      <td style="color:var(--text-muted)">→</td>
      <td class="ip">${escHtml(r.int_ip)}</td>
      <td class="port">${escHtml(r.int_port)}</td>
      <td>${escHtml(r.desc)}</td>
      <td><span class="lease-pill">${fmtLease(r.remaining)}</span></td>
    </tr>
  `).join("");
}

// ── polling loop ──────────────────────────────────────────────────────────────
fetchStats(); // first call immediately
setInterval(fetchStats, REFRESH_MS);
