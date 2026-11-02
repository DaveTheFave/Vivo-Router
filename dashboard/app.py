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


_pihole_ptr_cache: dict = {"data": {}, "expires": 0.0}
PIHOLE_PTR_CACHE_TTL = 30  # seconds — avoid repeated PTR lookups


def _pihole_dns_server() -> str:
    """Return the DNS server address used for PTR lookups."""
    explicit = (CONF.get("PIHOLE_DNS_SERVER") or "").strip()
    if explicit:
        return explicit

    api_url = (CONF.get("PIHOLE_API") or "").strip()
    if api_url:
        hostname = urllib.parse.urlparse(api_url).hostname
        if hostname:
            return hostname

    return "127.0.0.1"


def _resolve_pihole_ptr(ip: str) -> str | None:
    """Resolve a client name by querying Pi-hole's DNS server directly."""
    if not ip:
        return None

    now = time.time()
    cached = _pihole_ptr_cache["data"].get(ip)
    if cached and now < cached[1]:
        return cached[0]

    server = _pihole_dns_server()
    candidates = [
        ["dig", "+time=2", "+tries=1", "+short", "-x", ip, f"@{server}"],
        ["getent", "hosts", ip],
    ]

    hostname = None
    for cmd in candidates:
        out = _run(cmd).strip()
        if not out:
            continue
        line = out.splitlines()[0].strip()
        if not line:
            continue
        if cmd[0] == "dig":
            hostname = line.rstrip(".")
            break
        parts = line.split()
        if len(parts) >= 2 and parts[1].endswith("."):
            hostname = parts[1].rstrip(".")
            break

    if hostname and hostname not in {"*", "(unknown)"}:
        _pihole_ptr_cache["data"][ip] = (hostname, now + PIHOLE_PTR_CACHE_TTL)
        return hostname

    return None


