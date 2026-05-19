#!/usr/bin/env python3
"""
Vivo-Router Dashboard — Flask backend
Provides real-time router statistics via a REST API consumed by the frontend.
"""

import os
import re
import ssl
import json
import time
import uuid
import threading
import subprocess
import logging
import urllib.error
import urllib.parse
import urllib.request
import concurrent.futures
from pathlib import Path
from datetime import datetime, timezone
from collections import deque

from flask import Flask, jsonify, render_template, request

# ── configuration ─────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONF_FILE = BASE_DIR / "router.conf"
ENV_FILE = BASE_DIR / ".env"


def _load_dotenv(path: Path) -> dict:
    """Load KEY=VALUE pairs from a .env file."""
    env = {}
    if not path.exists():
        return env

    for raw_line in path.read_text(errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        # Support common .env quoting style.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]

        if key:
            env[key] = value

    return env

def load_conf() -> dict:
    """Load runtime config from router.conf, then override with .env and process env."""
    conf = {
        "WAN_IFACE": "eth0",
        "LAN_IFACE": "eth1",
        "LAN_IP": "192.168.1.1",
        "PIHOLE_ENABLED": "false",
        "PIHOLE_API": "http://localhost/api",
        "PIHOLE_PASSWORD": "",
    }
    if CONF_FILE.exists():
        for line in CONF_FILE.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                conf[k.strip()] = v.strip()

    # Allow secrets such as PIHOLE_PASSWORD to live outside router.conf.
    conf.update(_load_dotenv(ENV_FILE))

    # Environment variables have final precedence.
    for key in conf:
        env_val = os.environ.get(key)
        if env_val is not None:
            conf[key] = env_val

    return conf

CONF = load_conf()

PORT_FORWARDS_FILE = BASE_DIR / "port_forwards.json"
_pf_lock = threading.Lock()

app = Flask(__name__, template_folder="templates", static_folder="static")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vivo-router")

# ── bandwidth history (rolling 1-hour buffer) ─────────────────────────────────

class BandwidthHistory:
    """Maintain rolling 1-hour history of WAN/LAN bandwidth samples."""
    def __init__(self, max_samples=60):
        self.wan_history = deque(maxlen=max_samples)
        self.lan_history = deque(maxlen=max_samples)
        self.last_sample_time = 0
        self.sample_interval = 60  # sample every 60 seconds

    def update(self, wan_speed: dict, lan_speed: dict):
        """Add a sample to the history if enough time has passed."""
        now = time.time()
        if now - self.last_sample_time < self.sample_interval:
            return
        self.last_sample_time = now
        
        self.wan_history.append({
            "ts": now,
            "rx_mbps": (wan_speed.get("rx_bytes_sec", 0) * 8) / 1_000_000,
            "tx_mbps": (wan_speed.get("tx_bytes_sec", 0) * 8) / 1_000_000,
        })
        self.lan_history.append({
            "ts": now,
            "rx_mbps": (lan_speed.get("rx_bytes_sec", 0) * 8) / 1_000_000,
            "tx_mbps": (lan_speed.get("tx_bytes_sec", 0) * 8) / 1_000_000,
        })

    def get_history(self) -> dict:
        """Return current history as dict of lists."""
        return {
            "wan": list(self.wan_history),
            "lan": list(self.lan_history),
        }

bandwidth_history = BandwidthHistory()


class GatewayTimeoutTracker:
    """Track gateway disconnect windows and cumulative timeout duration."""

    def __init__(self, max_events: int = 30):
        self._lock = threading.Lock()
        self._max_events = max_events
        self._status = "unknown"
        self._disconnected_since: float | None = None
        self._timeout_count = 0
        self._total_timeout_seconds = 0.0
        self._events: deque[dict] = deque(maxlen=max_events)

    @staticmethod
    def _is_timed_out(status: str) -> bool:
        return status in {"down", "error", "unknown"}

    @staticmethod
    def _iso(ts: float | None) -> str | None:
        if ts is None:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat()

    def _snapshot(self, now: float) -> dict:
        current_outage = 0
        if self._disconnected_since is not None:
            current_outage = max(0, round(now - self._disconnected_since))

        last_event = self._events[-1] if self._events else None
        return {
            "status": self._status,
            "is_timed_out": self._disconnected_since is not None,
            "disconnected_since": self._iso(self._disconnected_since),
            "current_timeout_seconds": current_outage,
            "timeout_count": self._timeout_count,
            "total_timeout_seconds": round(self._total_timeout_seconds),
            "last_timeout_duration_seconds": (
                last_event.get("duration_seconds") if last_event else None
            ),
            "last_timeout_started_at": (
                last_event.get("started_at") if last_event else None
            ),
            "last_timeout_ended_at": (
                last_event.get("ended_at") if last_event else None
            ),
            "recent_timeouts": list(self._events),
        }

    def update(self, gateway_health: dict) -> dict:
        now = time.time()
        new_status = gateway_health.get("status", "unknown")
        timed_out = self._is_timed_out(new_status)

        with self._lock:
            was_timed_out = self._disconnected_since is not None

            if timed_out and not was_timed_out:
                self._disconnected_since = now
                self._timeout_count += 1
            elif not timed_out and was_timed_out:
                started_at = self._disconnected_since
                duration = max(0.0, now - started_at)
                self._total_timeout_seconds += duration
                self._events.append({
                    "started_at": self._iso(started_at),
                    "ended_at": self._iso(now),
                    "duration_seconds": round(duration),
                })
                self._disconnected_since = None

            self._status = new_status
            return self._snapshot(now)


gateway_timeout_tracker = GatewayTimeoutTracker()

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


def get_gateway_health() -> dict:
    """Check if the default gateway is reachable (ping test)."""
    try:
        # Get default gateway from routing table
        route_out = _run(["ip", "route", "show", "default"])
        if not route_out:
            return {"status": "unknown", "response_time_ms": None, "error": "No default route"}
        
        match = re.search(r"via (\d+\.\d+\.\d+\.\d+)", route_out)
        if not match:
            return {"status": "unknown", "response_time_ms": None, "error": "Could not parse gateway"}
        
        gateway = match.group(1)
        
        # Ping the gateway (1 ping, 2 second timeout)
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", gateway],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            # Extract response time from output (e.g., "time=45.2 ms")
            match = re.search(r"time=([0-9.]+)\s*ms", result.stdout)
            response_time = float(match.group(1)) if match else None
            return {
                "status": "up",
                "gateway": gateway,
                "response_time_ms": round(response_time, 1) if response_time else None,
            }
        else:
            return {"status": "down", "gateway": gateway, "response_time_ms": None}
    except Exception as exc:
        log.warning("get_gateway_health error: %s", exc)
        return {"status": "error", "response_time_ms": None, "error": str(exc)}


def get_dns_health() -> dict:
    """Check if DNS is resolving (test domain resolution)."""
    try:
        import socket
        
        # Try to resolve a test domain (example.com)
        start = time.time()
        try:
            # Use socket.getaddrinfo which is the standard Python DNS resolution
            socket.getaddrinfo("example.com", 80, socket.AF_INET, socket.SOCK_STREAM)
            elapsed_ms = round((time.time() - start) * 1000, 1)
            return {
                "status": "ok",
                "response_time_ms": elapsed_ms,
                "test_domain": "example.com",
            }
        except socket.gaierror:
            return {"status": "failed", "response_time_ms": None, "error": "Name resolution failed"}
    except Exception as exc:
        log.warning("get_dns_health error: %s", exc)
        return {"status": "error", "response_time_ms": None, "error": str(exc)}


def get_isp_health() -> dict:
    """Check internet reachability by pinging known external IPs (no DNS required)."""
    test_ips = ["1.1.1.1", "8.8.8.8"]
    for ip in test_ips:
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "2", ip],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                match = re.search(r"time=([0-9.]+)\s*ms", result.stdout)
                response_time = float(match.group(1)) if match else None
                return {
                    "status": "up",
                    "tested_ip": ip,
                    "response_time_ms": round(response_time, 1) if response_time else None,
                }
        except Exception:
            continue
    return {"status": "down", "tested_ip": test_ips[0], "response_time_ms": None}


