#!/usr/bin/env python3
"""Suricata auto-block for Vivo-Router.

Watches Suricata's eve.json for reputation-list hits (alert category
"Misc Attack" — Spamhaus DROP, CINS poor-reputation, etc.) targeting
one of this router's actually-exposed services, and adds the matching
source IP to the dashboard's existing nftables blocklist
(table inet dashboard_blocks, set manual_blocked) — the same set the
dashboard's manual Block button and this session have been using by
hand.

Scoped deliberately narrow: only the "Misc Attack" category (curated
third-party bad-IP lists, not generic protocol-anomaly noise), and
only when the destination is a LAN host with an actual forwarded port
right now — static (manage-portfwd.sh, table ip nat) or dynamic
(miniupnpd, table inet nat). Traffic to anything else never triggers
an auto-block.

Meant to run every 5 minutes via the suricata-autoblock.timer unit.
Tracks its own read position (inode + byte offset) in STATE_FILE so
each run only processes alerts written since the last run; falls back
to re-reading from the start if the log rotated or shrank.
"""
import json
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

EVE_LOG = Path("/var/log/suricata/eve.json")
STATE_FILE = Path("/var/lib/suricata-autoblock/state.json")
AUTOBLOCK_LOG = Path("/var/log/suricata-autoblock.jsonl")
NFT = "/usr/sbin/nft"

AUTO_BLOCK_CATEGORIES = {"Misc Attack"}

_DNAT_RE = re.compile(r"dnat to (\d{1,3}(?:\.\d{1,3}){3})")


def get_core_service_ips() -> set[str]:
    """LAN IPs with an actively forwarded port right now — static
    (ip nat, manage-portfwd.sh) or dynamic (inet nat, miniupnpd)."""
    ips = set()
    for family, table in (("ip", "nat"), ("inet", "nat")):
        try:
            out = subprocess.run(
                [NFT, "list", "table", family, table],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            continue
        ips.update(_DNAT_RE.findall(out))
    return ips


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def read_new_alerts() -> list[dict]:
    """Alerts written to eve.json since the last recorded position."""
    if not EVE_LOG.exists():
        return []

    st = EVE_LOG.stat()
    state = load_state()
    offset = state.get("offset", 0)
    if state.get("inode") != st.st_ino or offset > st.st_size:
        offset = 0  # rotated, truncated, or first run

    alerts = []
    with EVE_LOG.open() as f:
        f.seek(offset)
        for line in f:
            try:
                evt = json.loads(line)
            except Exception:
                continue
            if evt.get("event_type") == "alert":
                alerts.append(evt)
        new_offset = f.tell()

    save_state({"inode": st.st_ino, "offset": new_offset})
    return alerts


def already_blocked(ip: str) -> bool:
    try:
        out = subprocess.run(
            [NFT, "-j", "list", "set", "inet", "dashboard_blocks", "manual_blocked"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return ip in out
    except Exception:
        return False  # fail open to the block attempt; "nft add" is idempotent anyway


def block_ip(ip: str) -> bool:
    result = subprocess.run(
        [NFT, "add", "element", "inet", "dashboard_blocks", "manual_blocked", "{", ip, "}"],
        capture_output=True, text=True, timeout=5,
    )
    return result.returncode == 0


def log_event(ip: str, alert: dict) -> None:
    AUTOBLOCK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUTOBLOCK_LOG.open("a") as f:
        f.write(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ip": ip,
            "signature": alert.get("alert", {}).get("signature", ""),
            "category": alert.get("alert", {}).get("category", ""),
            "dest_ip": alert.get("dest_ip"),
            "dest_port": alert.get("dest_port"),
        }) + "\n")


def main() -> int:
    core_ips = get_core_service_ips()
    if not core_ips:
        return 0  # nothing currently forwarded, nothing to protect

    alerts = read_new_alerts()
    newly_blocked = set()

    for evt in alerts:
        alert = evt.get("alert", {})
        if alert.get("category") not in AUTO_BLOCK_CATEGORIES:
            continue
        if evt.get("dest_ip") not in core_ips:
            continue
        src_ip = evt.get("src_ip")
        if not src_ip or src_ip in newly_blocked or already_blocked(src_ip):
            continue
        if block_ip(src_ip):
            log_event(src_ip, evt)
            newly_blocked.add(src_ip)
            print(f"blocked {src_ip} ({alert.get('signature')} -> "
                  f"{evt.get('dest_ip')}:{evt.get('dest_port')})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