def resolve_hostname_pihole(ip: str) -> str | None:
    """Resolve a client hostname from Pi-hole's DNS resolver."""
    return _resolve_pihole_ptr(ip)


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

    1. Try Pi-hole DNS PTR lookups (conditional forwarding)
    2. Try avahi-resolve (mDNS) — primary for local network
    3. Try reverse DNS — fallback
    4. Return None if all fail
    """
    hostname = resolve_hostname_pihole(ip)
    if hostname:
        return hostname

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


class HostnameRefresher:
    """Periodically re-resolve hostnames for currently-seen clients in the
    background, independent of any HTTP request.

    The dashboard frontend only asks for a synchronous resolve on first page
    load; a kiosk display that never reloads would otherwise never refresh
    names again, leaving new/renamed clients stuck showing their bare IP.
    This background loop fixes that without changing the request-time
    behavior or the per-IP resolution rate: it defers to the existing
    HOSTNAME_CACHE_TTL, so each client's hostname is still looked up (mDNS /
    Pi-hole PTR / reverse DNS) at most once per TTL window — the same cadence
    already relied on to keep PTR query volume low after we previously
    overwhelmed the DNS server with lookups.
    """

    def __init__(self, interval: float = 60.0):
        self._interval = interval
        self._thread = threading.Thread(target=self._run, name="hostname-refresher", daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        while True:
            try:
                arp_clients = get_arp_clients()
                ips = [c["ip"] for c in arp_clients.values()]
                now = time.time()
                stale = [ip for ip in ips
                         if ip not in _hostname_cache
                         or now - _hostname_cache[ip][1] >= HOSTNAME_CACHE_TTL]
                if stale:
                    fresh = get_hostnames_batch(stale, timeout=1.5, max_workers=5)
                    for ip, hostname in fresh.items():
                        _hostname_cache[ip] = (hostname, now)
            except Exception as exc:
                log.warning("HostnameRefresher error: %s", exc)
            time.sleep(self._interval)


_hostname_refresher = HostnameRefresher(interval=60.0)
_hostname_refresher.start()


# ── per-client bandwidth tracking via nftables counters ─────────────────────

def _extract_nft_counter_bytes(expr: dict) -> int | None:
    """Return the byte count from an nftables JSON 'counter' expression, if present."""
    counter = expr.get("counter")
    if not isinstance(counter, dict):
        return None
    return counter.get("bytes", 0)


def _extract_nft_comment(expr: dict) -> str | None:
    """Return the comment string attached to an nftables JSON rule expression, if present."""
    comment = expr.get("comment")
    if isinstance(comment, str):
        return comment
    return None


class ClientBandwidthTracker:
    """Track per-client bandwidth using nftables counters.

    Existence of counter rules is checked against the *live* nftables
    ruleset on every poll, not against in-process memory. That makes this
    self-healing after anything external flushes or reloads nftables (a
    `systemctl restart nftables`, a boot, a manual `nft -f` load) instead of
    permanently believing rules exist that are actually gone. It also fixes
    the previous behavior of re-adding a full duplicate set of counter rules
    on every dashboard process restart, since the live ruleset — not a
    dict that resets to empty on restart — is now the source of truth for
    what already exists.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counters_cache: dict = {}  # {ip: {tx_bytes, rx_bytes, ts}}
        self._last_seen: dict = {}  # {ip: last_seen_ts}

    def _list_ruleset(self) -> dict | None:
        try:
            result = subprocess.run(
                ["nft", "-j", "-a", "list", "ruleset"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0 or not result.stdout.strip():
                log.debug("nftables query returned no data")
                return None
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            log.warning(f"Failed to parse nftables JSON: {exc}")
            return None
        except Exception as exc:
            log.warning(f"Failed to query nftables: {exc}")
            return None

    def _parse_counters(self, data: dict) -> tuple[dict, dict]:
        """Extract per-IP counter byte totals and rule handles from `nft -j` output.

        Returns (ip_counters, ip_handles):
          ip_counters: {ip: {"tx_bytes": n, "rx_bytes": n}}
          ip_handles:  {ip: {"tx": handle, "rx": handle}}
        """
        ip_counters: dict = {}
        ip_handles: dict = {}

        try:
            for item in data.get("nftables", []):
                rule = item.get("rule")
                if not rule:
                    continue

                # Rule "table" is the table *name* (e.g. "filter"); the
                # address family (e.g. "inet") is a separate "family" key.
                if rule.get("family") != "inet" or rule.get("table") != "filter":
                    continue
                if rule.get("chain") != "forward":
                    continue

                # A rule's comment (set via `... comment "..."`) is a
                # top-level key on the rule object itself, not an entry
                # inside its "expr" list.
                comment = _extract_nft_comment(rule)
                if not (comment and comment.startswith("vivo-router:")):
                    continue

                exprs = rule.get("expr", [])
                if not isinstance(exprs, list):
                    continue

                counter_bytes = None
                for expr in exprs:
                    if not isinstance(expr, dict):
                        continue
                    if counter_bytes is None:
                        counter_bytes = _extract_nft_counter_bytes(expr)

                if counter_bytes is None:
                    continue

                _, direction, ip = comment.split(":", 2)
                if direction not in ("tx", "rx"):
                    continue

                ip_counters.setdefault(ip, {})[f"{direction}_bytes"] = counter_bytes
                ip_handles.setdefault(ip, {})[direction] = rule.get("handle")
        except Exception as exc:
            log.warning(f"Failed to parse nftables output: {exc}")

        return ip_counters, ip_handles

    def _ensure_rules(self, missing_ips: set):
        """Add tx/rx counter rules for client IPs that don't already have them.

        Uses `nft insert` (prepend to chain start), not `nft add` (append to
        chain end): real traffic already hits a terminating `accept` verdict
        earlier in the forward chain, so a counter-only rule appended after
        that point would never actually see a packet. A bare `counter` rule
        has no verdict of its own, so inserting it ahead of everything else
        is safe — it just tallies bytes and falls through unchanged.
        """
        for ip in missing_ips:
            if not re.match(r"\d{1,3}(\.\d{1,3}){3}", ip):
                log.warning(f"Invalid IP format: {ip}")
                continue
            try:
                for direction, field in (("tx", "saddr"), ("rx", "daddr")):
                    result = subprocess.run(
                        ["nft", "insert", "rule", "inet", "filter", "forward",
                         f"ip {field} {ip} counter comment \"vivo-router:{direction}:{ip}\""],
                        capture_output=True, text=True, timeout=3
                    )
                    if result.returncode != 0:
                        log.warning(f"nft {field} rule failed for {ip}: {result.stderr.strip()}")
                log.debug(f"Added nftables counters for client {ip}")
            except subprocess.TimeoutExpired:
                log.warning(f"nft command timed out for {ip}")
            except Exception as exc:
                log.warning(f"Failed to add nftables rule for {ip}: {exc}")

    def _cleanup_stale_rules(self, stale_ips: set, ip_handles: dict):
        """Actually delete counter rules (by handle) for IPs no longer active."""
        for ip in stale_ips:
            for direction, handle in ip_handles.get(ip, {}).items():
                if handle is None:
                    continue
                try:
                    result = subprocess.run(
                        ["nft", "delete", "rule", "inet", "filter", "forward", "handle", str(handle)],
                        capture_output=True, text=True, timeout=3
                    )
                    if result.returncode != 0:
                        log.warning(f"nft delete rule failed for {ip} ({direction}): {result.stderr.strip()}")
                    else:
                        log.debug(f"Removed stale counter rule for {ip} ({direction})")
                except subprocess.TimeoutExpired:
                    log.warning(f"nft delete timed out for {ip} ({direction})")
                except Exception as exc:
                    log.warning(f"Failed to delete stale rule for {ip} ({direction}): {exc}")
            self._last_seen.pop(ip, None)
            self._counters_cache.pop(ip, None)

    def poll(self, client_ips: list, stale_timeout: int = 600) -> dict:
        """Sync counter rules with the currently active client list and
        return per-IP bandwidth data: {ip: {rx_bytes, tx_bytes, rx_rate, tx_rate}}.
        """
        results: dict = {}
        now = time.time()

        with self._lock:
            for ip in client_ips:
                self._last_seen[ip] = now

            data = self._list_ruleset()
            if data is None:
                return results

            ip_counters, ip_handles = self._parse_counters(data)

            # Rules seen in the ruleset that this process has no memory of
            # (e.g. right after a restart) get a fresh grace period instead
            # of being deleted outright.
            for ip in ip_handles:
                self._last_seen.setdefault(ip, now)

            missing = set(client_ips) - set(ip_handles.keys())
            if missing:
                self._ensure_rules(missing)

            stale_ips = {
                ip for ip in ip_handles
                if ip not in client_ips and (now - self._last_seen.get(ip, now)) > stale_timeout
            }
            if stale_ips:
                self._cleanup_stale_rules(stale_ips, ip_handles)

            for ip in client_ips:
                tx_bytes = ip_counters.get(ip, {}).get("tx_bytes", 0)
                rx_bytes = ip_counters.get(ip, {}).get("rx_bytes", 0)

                prev = self._counters_cache.get(ip, {})
                prev_tx = prev.get("tx_bytes", 0)
                prev_rx = prev.get("rx_bytes", 0)
                prev_ts = prev.get("ts", now)

                dt = max(now - prev_ts, 1)  # Avoid division by zero
                tx_rate = max(0, (tx_bytes - prev_tx) / dt) if prev else 0
                rx_rate = max(0, (rx_bytes - prev_rx) / dt) if prev else 0

                self._counters_cache[ip] = {"tx_bytes": tx_bytes, "rx_bytes": rx_bytes, "ts": now}

                results[ip] = {
                    "rx_bytes": rx_bytes,
                    "tx_bytes": tx_bytes,
                    "rx_rate": round(rx_rate),
                    "tx_rate": round(tx_rate),
                }

        return results


_client_bandwidth_tracker = ClientBandwidthTracker()


def get_active_clients(resolve_names: bool = True) -> list:
    """Discover active clients using ARP + nftables bandwidth tracking."""
    # Get clients from ARP table
    arp_clients = get_arp_clients()
    if not arp_clients:
        return []

    ips = [client["ip"] for client in arp_clients.values()]
    if resolve_names:
        # Resolve hostnames only when explicitly requested; otherwise reuse cache.
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

    # Sync nftables counter rules with the active client list and read
    # current bandwidth data (best effort).
    try:
        bandwidth_data = _client_bandwidth_tracker.poll(ips, stale_timeout=600)
    except Exception as exc:
        log.error(f"Bandwidth tracking failed: {exc}")
        bandwidth_data = {}

    # Build final client list with bandwidth data from nftables
    clients = []
    for mac, arp_data in arp_clients.items():
        ip = arp_data["ip"]
        bw = bandwidth_data.get(ip, {})
        rx_bytes = bw.get("rx_bytes", 0)
        tx_bytes = bw.get("tx_bytes", 0)
        rx_rate = bw.get("rx_rate", 0)
        tx_rate = bw.get("tx_rate", 0)

        clients.append({
            "mac": mac,
            "ip": ip,
            "hostname": arp_data["hostname"],
            "lease_remaining": -1,
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
            "rx_human": _human_bytes(rx_rate),
            "tx_human": _human_bytes(tx_rate),
            "total_human": _human_bytes(rx_rate + tx_rate),
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


_PSAD_BLOCK_CHAINS = ("PSAD_BLOCK_INPUT", "PSAD_BLOCK_OUTPUT", "PSAD_BLOCK_FORWARD")


def get_psad_auto_blocked_ips() -> dict:
    """Query nftables for IPs currently auto-blocked by psad.

    psad's iptables-nft compat layer manages a separate 'ip filter' table
    (distinct from our own 'inet filter' table) — reading that directly
    reflects live block state, rather than parsing psad's own bookkeeping
    files which may lag or include already-expired entries.

    psad spreads a single block across up to three chains (INPUT matches
    saddr, OUTPUT matches daddr, FORWARD can carry both a saddr and a daddr
    rule for the same IP) — an earlier version of this only read
    PSAD_BLOCK_INPUT, so "unblocking" an IP silently left it still dropped
    by the other two. Returns {ip: {"packets", "bytes", "rules": [(chain,
    handle), ...]}} so callers can remove every rule for an IP, not just one.
    """
    blocked: dict = {}
    try:
        result = subprocess.run(
            ["nft", "-j", "-a", "list", "table", "ip", "filter"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        data = json.loads(result.stdout)
    except Exception as exc:
        log.debug(f"get_psad_auto_blocked_ips: nft query failed: {exc}")
        return {}

    try:
        for item in data.get("nftables", []):
            rule = item.get("rule")
            if not rule or rule.get("chain") not in _PSAD_BLOCK_CHAINS:
                continue
            ip = None
            counter = None
            for expr in rule.get("expr", []):
                if not isinstance(expr, dict):
                    continue
                if "match" in expr:
                    left = expr["match"].get("left", {})
                    right = expr["match"].get("right")
                    if isinstance(left, dict):
                        field = left.get("payload", {}).get("field")
                        if field in ("saddr", "daddr") and isinstance(right, str):
                            ip = right
                if "counter" in expr:
                    counter = expr["counter"]
            if not ip:
                continue
            entry = blocked.setdefault(ip, {"packets": 0, "bytes": 0, "rules": []})
            entry["packets"] += (counter or {}).get("packets", 0)
            entry["bytes"] += (counter or {}).get("bytes", 0)
            entry["rules"].append((rule.get("chain"), rule.get("handle")))
    except Exception as exc:
        log.debug(f"get_psad_auto_blocked_ips: parse failed: {exc}")

    return blocked


# ── dashboard-managed manual blocks ──────────────────────────────────────────
#
# Kept in a dedicated nftables table this dashboard fully owns, independent
# of psad's own PSAD_BLOCK_* chains (which psad creates lazily on its first
# auto-block and manages the lifecycle of — inserting into them ourselves
# would risk racing psad's own bootstrap). Runs at a lower priority than
# both psad's table and our hand-tuned firewall, so a manual block takes
# effect immediately regardless of what either of those decide.

_DASHBOARD_BLOCKS_TABLE_READY = False
MANUAL_BLOCKS_EVENTS_FILE = BASE_DIR / "psad_manual_events.json"
_manual_events_lock = threading.Lock()


def _ensure_dashboard_blocks_table() -> None:
    """Idempotently create the dashboard's own manual-block table/chains."""
    global _DASHBOARD_BLOCKS_TABLE_READY
    if _DASHBOARD_BLOCKS_TABLE_READY:
        return
    cmds = [
        ["nft", "add", "table", "inet", "dashboard_blocks"],
        ["nft", "add", "set", "inet", "dashboard_blocks", "manual_blocked",
         "{ type ipv4_addr; }"],
        ["nft", "add", "chain", "inet", "dashboard_blocks", "input",
         "{ type filter hook input priority -10; policy accept; }"],
        ["nft", "add", "chain", "inet", "dashboard_blocks", "forward",
         "{ type filter hook forward priority -10; policy accept; }"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            log.warning(f"_ensure_dashboard_blocks_table: {' '.join(cmd)} -> {result.stderr.strip()}")
    # Rules referencing the set are only added once the set exists; adding
    # a duplicate rule is harmless (nft dedupes identical rule bodies here
    # only if we check first, so guard with a chain listing).
    check = subprocess.run(
        ["nft", "-j", "list", "chain", "inet", "dashboard_blocks", "input"],
        capture_output=True, text=True, timeout=5
    )
    has_drop_rule = False
    if check.returncode == 0 and check.stdout.strip():
        try:
            data = json.loads(check.stdout)
            for item in data.get("nftables", []):
                rule = item.get("rule")
                if rule and "drop" in json.dumps(rule.get("expr", [])):
                    has_drop_rule = True
        except Exception:
            pass
    if not has_drop_rule:
        subprocess.run(
            ["nft", "add", "rule", "inet", "dashboard_blocks", "input",
             "ip saddr @manual_blocked drop"],
            capture_output=True, text=True, timeout=5
        )
        subprocess.run(
            ["nft", "add", "rule", "inet", "dashboard_blocks", "forward",
             "ip saddr @manual_blocked drop"],
            capture_output=True, text=True, timeout=5
        )
    _DASHBOARD_BLOCKS_TABLE_READY = True


def get_manual_blocked_ips() -> list:
    """Return IPs currently blocked via the dashboard's manual-block set."""
    _ensure_dashboard_blocks_table()
    try:
        result = subprocess.run(
            ["nft", "-j", "list", "set", "inet", "dashboard_blocks", "manual_blocked"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        data = json.loads(result.stdout)
    except Exception as exc:
        log.debug(f"get_manual_blocked_ips: nft query failed: {exc}")
        return []

    ips = []
    try:
        for item in data.get("nftables", []):
            s = item.get("set")
            if not s or s.get("name") != "manual_blocked":
                continue
            for elem in s.get("elem", []):
                if isinstance(elem, str):
                    ips.append(elem)
                elif isinstance(elem, dict) and "elem" in elem:
                    val = elem["elem"].get("val") if isinstance(elem["elem"], dict) else elem["elem"]
                    if isinstance(val, str):
                        ips.append(val)
    except Exception as exc:
        log.debug(f"get_manual_blocked_ips: parse failed: {exc}")

    return ips


def _load_manual_events() -> list:
    with _manual_events_lock:
        if not MANUAL_BLOCKS_EVENTS_FILE.exists():
            return []
        try:
            return json.loads(MANUAL_BLOCKS_EVENTS_FILE.read_text())
        except Exception:
            return []


def _append_manual_event(event: dict) -> None:
    with _manual_events_lock:
        events = []
        if MANUAL_BLOCKS_EVENTS_FILE.exists():
            try:
                events = json.loads(MANUAL_BLOCKS_EVENTS_FILE.read_text())
            except Exception:
                events = []
        events.append(event)
        events = events[-200:]  # keep the file bounded
        MANUAL_BLOCKS_EVENTS_FILE.write_text(json.dumps(events, indent=2))


def psad_manual_block(ip: str) -> tuple[bool, str]:
    """Manually block an IP via the dashboard's own nftables set."""
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
        return False, "Invalid IP address"

    _ensure_dashboard_blocks_table()
    result = subprocess.run(
        ["nft", "add", "element", "inet", "dashboard_blocks", "manual_blocked",
         "{ " + ip + " }"],
        capture_output=True, text=True, timeout=5
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or "nft command failed"

    _append_manual_event({
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "type": "manual_block",
        "ip": ip,
        "detail": "Manually blocked from dashboard",
    })
    return True, "Blocked"


def psad_manual_unblock(ip: str) -> tuple[bool, str]:
    """Remove every block on an IP — the dashboard's own manual-block set
    entry (if any) AND every psad auto-block rule for it across all three
    chains psad manages (if any). A single "Unblock" action has to clear
    all of these: psad spreads one block across PSAD_BLOCK_INPUT/OUTPUT/
    FORWARD, and clearing only one (the original bug here) leaves the IP
    silently still dropped by the others.
    """
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
        return False, "Invalid IP address"

    removed_anything = False
    errors = []

    _ensure_dashboard_blocks_table()
    result = subprocess.run(
        ["nft", "delete", "element", "inet", "dashboard_blocks", "manual_blocked",
         "{ " + ip + " }"],
        capture_output=True, text=True, timeout=5
    )
    stderr_lower = (result.stderr or "").lower()
    if result.returncode == 0:
        removed_anything = True
    elif "does not exist" not in stderr_lower and "no such file or directory" not in stderr_lower:
        errors.append(result.stderr.strip())

    auto_blocked = get_psad_auto_blocked_ips()
    entry = auto_blocked.get(ip)
    if entry:
        for chain, handle in entry["rules"]:
            if handle is None:
                continue
            result = subprocess.run(
                ["nft", "delete", "rule", "ip", "filter", chain, "handle", str(handle)],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                removed_anything = True
            else:
                errors.append(result.stderr.strip())

    if not removed_anything:
        return False, "IP was not found in any block list"

    _append_manual_event({
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "type": "manual_unblock",
        "ip": ip,
        "detail": "Manually unblocked from dashboard",
    })
    if errors:
        return True, "Unblocked, with some errors: " + "; ".join(errors)
    return True, "Unblocked"


def get_psad_blocked_ips() -> list:
    """Return all currently-blocked IPs, from both psad's own auto-blocks
    and the dashboard's manual-block set, tagged with their source."""
    blocked = {}
    for ip, entry in get_psad_auto_blocked_ips().items():
        blocked[ip] = {
            "ip": ip,
            "packets": entry["packets"],
            "bytes": entry["bytes"],
            "source": "psad",
        }
    for ip in get_manual_blocked_ips():
        if ip in blocked:
            blocked[ip]["source"] = "psad+manual"
        else:
            blocked[ip] = {"ip": ip, "packets": 0, "bytes": 0, "source": "manual"}
    return list(blocked.values())


_IP_ADDR_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _summarize_whois(raw: str) -> str | None:
    """Boil a full ARIN/RIPE-style whois dump down to one line.

    Whois output cascades from the broad allocation down through each
    reassignment to the actual registrant, as separate '# start'/'# end'
    blocks — the last block is the most specific and most useful one.
    """
    blocks = re.split(r"#\s*start\s*\n", raw)
    if not blocks:
        return None
    last = blocks[-1].split("# end")[0]

    org = None
    for key in ("CustName", "OrgName"):
        m = re.search(rf"^{key}:\s*(.+)$", last, re.MULTILINE)
        if m:
            org = m.group(1).strip()
            break

    country_m = re.search(r"^Country:\s*(.+)$", last, re.MULTILINE)
    city_m = re.search(r"^City:\s*(.+)$", last, re.MULTILINE)
    country = country_m.group(1).strip() if country_m else None
    city = city_m.group(1).strip() if city_m else None

    parts = [p for p in (org, city, country) if p]
    return " · ".join(parts) if parts else None


def get_psad_ip_detail(ip: str) -> dict:
    """Return psad's own detailed tracking data for one attacker IP.

    Reads /var/log/psad/<ip>/ directly — the same per-attacker bookkeeping
    'psad -S' summarizes into its "Top attackers" table (danger level,
    packet counts per destination, matched signatures, scan start time,
    whois). This is the authoritative "why was this IP flagged" answer,
    richer than what fits in the journal-derived activity feed. Returns
    tracked: false for IPs psad never scored (e.g. a manual block against
    an address that was never actually seen scanning).
    """
    if not _IP_ADDR_RE.match(ip):
        return {"ip": ip, "tracked": False}

    ip_dir = Path("/var/log/psad") / ip
    if not ip_dir.is_dir():
        return {"ip": ip, "tracked": False}

    detail: dict = {"ip": ip, "tracked": True, "destinations": []}

    dl_file = ip_dir / "danger_level"
    if dl_file.exists():
        try:
            detail["danger_level"] = int(dl_file.read_text(errors="replace").strip())
        except Exception:
            pass

    whois_file = ip_dir / f"{ip}_whois"
    if whois_file.exists():
        try:
            raw = whois_file.read_text(errors="replace").strip()
            detail["whois"] = raw
            detail["whois_summary"] = _summarize_whois(raw)
        except Exception:
            pass

    dest_ips = set()
    try:
        for f in ip_dir.glob("*_packet_ctr"):
            dest_ips.add(f.name[: -len("_packet_ctr")])
    except Exception as exc:
        log.debug(f"get_psad_ip_detail: glob failed for {ip}: {exc}")

    for dest in sorted(dest_ips):
        dest_info: dict = {"dest": dest}

        pc = ip_dir / f"{dest}_packet_ctr"
        if pc.exists():
            try:
                dest_info["packets"] = int(pc.read_text(errors="replace").strip())
            except Exception:
                pass

        st = ip_dir / f"{dest}_start_time"
        if st.exists():
            try:
                epoch = int(st.read_text(errors="replace").strip())
                dest_info["start_time"] = datetime.fromtimestamp(
                    epoch, tz=timezone.utc
                ).replace(microsecond=0).isoformat()
            except Exception:
                pass

        sig = ip_dir / f"{dest}_signatures"
        if sig.exists():
            try:
                lines = [l.strip() for l in sig.read_text(errors="replace").splitlines() if l.strip()]
                dest_info["signatures"] = lines
            except Exception:
                dest_info["signatures"] = []

        detail["destinations"].append(dest_info)

    return detail


_PSAD_LOG_LINE_RE = re.compile(r'^(\S+)\s+\S+\s+psad\[\d+\]:\s*(.*)$')
_PSAD_RE_BLOCK = re.compile(r'added iptables auto-block against (?P<ip>\S+) for (?P<seconds>\d+) seconds')
_PSAD_RE_SCAN = re.compile(
    r'scan detected \((?P<scan_type>[^)]+)\): (?P<src>\S+) -> (?P<dst>\S+) '
    r'(?P<proto>\w+): \[(?P<ports>[\d,]+)\].*?DL: (?P<dl>\d+)'
)
_PSAD_RE_SIG = re.compile(
    r'src: (?P<ip>\S+) signature match: "(?P<sig>[^"]+)" \(sid: (?P<sid>\d+)\) '
    r'(?P<proto>\w+) port: (?P<port>\d+)'
)


def get_psad_activity(n: int = 25) -> dict:
    """Return currently-blocked IPs and a merged recent-activity feed.

    Parses recent 'psad' journal entries directly rather than psad's own
    per-attacker files under /var/log/psad/ — the three log line formats
    psad emits (signature match, scan detected, auto-block) are stable and
    simple to match, and this avoids depending on root-owned per-IP
    tracking file layouts that can change between psad versions. Merges in
    dashboard-initiated manual block/unblock events from their own local
    log, and flags every event whose IP has been blocked (by psad or
    manually) anywhere in this window as "resolved" — even if that block
    has since expired — so past scan/signature entries for a handled
    attacker read as taken care of, not just its live block state.
    """
    psad_active = _run(["systemctl", "is-active", "psad"]) == "active"

    events = []
    if psad_active:
        try:
            result = subprocess.run(
                ["journalctl", "-u", "psad", "-n", "200", "--no-pager", "-o", "short-iso"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.splitlines() if result.returncode == 0 else []
        except Exception as exc:
            log.debug(f"get_psad_activity: journalctl failed: {exc}")
            lines = []

        for line in lines:
            m = _PSAD_LOG_LINE_RE.match(line)
            if not m:
                continue
            ts, msg = m.group(1), m.group(2)

            bm = _PSAD_RE_BLOCK.search(msg)
            if bm:
                mins = int(bm.group("seconds")) // 60
                events.append({
                    "timestamp": ts, "type": "auto_block", "ip": bm.group("ip"),
                    "detail": f"Blocked for {mins} min" if mins else "Blocked",
                })
                continue

            sm = _PSAD_RE_SCAN.search(msg)
            if sm:
                events.append({
                    "timestamp": ts, "type": "scan_detected", "ip": sm.group("src"),
                    "detail": f"{sm.group('scan_type')} → {sm.group('proto')}/{sm.group('ports')} (DL{sm.group('dl')})",
                })
                continue

            gm = _PSAD_RE_SIG.search(msg)
            if gm:
                events.append({
                    "timestamp": ts, "type": "signature_match", "ip": gm.group("ip"),
                    "detail": f"{gm.group('sig')} ({gm.group('proto')}/{gm.group('port')})",
                })
                continue

    events.extend(_load_manual_events())
    events.sort(key=lambda ev: ev.get("timestamp", ""))

    resolved_ips = {ev["ip"] for ev in events if ev["type"] in ("auto_block", "manual_block")}
    for ev in events:
        ev["resolved"] = ev["ip"] in resolved_ips

    events = events[-n:]
    events.reverse()

    return {
        "enabled": psad_active,
        "events": events,
        "currently_blocked": get_psad_blocked_ips(),
    }


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


AUTOBLOCK_LOG_FILE = Path("/var/log/suricata-autoblock.jsonl")


def _load_autoblock_events() -> list:
    if not AUTOBLOCK_LOG_FILE.exists():
        return []
    events = []
    try:
        for line in AUTOBLOCK_LOG_FILE.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    except Exception as exc:
        log.debug(f"_load_autoblock_events: read failed: {exc}")
    return events


def get_blocklist_activity() -> dict:
    """Return every IP currently in the dashboard's block set, each with
    the most recent reason we have on record for it.

    Reasons come from two places: the suricata-autoblock audit log (for
    IPs the autoblock timer added, with the triggering signature and
    target service) and the dashboard's own manual block/unblock event
    log (for IPs blocked by hand via the PSAD panel's Block button).
    An IP can be in neither — e.g. one added directly via nft before
    either of those existed — and is still shown, flagged as such.
    """
    current_ips = set(get_manual_blocked_ips())

    # Latest known event per IP, from whichever source is more recent.
    latest: dict = {}

    for ev in _load_autoblock_events():
        ip = ev.get("ip")
        if ip not in current_ips:
            continue
        ts = ev.get("timestamp", "")
        if ip not in latest or ts > latest[ip]["timestamp"]:
            dest = ev.get("dest_ip") or ""
            if dest and ev.get("dest_port"):
                dest = f"{dest}:{ev['dest_port']}"
            reason = ev.get("signature") or ev.get("category") or "Reputation-flagged IP"
            if dest:
                reason = f"{reason} → {dest}"
            latest[ip] = {"timestamp": ts, "reason": reason, "source": "auto"}

    for ev in _load_manual_events():
        if ev.get("type") != "manual_block":
            continue
        ip = ev.get("ip")
        if ip not in current_ips:
            continue
        ts = ev.get("timestamp", "")
        if ip not in latest or ts > latest[ip]["timestamp"]:
            latest[ip] = {
                "timestamp": ts,
                "reason": ev.get("detail") or "Manually blocked from dashboard",
                "source": "manual",
            }

    entries = []
    for ip in current_ips:
        info = latest.get(ip)
        if info:
            entries.append({"ip": ip, **info})
        else:
            entries.append({
                "ip": ip,
                "timestamp": "",
                "reason": "No record of why — added before activity logging existed",
                "source": "unknown",
            })

    entries.sort(key=lambda e: e["timestamp"], reverse=True)

    return {"total": len(entries), "entries": entries}


UPNP_LEASE_FILE = Path("/var/log/miniupnpd.leases")

# Well-known ports/ranges for guessing why a UPnP mapping was opened.
# Checked only after the client's own self-reported description and its
# DHCP/mDNS hostname — those are more reliable when present.
_UPNP_PORT_HINTS = [
    ({25565}, "Minecraft (Java)"),
    ({19132, 19133}, "Minecraft (Bedrock)"),
    ({3074}, "Xbox Live multiplayer"),
    ({3544}, "Xbox Live (Teredo/NAT traversal)"),
    ({3478, 3479, 3480}, "PlayStation Network (voice/matchmaking)"),
    (range(9293, 9296), "Nintendo Switch online play"),
    (range(27015, 27031), "Steam / Source engine game server"),
    (range(6881, 6890), "BitTorrent"),
    ({51413}, "Transmission (BitTorrent)"),
    ({4662, 4672}, "eMule"),
    ({1935}, "RTMP streaming"),
    ({554}, "RTSP (camera/streaming)"),
    ({32400}, "Plex Media Server"),
    ({8096, 8920}, "Jellyfin/Emby media server"),
    ({2869, 5357}, "Windows device discovery (WSD/UPnP host)"),
    ({500, 4500}, "IPSec VPN"),
    ({1194}, "OpenVPN"),
    ({51820}, "WireGuard VPN"),
    ({1723}, "PPTP VPN"),
    ({5060, 5061}, "SIP/VoIP"),
    ({80, 443, 8080, 8443}, "Web server"),
    ({21, 990}, "FTP/FTPS server"),
]

# Substrings matched against "<description> <hostname>" (lowercased).
_UPNP_TEXT_HINTS = [
    ("xbox", "Xbox"),
    ("playstation", "PlayStation"),
    ("ps5", "PlayStation 5"),
    ("ps4", "PlayStation 4"),
    ("nintendo", "Nintendo Switch"),
    ("switch", "Nintendo Switch"),
    ("steam", "Steam"),
    ("utorrent", "uTorrent"),
    ("qbittorrent", "qBittorrent"),
    ("deluge", "Deluge (BitTorrent)"),
    ("transmission", "Transmission (BitTorrent)"),
    ("skype", "Skype"),
    ("discord", "Discord"),
    ("plex", "Plex"),
    ("wsdapi", "Windows device discovery"),
]


def _classify_upnp_mapping(ext_port, description: str, hostname: str) -> str:
    """Best-effort guess at why a UPnP mapping was likely requested."""
    text = f"{description} {hostname}".lower()
    for needle, label in _UPNP_TEXT_HINTS:
        if needle in text:
            return label

    try:
        port = int(ext_port)
    except (TypeError, ValueError):
        port = None
    if port is not None:
        for ports, label in _UPNP_PORT_HINTS:
            if port in ports:
                return label

    return "Unknown — no recognized app, device, or port pattern"


def get_upnp_activity(clients: list | None = None) -> dict:
    """Return currently active UPnP/NAT-PMP/PCP port mappings.

    Reads miniupnpd's lease file directly — each line mirrors a live
    mapping (rewritten as mappings come and go, not an append-only log)
    — and enriches it with the requesting device's DHCP/mDNS hostname
    and a best-effort guess at why the port was opened.
    """
    active = _run(["systemctl", "is-active", "miniupnpd"]) == "active"

    if clients is None:
        clients = get_active_clients(resolve_names=True)
    hostnames = {c["ip"]: c.get("hostname", "") for c in clients}

    mappings = []
    if UPNP_LEASE_FILE.exists():
        try:
            lines = UPNP_LEASE_FILE.read_text(errors="replace").splitlines()
        except Exception as exc:
            log.debug(f"get_upnp_activity: read failed: {exc}")
            lines = []

        now = time.time()
        for line in lines:
            parts = line.strip().split(":")
            if len(parts) < 5:
                continue
            proto, ext_port, int_ip, int_port, expiry = parts[:5]
            description = ":".join(parts[5:]) if len(parts) > 5 else ""
            try:
                expiry_ts = int(expiry)
            except ValueError:
                continue
            if expiry_ts and expiry_ts < now:
                continue  # stale line miniupnpd hasn't pruned yet

            hostname = hostnames.get(int_ip, "")
            mappings.append({
                "proto": proto,
                "ext_port": ext_port,
                "int_ip": int_ip,
                "int_port": int_port,
                "hostname": hostname or int_ip,
                "description": description,
                "expires": (
                    datetime.fromtimestamp(expiry_ts, tz=timezone.utc).isoformat()
                    if expiry_ts else None
                ),
                "likely_reason": _classify_upnp_mapping(ext_port, description, hostname),
            })

    mappings.sort(key=lambda m: (m["int_ip"], int(m["ext_port"])))

    return {
        "enabled": active,
        "mapping_count": len(mappings),
        "mappings": mappings,
    }


SURICATA_EVE_LOG = Path("/var/log/suricata/eve.json")


def get_suricata_activity(n: int = 25) -> dict:
    """Return Suricata's IDS status and its most recent alert events.

    Suricata logs every event (flow, dns, tls, alert, ...) as one JSON
    object per line to eve.json. Only "alert" events matter here, so we
    read just the tail of the file — cheap even as it grows between
    logrotate rotations, rather than parsing the whole thing.
    """
    active = _run(["systemctl", "is-active", "suricata"]) == "active"

    alerts = []
    if SURICATA_EVE_LOG.exists():
        try:
            with SURICATA_EVE_LOG.open(errors="replace") as f:
                tail = deque(f, maxlen=3000)
        except Exception as exc:
            log.debug(f"get_suricata_activity: read failed: {exc}")
            tail = []

        for line in tail:
            try:
                evt = json.loads(line)
            except Exception:
                continue
            if evt.get("event_type") != "alert":
                continue
            a = evt.get("alert") or {}
            alerts.append({
                "timestamp": evt.get("timestamp", ""),
                "iface": evt.get("in_iface"),
                "src_ip": evt.get("src_ip"),
                "src_port": evt.get("src_port"),
                "dest_ip": evt.get("dest_ip"),
                "dest_port": evt.get("dest_port"),
                "proto": evt.get("proto"),
                "signature": a.get("signature", ""),
                "severity": a.get("severity"),
                "category": a.get("category", ""),
            })

    alerts.sort(key=lambda e: e["timestamp"])
    alerts = alerts[-n:]
    alerts.reverse()

    return {
        "enabled": active,
        "alert_count": len(alerts),
        "alerts": alerts,
    }


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", conf=CONF)


@app.route("/api/stats")
def api_stats():
    """Master endpoint — returns all dashboard data in one call."""
    resolve_names = request.args.get("resolve_names", "0") == "1"
    wan_ip = get_wan_ip()
    speeds = get_link_speeds()
    clients = get_active_clients(resolve_names=resolve_names)
    system = get_system_stats()
    pihole = get_pihole_stats()
    errors = get_recent_errors()
    health = get_health_checks()
    gateway_timeout = gateway_timeout_tracker.update(health.get("gateway", {}))
    psad = get_psad_activity()
    suricata = get_suricata_activity()
    upnp = get_upnp_activity(clients)
    blocklist = get_blocklist_activity()

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
        "psad": psad,
        "suricata": suricata,
        "upnp": upnp,
        "blocklist": blocklist,
    })


@app.route("/api/clients")
def api_clients():
    return jsonify(get_active_clients(resolve_names=True))


@app.route("/api/psad")
def api_psad():
    return jsonify(get_psad_activity())


@app.route("/api/psad/block", methods=["POST"])
def api_psad_block():
    data = request.get_json(force=True) or {}
    ip = str(data.get("ip", "")).strip()
    ok, msg = psad_manual_block(ip)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/psad/unblock", methods=["POST"])
def api_psad_unblock():
    data = request.get_json(force=True) or {}
    ip = str(data.get("ip", "")).strip()
    ok, msg = psad_manual_unblock(ip)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/psad/detail/<ip>")
def api_psad_detail(ip):
    return jsonify(get_psad_ip_detail(ip))


@app.route("/api/suricata")
def api_suricata():
    return jsonify(get_suricata_activity())


@app.route("/api/upnp")
def api_upnp():
    return jsonify(get_upnp_activity())


@app.route("/api/blocklist")
def api_blocklist():
    return jsonify(get_blocklist_activity())


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