def get_ap_health() -> dict:
    """Ping the configured AP to check reachability."""
    if CONF.get("AP_ENABLED", "false").lower() != "true":
        return {"status": "disabled"}
    ap_ip = CONF.get("AP_IP", "").strip()
    if not ap_ip:
        return {"status": "unconfigured"}
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ap_ip):
        return {"status": "error", "error": "Invalid AP_IP in config"}
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", ap_ip],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            match = re.search(r"time=([0-9.]+)\s*ms", result.stdout)
            response_time = float(match.group(1)) if match else None
            return {
                "status": "up",
                "ap_ip": ap_ip,
                "response_time_ms": round(response_time, 1) if response_time else None,
            }
        return {"status": "down", "ap_ip": ap_ip, "response_time_ms": None}
    except Exception as exc:
        log.warning("get_ap_health error: %s", exc)
        return {"status": "error", "ap_ip": ap_ip, "error": str(exc)}


def get_network_diagnosis(gateway: dict, isp: dict, dns: dict, ap: dict) -> dict:
    """Correlate all health checks and assign a blame/fault label."""
    issues = []
    blame = "none"   # none | gateway | isp | dns | ap
    overall = "ok"   # ok | warning | critical

    gw_up  = gateway.get("status") == "up"
    isp_up = isp.get("status") == "up"
    dns_ok = dns.get("status") == "ok"
    ap_status = ap.get("status")
    ap_active = ap_status not in ("disabled", "unconfigured")

    if not gw_up:
        issues.append("Gateway unreachable — check your router or modem connection")
        blame = "gateway"
        overall = "critical"
    elif not isp_up:
        issues.append("ISP Down — gateway is reachable but the internet is not")
        blame = "isp"
        overall = "critical"

    if gw_up and not dns_ok:
        issues.append("DNS resolution is failing")
        if overall == "ok":
            overall = "warning"
            blame = "dns"

    if ap_active and ap_status != "up":
        issues.append("No connection to AP — the access point is not responding")
        if overall == "ok":
            overall = "warning"
        if blame == "none":
            blame = "ap"

    return {"overall": overall, "blame": blame, "issues": issues}


