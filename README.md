# Vivo-Router

Turn an Ubuntu server (e.g., a laptop) into a fully-featured router / gateway, complete with a live GUI dashboard displayed on the local screen.

---

## Features

| Feature | Details |
|---|---|
| **Interactive setup wizard** | `setup.sh` walks you through every configuration step |
| **VLAN support** | Optional VLAN tagging for WAN connections (e.g., ISP VLAN requirements) |
| **NAT / IP Forwarding** | nftables firewall with persistent rules across reboots |
| **Port forwarding** | Easy-to-use `manage-portfwd.sh` script for adding/removing rules |
| **DHCP + DNS** | dnsmasq configured for your LAN subnet |
| **Pi-hole integration** | Optional DNS-level ad blocking, wired automatically to dnsmasq |
| **GUI Dashboard** | Dark-themed web dashboard served by Flask on port 5000 |
| **Active clients** | Live DHCP lease table (hostname, IP, MAC, lease remaining) |
| **WAN IP** | Real-time WAN IP address display |
| **Link speeds** | Per-interface upload / download in B/s → MB/s |
| **Pi-hole stats** | Domains blocked, queries today, block %, unique clients |
| **System health** | CPU %, RAM %, Disk %, CPU temp, uptime |
| **Error log** | Recent dnsmasq and syslog errors shown in the dashboard |
| **Suricata IDS** | Passive network intrusion detection (ET Open ruleset) on WAN + LAN, alerts surfaced on the dashboard |
| **UPnP / NAT-PMP** | Automatic port forwarding via `miniupnpd`, with a dashboard widget showing active mappings, the requesting device, and a best-guess reason |
| **Kiosk mode** | Chromium opens the dashboard fullscreen on boot |
| **Systemd services** | Dashboard and kiosk run as managed systemd units |

---

## Hardware Setup

```
  Internet
     │
  [Modem / ONT]
     │  (WAN port)
  [Ubuntu Laptop]  ◄── this machine runs Vivo-Router
     │  (LAN port / USB-Ethernet adapter)
     │
  [Archer BE11000 Pro]  ← configured as Access Point (AP mode)
     │
  Wi-Fi clients
```

> **TP-Link Archer BE11000 Pro — AP mode**  
> Go to the router's admin panel → Advanced → Operation Mode → Access Point.
> Connect the Archer's WAN port to the Ubuntu laptop's LAN interface with an Ethernet cable.

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/DaveTheFave/Vivo-Router.git
cd Vivo-Router
```

### 2. Run the setup wizard

```bash
sudo bash setup.sh
```

The wizard will ask you for:

| Prompt | Example |
|---|---|
| WAN interface | `enp2s0` (connected to modem) |
| WAN VLAN required? | `y` / `n` — ask if ISP requires VLAN tagging |
| WAN VLAN ID | `100` — only if VLAN enabled |
| LAN interface | `enp3s0` or `eth1` (connected to AP) |
| LAN gateway IP | `192.168.1.1` |
| LAN subnet | `192.168.1.0/24` |
| DHCP range | `192.168.1.100` – `192.168.1.200` |
| DHCP lease time | `24h` |
| Upstream DNS servers | `1.1.1.1`, `8.8.8.8` |
| Install Pi-hole? | `y` / `n` |
| Kiosk mode on this screen? | `y` / `n` |

### 3. Reboot

```bash
sudo reboot
```

The dashboard will be available at **http://192.168.1.1:5000** (or whatever LAN IP you chose).

---

## Dashboard

The dashboard auto-refreshes every **5 seconds** and shows:

- 🌐 **WAN IP** — current public IP address
- 👥 **Active clients** — DHCP lease table
- ⬆⬇ **Link speeds** — real-time upload/download per interface
- 🕳 **Pi-hole** — enabled/disabled, blocked today, block rate, blocklist size
- ⚙ **System resources** — CPU, RAM, Disk progress bars + CPU temperature
- ⏱ **Uptime** — server uptime
- ⚠ **Error log** — last 20 lines from dnsmasq/syslog matching error patterns
- 🔎 **Suricata IDS** — recent alerts with severity, source/destination, and signature
- 🔌 **UPnP** — active port mappings, the requesting device, and a best-guess reason the port was opened

### Running manually (development)

```bash
cd dashboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Then open **http://localhost:5000** in a browser.

### Pi-hole API password via .env

If your Pi-hole API requires authentication (Pi-hole v6), set the password in:

```bash
cp dashboard/.env.example dashboard/.env
```

Then edit `dashboard/.env` and set:

```bash
PIHOLE_PASSWORD=your-pihole-admin-password
```

Restart the dashboard service after updating `.env`:

```bash
sudo systemctl restart vivo-router-dashboard
```

---

## Service Management

```bash
# Dashboard backend
sudo systemctl status  vivo-router-dashboard
sudo systemctl restart vivo-router-dashboard
sudo journalctl -fu    vivo-router-dashboard

# Kiosk browser (if enabled)
sudo systemctl status  vivo-router-kiosk
sudo systemctl restart vivo-router-kiosk
sudo journalctl -fu    vivo-router-kiosk

# dnsmasq (DHCP/DNS)
sudo systemctl status  dnsmasq
sudo journalctl -fu    dnsmasq
```

