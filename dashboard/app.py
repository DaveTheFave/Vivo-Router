#!/usr/bin/env python3
"""
Vivo-Router Dashboard — Flask backend
Provides real-time router statistics via a REST API consumed by the frontend.
"""

import os
import re
import json
import time
import subprocess
import logging
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, send_from_directory

# ── configuration ─────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONF_FILE = BASE_DIR / "router.conf"

def load_conf() -> dict:
    """Load key=value pairs from router.conf written by setup.sh."""
    conf = {
        "WAN_IFACE": "eth0",
        "LAN_IFACE": "eth1",
        "LAN_IP": "192.168.1.1",
        "PIHOLE_ENABLED": "false",
        "PIHOLE_API": "http://localhost/admin/api.php",
    }
    if CONF_FILE.exists():
        for line in CONF_FILE.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                conf[k.strip()] = v.strip()
    return conf

CONF = load_conf()

app = Flask(__name__, template_folder="templates", static_folder="static")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vivo-router")

# ── helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list[str], fallback: str = "") -> str:
    """Run a shell command and return stdout, returning fallback on error."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except Exception:
        return fallback


def _run_json(cmd: list[str]) -> dict | list | None:
    """Run a command that returns JSON."""
    out = _run(cmd)
    try:
        return json.loads(out)
    except Exception:
        return None


# ── data collectors ───────────────────────────────────────────────────────────

def get_wan_ip() -> str:
    """Detect WAN IP by querying the WAN interface address or a public endpoint."""
    iface = CONF["WAN_IFACE"]
    # Try the interface address first (faster, no external call)
    out = _run(["ip", "-4", "-o", "addr", "show", iface])
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    if match:
        return match.group(1)
    # Fallback: ask an external service
    for endpoint in [
        ["curl", "-s", "--max-time", "4", "https://api4.my-ip.io/ip"],
        ["curl", "-s", "--max-time", "4", "https://ifconfig.me"],
    ]:
        ip = _run(endpoint)
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
            return ip
    return "unavailable"


def get_link_speeds() -> dict:
    """
    Read TX/RX byte counters from /proc/net/dev and compute per-second rates.
    Uses a module-level cache to store the previous sample.
    """
    results = {}
    try:
        now = time.time()
        with open("/proc/net/dev") as f:
            lines = f.readlines()
        for line in lines[2:]:
            parts = line.split()
            if len(parts) < 10:
                continue
            iface = parts[0].rstrip(":")
            rx_bytes = int(parts[1])
            tx_bytes = int(parts[9])
            key = f"_prev_{iface}"
            prev = get_link_speeds._cache.get(key)
            if prev:
                dt = now - prev["ts"]
                if dt > 0:
                    rx_rate = (rx_bytes - prev["rx"]) / dt
                    tx_rate = (tx_bytes - prev["tx"]) / dt
                else:
                    rx_rate = tx_rate = 0.0
            else:
                rx_rate = tx_rate = 0.0
            get_link_speeds._cache[key] = {"ts": now, "rx": rx_bytes, "tx": tx_bytes}
            results[iface] = {
                "rx_bytes_sec": round(max(rx_rate, 0)),
                "tx_bytes_sec": round(max(tx_rate, 0)),
                "rx_human": _human_bytes(max(rx_rate, 0)),
                "tx_human": _human_bytes(max(tx_rate, 0)),
            }
    except Exception as exc:
        log.warning("get_link_speeds error: %s", exc)
    return results

get_link_speeds._cache = {}


def _human_bytes(bps: float) -> str:
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024:
            return f"{bps:.1f} {unit}"
        bps /= 1024
    return f"{bps:.1f} TB/s"


def get_active_clients() -> list:
    """Parse /var/lib/misc/dnsmasq.leases for active DHCP leases."""
    leases_file = Path("/var/lib/misc/dnsmasq.leases")
    clients = []
    if not leases_file.exists():
        return clients
    now = int(time.time())
    try:
        for line in leases_file.read_text().splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            expiry, mac, ip, hostname = parts[0], parts[1], parts[2], parts[3]
            hostname = hostname if hostname != "*" else "(unknown)"
            remaining = int(expiry) - now if expiry.isdigit() else -1
            if remaining > 0 or int(expiry) == 0:  # 0 = static lease
                clients.append({
                    "ip": ip,
                    "mac": mac,
                    "hostname": hostname,
                    "lease_remaining": remaining,
                })
    except Exception as exc:
        log.warning("get_active_clients error: %s", exc)
    return clients


def get_system_stats() -> dict:
    """CPU, RAM, disk, uptime, temperature."""
    stats = {}

    # CPU
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        parts = list(map(int, line.split()[1:]))
        idle = parts[3]
        total = sum(parts)
        prev = get_system_stats._prev_cpu
        if prev:
            d_total = total - prev["total"]
            d_idle = idle - prev["idle"]
            stats["cpu_percent"] = round(100 * (1 - d_idle / max(d_total, 1)), 1)
        else:
            stats["cpu_percent"] = 0.0
        get_system_stats._prev_cpu = {"total": total, "idle": idle}
    except Exception:
        stats["cpu_percent"] = None

    # RAM
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                mem[k.strip()] = int(v.split()[0])
        total_mem = mem.get("MemTotal", 1)
        avail_mem = mem.get("MemAvailable", 0)
        used_mem = total_mem - avail_mem
        stats["ram_total_mb"] = round(total_mem / 1024)
        stats["ram_used_mb"] = round(used_mem / 1024)
        stats["ram_percent"] = round(100 * used_mem / max(total_mem, 1), 1)
    except Exception:
        stats["ram_total_mb"] = stats["ram_used_mb"] = stats["ram_percent"] = None

    # Disk
    try:
        df_out = _run(["df", "-BM", "--output=size,used,avail,pcent", "/"])
        lines = df_out.splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            stats["disk_total"] = parts[0]
            stats["disk_used"] = parts[1]
            stats["disk_avail"] = parts[2]
            stats["disk_percent"] = int(parts[3].rstrip("%"))
    except Exception:
        stats.update({"disk_total": None, "disk_used": None,
                      "disk_avail": None, "disk_percent": None})

    # Uptime
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        days = int(secs // 86400)
        hours = int((secs % 86400) // 3600)
        mins = int((secs % 3600) // 60)
        stats["uptime"] = f"{days}d {hours}h {mins}m"
        stats["uptime_seconds"] = int(secs)
    except Exception:
        stats["uptime"] = stats["uptime_seconds"] = None

    # CPU Temperature
    try:
        temp_paths = list(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
        if temp_paths:
            temp_c = int(temp_paths[0].read_text().strip()) / 1000
            stats["cpu_temp_c"] = round(temp_c, 1)
        else:
            stats["cpu_temp_c"] = None
    except Exception:
        stats["cpu_temp_c"] = None

    return stats

get_system_stats._prev_cpu = None


def get_pihole_stats() -> dict:
    """Fetch Pi-hole summary statistics via its local API."""
    default = {
        "enabled": False,
        "status": "disabled",
        "domains_blocked": 0,
        "queries_today": 0,
        "ads_blocked_today": 0,
        "ads_percentage_today": 0.0,
        "unique_clients": 0,
    }

    if CONF.get("PIHOLE_ENABLED", "false").lower() != "true":
        return default

    try:
        import urllib.request
        url = CONF["PIHOLE_API"] + "?summary"
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode())

        return {
            "enabled": data.get("status") == "enabled",
            "status": data.get("status", "unknown"),
            "domains_blocked": data.get("domains_being_blocked", 0),
            "queries_today": data.get("dns_queries_today", 0),
            "ads_blocked_today": data.get("ads_blocked_today", 0),
            "ads_percentage_today": round(
                float(data.get("ads_percentage_today", 0)), 1
            ),
            "unique_clients": data.get("unique_clients", 0),
        }
    except Exception as exc:
        log.warning("Pi-hole API error: %s", exc)
        return {**default, "status": "error", "error": "Pi-hole API unavailable"}


def get_recent_errors(n: int = 20) -> list:
    """
    Return the last n error/warning lines from dnsmasq and syslog.
    """
    errors = []
    sources = [
        ("/var/log/dnsmasq.log", r"(error|warning|failed|refused)", "dnsmasq"),
        ("/var/log/syslog", r"(kernel:|error|segfault|DHCP)", "syslog"),
    ]
    for path, pattern, source in sources:
        p = Path(path)
        if not p.exists():
            continue
        try:
            lines = p.read_text(errors="replace").splitlines()
            for line in reversed(lines[-500:]):
                if re.search(pattern, line, re.IGNORECASE):
                    errors.append({"source": source, "message": line.strip()})
                    if len(errors) >= n:
                        break
        except Exception:
            pass
    return errors[:n]


def get_firewall_status() -> dict:
    """Return nftables service status and a summary of the active ruleset."""
    status = {
        "service": "unknown",
        "active": False,
        "rules_summary": [],
    }

    # Service status
    svc = _run(["systemctl", "is-active", "nftables"])
    status["service"] = svc if svc else "inactive"
    status["active"] = svc == "active"

    # Quick rule count per chain via nft list ruleset
    try:
        raw = _run(["nft", "list", "ruleset"])
        chains = re.findall(r"chain\s+(\S+)\s*\{", raw)
        tables = re.findall(r"table\s+\w+\s+(\S+)\s*\{", raw)
        rule_lines = [l.strip() for l in raw.splitlines()
                      if l.strip() and not l.strip().startswith(("#", "table", "chain", "}", "{", "flush"))]
        status["rules_summary"] = [
            {"key": "Tables", "value": ", ".join(dict.fromkeys(tables)) or "–"},
            {"key": "Chains", "value": str(len(chains))},
            {"key": "Rules",  "value": str(len(rule_lines))},
        ]
    except Exception as exc:
        log.warning("get_firewall_status error: %s", exc)

    return status


def get_port_forwards() -> list:
    """Return static port-forward rules from router.conf."""
    forwards = []
    for line in CONF_FILE.read_text().splitlines() if CONF_FILE.exists() else []:
        line = line.strip()
        if line.startswith("PORT_FORWARD="):
            value = line.partition("=")[2].strip()
            parts = value.split(":")
            if len(parts) == 4:
                proto, ext_port, int_ip, int_port = parts
                forwards.append({
                    "proto":    proto.upper(),
                    "ext_port": ext_port,
                    "int_ip":   int_ip,
                    "int_port": int_port,
                })
    return forwards


def get_upnp_leases() -> list:
    """
    Parse /var/log/miniupnpd.leases for active UPnP/NAT-PMP mappings.
    File format: proto|ext_port|int_ip|int_port|description|expiry_ts
    """
    leases_file = Path("/var/log/miniupnpd.leases")
    leases = []
    if not leases_file.exists():
        return leases
    now = int(time.time())
    try:
        for line in leases_file.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 6:
                continue
            proto, ext_port, int_ip, int_port, desc, expiry = (
                parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
            )
            try:
                exp_ts = int(expiry)
                remaining = exp_ts - now if exp_ts > 0 else -1
            except ValueError:
                remaining = -1
            leases.append({
                "proto":     proto.upper(),
                "ext_port":  ext_port,
                "int_ip":    int_ip,
                "int_port":  int_port,
                "desc":      desc or "(unknown)",
                "remaining": remaining,
            })
    except Exception as exc:
        log.warning("get_upnp_leases error: %s", exc)
    return leases


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", conf=CONF)


@app.route("/api/stats")
def api_stats():
    """Master endpoint — returns all dashboard data in one call."""
    wan_ip = get_wan_ip()
    speeds = get_link_speeds()
    clients = get_active_clients()
    system = get_system_stats()
    pihole = get_pihole_stats()
    errors = get_recent_errors()
    firewall = get_firewall_status()
    port_forwards = get_port_forwards()
    upnp = get_upnp_leases()

    wan_iface = CONF["WAN_IFACE"]
    lan_iface = CONF["LAN_IFACE"]

    return jsonify({
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "wan_ip": wan_ip,
        "_wan_iface": wan_iface,
        "_lan_iface": lan_iface,
        "wan_speed": speeds.get(wan_iface, {}),
        "lan_speed": speeds.get(lan_iface, {}),
        "active_clients": clients,
        "active_client_count": len(clients),
        "system": system,
        "pihole": pihole,
        "recent_errors": errors,
        "firewall": firewall,
        "port_forwards": port_forwards,
        "upnp_leases": upnp,
    })


@app.route("/api/clients")
def api_clients():
    return jsonify(get_active_clients())


@app.route("/api/pihole")
def api_pihole():
    return jsonify(get_pihole_stats())


@app.route("/api/speeds")
def api_speeds():
    return jsonify(get_link_speeds())


@app.route("/api/system")
def api_system():
    return jsonify(get_system_stats())


@app.route("/api/firewall")
def api_firewall():
    return jsonify(get_firewall_status())


@app.route("/api/upnp")
def api_upnp():
    return jsonify({
        "enabled": CONF.get("UPNP_ENABLED", "false").lower() == "true",
        "leases": get_upnp_leases(),
    })


@app.route("/api/portforwards")
def api_portforwards():
    return jsonify(get_port_forwards())


if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.environ.get("DASHBOARD_PORT", 5000))
    log.info("Vivo-Router Dashboard starting on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False, threaded=True)