def get_health_checks() -> dict:
    """Run gateway, ISP, DNS, and AP checks in parallel and return combined status."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        f_gw  = ex.submit(get_gateway_health)
        f_isp = ex.submit(get_isp_health)
        f_dns = ex.submit(get_dns_health)
        f_ap  = ex.submit(get_ap_health)
        gateway = f_gw.result(timeout=8)
        isp     = f_isp.result(timeout=8)
        dns     = f_dns.result(timeout=8)
        ap      = f_ap.result(timeout=8)

    diagnosis = get_network_diagnosis(gateway, isp, dns, ap)
    return {
        "gateway": gateway,
        "isp": isp,
        "dns": dns,
        "ap": ap,
        "diagnosis": diagnosis,
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def get_arp_clients() -> dict:
    """Scan ARP table to discover connected clients.

    Returns dict mapping MAC -> {mac, ip, hostname}.
    Filters to LAN interface to avoid router management traffic.
    """
    import socket
    clients = {}
    lan_iface = CONF.get("LAN_IFACE", "eth1")

    try:
        with open("/proc/net/arp") as f:
            for line in f.readlines()[1:]:  # Skip header
                parts = line.split()
                if len(parts) < 4:
                    continue

                ip = parts[0]
                hw_type = parts[1]
                mac = parts[3].lower()
                device = parts[5] if len(parts) > 5 else ""

                # Skip invalid MACs and filter to LAN interface
                if mac == "00:00:00:00:00:00" or device != lan_iface:
                    continue

                # Validate MAC format
                if not re.match(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac, re.I):
                    continue

                clients[mac] = {
                    "mac": mac,
                    "ip": ip,
                    "hostname": "(unknown)",
                }
    except Exception as exc:
        log.warning("get_arp_clients error: %s", exc)

    return clients


def resolve_hostname_avahi(ip: str, timeout: float = 1.5) -> str | None:
    """Resolve IP to hostname using avahi-resolve (mDNS).

    Returns hostname (e.g., "device.local") or None if resolution fails.
    """
    try:
        result = subprocess.run(
            ["avahi-resolve", "-a", ip],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False
        )
        if result.returncode == 0 and result.stdout:
            # Parse: "192.168.1.100<TAB>device.local"
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                hostname = parts[1].rstrip('.')
                return hostname
        return None
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def resolve_hostname_reverse_dns(ip: str, timeout: float = 1.5) -> str | None:
    """Fallback: Resolve IP to hostname using reverse DNS."""
    import socket
    try:
        socket.setdefaulttimeout(timeout)
        hostname = socket.gethostbyaddr(ip)[0]
        return hostname
    except (socket.herror, socket.gaierror, socket.timeout):
        return None
    finally:
        socket.setdefaulttimeout(None)


def get_hostname(ip: str, timeout: float = 2.0) -> str | None:
    """Resolve hostname with fallback chain.

    1. Try avahi-resolve (mDNS) — primary for local network
    2. Try reverse DNS — fallback
    3. Return None if all fail
    """
    # Try mDNS first (fast on LANs)
    hostname = resolve_hostname_avahi(ip, timeout * 0.7)
    if hostname:
        return hostname

    # Fallback to reverse DNS
    hostname = resolve_hostname_reverse_dns(ip, timeout * 0.3)
    return hostname


_hostname_cache: dict = {}  # {ip: (hostname, resolved_timestamp)}
HOSTNAME_CACHE_TTL = 300  # seconds — re-resolve at most once per 5 minutes


def get_hostnames_batch(ips: list, timeout: float = 1.5, max_workers: int = 5) -> dict:
    """Resolve multiple IPs in parallel.

    Returns dict mapping IP -> hostname (or IP if resolution fails).
    """
    import concurrent.futures
    results = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(get_hostname, ip, timeout): ip
            for ip in ips
        }

        # Use a longer timeout for as_completed to allow all workers to finish
        # timeout + (max_workers * overhead) to account for thread scheduling
        total_timeout = timeout + max_workers
        try:
            for future in concurrent.futures.as_completed(futures, timeout=total_timeout):
                ip = futures[future]
                try:
                    hostname = future.result()
                    results[ip] = hostname or ip
                except Exception:
                    results[ip] = ip
        except concurrent.futures.TimeoutError:
            # Some futures didn't complete in time, add them with IP as fallback
            for future in futures:
                if not future.done():
                    ip = futures[future]
                    results[ip] = ip

    return results




def get_active_clients() -> list:
    """Discover active clients using ARP + parallel hostname resolution."""
    # Get clients from ARP table
    arp_clients = get_arp_clients()
    if not arp_clients:
        return []


    # Resolve hostnames; only look up IPs missing from cache or past TTL
    ips = [client["ip"] for client in arp_clients.values()]
    now = time.time()
    stale = [ip for ip in ips
             if ip not in _hostname_cache
             or now - _hostname_cache[ip][1] >= HOSTNAME_CACHE_TTL]
    if stale:
        fresh = get_hostnames_batch(stale, timeout=1.5, max_workers=5)
        for ip, hostname in fresh.items():
            _hostname_cache[ip] = (hostname, now)
    hostnames = {ip: _hostname_cache.get(ip, (ip, 0))[0] for ip in ips}

    # Update clients with resolved hostnames.
    for client in arp_clients.values():
        client["hostname"] = hostnames.get(client["ip"], "(unknown)")

    # Build final client list with normalized fields expected by the UI.
    clients = []
    for mac, arp_data in arp_clients.items():
        clients.append({
            "mac": mac,
            "ip": arp_data["ip"],
            "hostname": arp_data["hostname"],
            "lease_remaining": -1,
            "rx_bytes": 0,
            "tx_bytes": 0,
            "rx_human": _human_bytes(0),
            "tx_human": _human_bytes(0),
            "total_human": _human_bytes(0),
        })

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
    """Fetch Pi-hole summary statistics via Pi-hole API (v5/v6 compatible)."""
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

    api_base = (CONF.get("PIHOLE_API") or "http://localhost/api").strip()

    try:
        # Pi-hole v6 API lives at /api and uses session-based auth.
        if api_base.rstrip("/").endswith("/admin/api.php"):
            parsed = urllib.parse.urlsplit(api_base)
            api_base = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, "/api", "", "")
            )

        if api_base.rstrip("/").endswith("/api"):
            return _get_pihole_stats_v6(api_base, default)

        return _get_pihole_stats_legacy(api_base, default)
    except Exception as exc:
        log.warning("Pi-hole API error: %s", exc)
        return {**default, "status": "error", "error": "Pi-hole API unavailable"}


def _http_json(url: str, *, timeout: int = 3, method: str = "GET",
               headers: dict | None = None, body: dict | None = None,
               verify_ssl: bool = True) -> tuple[int, dict]:
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

    ctx = None
    if not verify_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
            return resp.status, payload
    except urllib.error.HTTPError as err:
        try:
            payload = json.loads(err.read().decode("utf-8") or "{}")
        except Exception:
            payload = {}
        return err.code, payload


_pihole_sid: dict = {"sid": None, "expires": 0.0}
_pihole_cache: dict = {"data": None, "expires": 0.0}
PIHOLE_CACHE_TTL = 30  # seconds — avoid hammering Pi-hole on every 5 s poll


def _resolve_pihole_password() -> str:
    conf_pw = (CONF.get("PIHOLE_PASSWORD") or "").strip()
    if conf_pw:
        return conf_pw

    cli_pw = Path("/etc/pihole/cli_pw")
    if cli_pw.exists():
        try:
            return cli_pw.read_text(errors="replace").strip()
        except Exception:
            return ""
    return ""


def _pihole_authenticate(api_base: str) -> str | None:
    """Return a valid session SID, reusing the cached value while it lives."""
    now = time.time()
    if _pihole_sid["sid"] and now < _pihole_sid["expires"]:
        return _pihole_sid["sid"]

    password = _resolve_pihole_password()
    if not password:
        return None

    sc, login = _http_json(
        f"{api_base}/auth",
        method="POST",
        body={"password": password},
        verify_ssl=False,
    )
    if sc != 200:
        return None

    session = login.get("session") or {}
    sid = session.get("sid")
    validity = float(session.get("validity") or 270)
    if sid:
        _pihole_sid["sid"] = sid
        _pihole_sid["expires"] = now + validity - 30
    return sid


def _get_pihole_stats_v6(api_base: str, default: dict) -> dict:
    now = time.time()
    if _pihole_cache["data"] and now < _pihole_cache["expires"]:
        return _pihole_cache["data"]

    base = api_base.rstrip("/")

    # Check for anonymous access (Pi-hole with no password returns valid=true, sid=null)
    sc, auth_info = _http_json(f"{base}/auth", verify_ssl=False)
    session = auth_info.get("session") or {}
    sid = session.get("sid")

    if sc == 401 or not session.get("valid", False):
        sid = _pihole_authenticate(base)
        if sid is None:
            return {**default, "status": "auth_required",
                    "error": "Pi-hole API password required"}

    headers = {"sid": sid} if sid else {}

    sc, summary = _http_json(f"{base}/stats/summary", headers=headers, verify_ssl=False)
    if sc == 401:
        # Cached SID expired; force re-auth once
        _pihole_sid.update({"sid": None, "expires": 0.0})
        sid = _pihole_authenticate(base)
        headers = {"sid": sid} if sid else {}
        sc, summary = _http_json(f"{base}/stats/summary", headers=headers, verify_ssl=False)
    if sc != 200:
        raise RuntimeError(f"Pi-hole v6 summary failed ({sc})")

    queries = summary.get("queries") or {}
    clients = summary.get("clients") or {}
    gravity = summary.get("gravity") or {}

    _, top_blocked_resp = _http_json(
        f"{base}/stats/top_domains?count=5&blocked=true",
        headers=headers, verify_ssl=False,
    )
    _, top_queried_resp = _http_json(
        f"{base}/stats/top_domains?count=5",
        headers=headers, verify_ssl=False,
    )
    _, qt_resp = _http_json(
        f"{base}/stats/query_types",
        headers=headers, verify_ssl=False,
    )

    def _parse_domains(resp: dict) -> list:
        return [
            {"domain": d.get("domain", "?"), "count": d.get("count", 0)}
            for d in (resp.get("domains") or [])
        ]

    result = {
        "enabled": True,
        "status": "enabled",
        "domains_blocked": gravity.get("domains_being_blocked", 0),
        "queries_today": queries.get("total", 0),
        "ads_blocked_today": queries.get("blocked", 0),
        "ads_percentage_today": round(float(queries.get("percent_blocked", 0) or 0), 1),
        "unique_clients": clients.get("active", 0),
        "top_blocked": _parse_domains(top_blocked_resp),
        "top_queried": _parse_domains(top_queried_resp),
        "query_types": {k: v for k, v in (qt_resp.get("types") or {}).items() if v > 0},
    }

    _pihole_cache.update({"data": result, "expires": now + PIHOLE_CACHE_TTL})
    return result


def _get_pihole_stats_legacy(api_base: str, default: dict) -> dict:
    url = api_base
    if "?" not in url:
        joiner = "&" if url.endswith("/") else "?"
        url = f"{url}{joiner}summary"
    status_code, data = _http_json(url)
    if status_code != 200:
        raise RuntimeError(f"Pi-hole legacy summary request failed ({status_code})")

    return {
        "enabled": data.get("status") == "enabled",
        "status": data.get("status", "unknown"),
        "domains_blocked": data.get("domains_being_blocked", 0),
        "queries_today": data.get("dns_queries_today", 0),
        "ads_blocked_today": data.get("ads_blocked_today", 0),
        "ads_percentage_today": round(float(data.get("ads_percentage_today", 0) or 0), 1),
        "unique_clients": data.get("unique_clients", 0),
    }


def _load_port_forwards() -> list:
    with _pf_lock:
        if not PORT_FORWARDS_FILE.exists():
            return []
        try:
            return json.loads(PORT_FORWARDS_FILE.read_text())
        except Exception:
            return []


def _save_port_forwards(entries: list) -> None:
    with _pf_lock:
        PORT_FORWARDS_FILE.write_text(json.dumps(entries, indent=2))


def get_nftables_forwards() -> list:
    """Return list of DNAT port forward rules from nftables.

    Tries JSON output first (nft -j), falls back to text parsing.
    Each entry: {protocol, external_port, internal_ip, internal_port, table, chain}
    """
    rules = _parse_nft_json()
    if rules is not None:
        return rules
    return _parse_nft_text()


def _parse_nft_json() -> list | None:
    """Parse 'nft -j list ruleset'. Returns None if JSON output unavailable."""
    try:
        result = subprocess.run(
            ["nft", "-j", "list", "ruleset"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        data = json.loads(result.stdout)
    except Exception:
        return None

    rules = []
    for item in data.get("nftables", []):
        rule = item.get("rule")
        if not rule:
            continue
        # Only DNAT rules in nat/prerouting chains
        if rule.get("table", "").lower() != "nat":
            continue
        if rule.get("chain", "").lower() not in ("prerouting",):
            continue

        proto = None
        ext_port = None
        dnat_addr = None
        dnat_port = None

        for expr in rule.get("expr", []):
            if "match" in expr:
                left = expr["match"].get("left", {})
                right = expr["match"].get("right")
                if isinstance(left, dict):
                    payload = left.get("payload", {})
                    if payload.get("field") == "dport" and isinstance(right, int):
                        proto = proto or payload.get("protocol", "tcp")
                        ext_port = right
                    meta = left.get("meta", {})
                    if meta.get("key") == "l4proto" and isinstance(right, str):
                        proto = proto or right
            if "dnat" in expr:
                dnat_addr = expr["dnat"].get("addr")
                dnat_port = expr["dnat"].get("port")

        if ext_port and dnat_addr:
            rules.append({
                "protocol": (proto or "tcp").lower(),
                "external_port": ext_port,
                "internal_ip": dnat_addr,
                "internal_port": dnat_port or ext_port,
                "table": rule.get("table", "nat"),
                "chain": rule.get("chain", "prerouting"),
            })
    return rules


def _parse_nft_text() -> list:
    """Parse 'nft list ruleset' text output as fallback."""
    rules = []
    try:
        out = _run(["nft", "list", "ruleset"])
        for line in out.splitlines():
            m = re.search(
                r"(tcp|udp)?\s*dport\s+(\d+)\s+dnat\s+to\s+(\d+\.\d+\.\d+\.\d+):(\d+)",
                line, re.IGNORECASE
            )
            if m:
                rules.append({
                    "protocol": (m.group(1) or "tcp").lower(),
                    "external_port": int(m.group(2)),
                    "internal_ip": m.group(3),
                    "internal_port": int(m.group(4)),
                    "table": "nat",
                    "chain": "prerouting",
                })
    except Exception:
        pass
    return rules


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
    health = get_health_checks()
    gateway_timeout = gateway_timeout_tracker.update(health.get("gateway", {}))

    wan_iface = CONF["WAN_IFACE"]
    lan_iface = CONF["LAN_IFACE"]

    # Update bandwidth history for trends
    bandwidth_history.update(speeds.get(wan_iface, {}), speeds.get(lan_iface, {}))

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
        "health": health,
        "gateway_timeout": gateway_timeout,
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


@app.route("/api/trends")
def api_trends():
    """Return bandwidth trend history (rolling 1-hour)."""
    history = bandwidth_history.get_history()
    return jsonify({
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "trends": history,
    })


@app.route("/api/health")
def api_health():
    """Return gateway and DNS health checks."""
    return jsonify(get_health_checks())


@app.route("/api/port-forwards", methods=["GET"])
def api_port_forwards_get():
    entries = _load_port_forwards()
    nft_rules = get_nftables_forwards()
    nft_ports = {(r["protocol"], r["external_port"]) for r in nft_rules}

    for entry in entries:
        proto = entry.get("protocol", "tcp").lower()
        ext = int(entry.get("external_port", 0))
        if proto == "tcp/udp":
            entry["system_detected"] = ("tcp", ext) in nft_ports or ("udp", ext) in nft_ports
        else:
            entry["system_detected"] = (proto, ext) in nft_ports

    return jsonify({"entries": entries, "nft_rules": nft_rules})


@app.route("/api/port-forwards", methods=["POST"])
def api_port_forwards_create():
    data = request.get_json(force=True) or {}
    try:
        ext_port = int(data.get("external_port", 0))
        int_port = int(data.get("internal_port", 0))
        if not (1 <= ext_port <= 65535) or not (1 <= int_port <= 65535):
            return jsonify({"error": "Port must be 1–65535"}), 400
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid port number"}), 400

    internal_ip = str(data.get("internal_ip", "")).strip()
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", internal_ip):
        return jsonify({"error": "Invalid internal IP address"}), 400

    protocol = str(data.get("protocol", "tcp")).strip().lower()
    if protocol not in ("tcp", "udp", "tcp/udp"):
        return jsonify({"error": "Protocol must be tcp, udp, or tcp/udp"}), 400

    entry = {
        "id": uuid.uuid4().hex[:8],
        "protocol": protocol,
        "external_port": ext_port,
        "internal_ip": internal_ip,
        "internal_port": int_port,
        "description": str(data.get("description", "")).strip()[:200],
        "active": bool(data.get("active", True)),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    entries = _load_port_forwards()
    entries.append(entry)
    _save_port_forwards(entries)
    return jsonify(entry), 201


@app.route("/api/port-forwards/<entry_id>", methods=["PUT"])
def api_port_forwards_update(entry_id: str):
    data = request.get_json(force=True) or {}
    entries = _load_port_forwards()
    for i, e in enumerate(entries):
        if e.get("id") != entry_id:
            continue
        if "description" in data:
            entries[i]["description"] = str(data["description"]).strip()[:200]
        if "active" in data:
            entries[i]["active"] = bool(data["active"])
        if "protocol" in data:
            proto = str(data["protocol"]).strip().lower()
            if proto in ("tcp", "udp", "tcp/udp"):
                entries[i]["protocol"] = proto
        if "external_port" in data:
            try:
                p = int(data["external_port"])
                if 1 <= p <= 65535:
                    entries[i]["external_port"] = p
            except (ValueError, TypeError):
                pass
        if "internal_port" in data:
            try:
                p = int(data["internal_port"])
                if 1 <= p <= 65535:
                    entries[i]["internal_port"] = p
            except (ValueError, TypeError):
                pass
        if "internal_ip" in data:
            ip = str(data["internal_ip"]).strip()
            if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
                entries[i]["internal_ip"] = ip
        _save_port_forwards(entries)
        return jsonify(entries[i])
    return jsonify({"error": "Not found"}), 404


@app.route("/api/port-forwards/<entry_id>", methods=["DELETE"])
def api_port_forwards_delete(entry_id: str):
    entries = _load_port_forwards()
    new_entries = [e for e in entries if e.get("id") != entry_id]
    if len(new_entries) == len(entries):
        return jsonify({"error": "Not found"}), 404
    _save_port_forwards(new_entries)
    return jsonify({"ok": True})


if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.environ.get("DASHBOARD_PORT", 5000))
    log.info("Vivo-Router Dashboard starting on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False, threaded=True)