---

## File Structure

```
Vivo-Router/
├── setup.sh                         # Interactive router setup wizard
├── manage-portfwd.sh                # Port forwarding manager (add/remove/list rules)
├── manage-upnp-nft.sh               # miniupnpd nftables bootstrap (see UPnP section below)
├── dashboard/
│   ├── app.py                       # Flask backend / REST API
│   ├── requirements.txt             # Python dependencies (flask, psutil)
│   ├── router.conf                  # Generated by setup.sh (runtime config)
│   ├── templates/
│   │   └── index.html               # Dashboard HTML
│   └── static/
│       ├── style.css                # Dark NOC-style theme
│       └── dashboard.js             # Live-update polling logic
└── services/
    ├── vivo-router-dashboard.service    # systemd unit — Flask app
    ├── vivo-router-kiosk.service        # systemd unit — Chromium kiosk
    └── miniupnpd-nft-bootstrap.conf     # systemd drop-in — see UPnP section below
```

---

## Port Forwarding Management

Use **`manage-portfwd.sh`** to quickly add or remove port forwarding rules without manually editing nftables:

### Add a port forward

```bash
sudo bash manage-portfwd.sh add
```

Prompts you for:
- **External port** (WAN side) — e.g., `8080`
- **Internal IP** (LAN host) — e.g., `192.168.1.54`
- **Internal port** (LAN side) — defaults to external port
- **Protocol** — `tcp`, `udp`, or `both`

Example: Forward external port `8080` to internal host `192.168.1.54:8080`

### List all port forwards

```bash
sudo bash manage-portfwd.sh list
```

Shows active DNAT and forward rules.

### Remove a port forward

```bash
sudo bash manage-portfwd.sh remove
```

Prompts for the rule details and removes it.

### Reload rules (after reboot)

```bash
sudo bash manage-portfwd.sh reload
```

Restores all saved port forwarding rules from `/etc/nftables.d/10-portfwd.conf`.

---

## Suricata IDS

Suricata runs in passive IDS mode — a non-inline AF_PACKET tap on both the WAN
(`enx.201`) and LAN interfaces, so it can never affect live traffic even if
misconfigured or crashed. It uses the Emerging Threats Open ruleset, kept
current with `suricata-update`.

```bash
sudo systemctl status  suricata
sudo journalctl -fu     suricata

# Live alerts only, filtered from the firehose of flow/DNS/TLS events
tail -f /var/log/suricata/eve.json | jq -c 'select(.event_type=="alert")'
```

To quiet a noisy signature without losing the rest of the ruleset, add its
SID to `/etc/suricata/disable.conf`, then `sudo suricata-update && sudo
systemctl restart suricata`.

Recent alerts are also surfaced on the dashboard's Suricata panel.

---

## UPnP Port Forwarding

`miniupnpd` handles dynamic port forwarding requests (UPnP IGD, NAT-PMP,
PCP) from LAN clients — games, torrent clients, VoIP apps, etc. — so they
don't need a manual entry in `manage-portfwd.sh`.

**Known issue, fixed:** the `miniupnpd-nftables` package's own init script
only checks whether `table inet filter` exists before deciding whether to
create the chains it needs. Since this repo's `setup.sh` already owns that
table, the vendor script always found it "already there" and skipped
creating miniupnpd's chains entirely — so port mapping requests succeeded
over SOAP but silently never took effect in nftables.

`manage-upnp-nft.sh` creates the missing structure (purely additive — a new
`inet nat` table plus one jump rule into the existing `forward` chain) and
is wired in as an `ExecStartPre` via the systemd drop-in in
`services/miniupnpd-nft-bootstrap.conf` (deployed to
`/etc/systemd/system/miniupnpd.service.d/`), so it self-heals on every
miniupnpd start. The deployed copy of the script lives at
`/etc/miniupnpd/vivo-nft-bootstrap.sh` rather than this repo's checkout
path — `miniupnpd.service` runs with `ProtectHome=yes`, which hides `/home`
from it entirely.

Active mappings, the requesting device, and a best-guess reason for each
port are shown on the dashboard's UPnP panel.

```bash
sudo systemctl status miniupnpd
cat /var/log/miniupnpd.leases   # current mappings, mirrors live state
```

---

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard HTML page |
| `GET /api/stats` | All stats in one JSON response |
| `GET /api/clients` | Active DHCP clients |
| `GET /api/speeds` | Per-interface TX/RX rates |
| `GET /api/system` | CPU, RAM, Disk, Temp, Uptime |
| `GET /api/pihole` | Pi-hole summary stats |
| `GET /api/suricata` | Recent Suricata IDS alerts |
| `GET /api/upnp` | Active UPnP/NAT-PMP port mappings |

---

## Requirements

- Ubuntu 22.04 LTS or newer
- Two network interfaces (WAN + LAN) — a USB-to-Ethernet adapter works for the second interface
- Python 3.10+
- Internet access during setup (for package installation)
- Root / sudo access
