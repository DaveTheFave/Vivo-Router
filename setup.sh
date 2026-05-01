#!/usr/bin/env bash
# =============================================================================
#  Vivo-Router Setup Script
#  Turns an Ubuntu server into a fully-featured router / gateway.
#
#  What this script does:
#    1. Collects network configuration interactively
#    2. Installs required packages (dnsmasq, iptables-persistent, Pi-hole, …)
#    3. Configures IP forwarding and NAT masquerading
#    4. Sets up dnsmasq for DHCP and local DNS
#    5. Optionally installs Pi-hole and wires it to dnsmasq
#    6. Deploys the Vivo-Router GUI dashboard as a systemd service
#    7. Optionally opens the dashboard in a fullscreen kiosk browser on boot
#
#  Requirements: Ubuntu 22.04+, run as root, internet access.
# =============================================================================

set -euo pipefail

# ── colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }
hr()      { echo -e "${BOLD}──────────────────────────────────────────────────────${NC}"; }

# ── sanity checks ─────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && die "Please run this script as root: sudo bash setup.sh"
[[ "$(uname -s)" != "Linux" ]] && die "This script only runs on Linux."

command -v apt-get &>/dev/null || die "apt-get not found. Ubuntu/Debian required."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── banner ────────────────────────────────────────────────────────────────────
clear
cat <<'BANNER'

  ██╗   ██╗██╗██╗   ██╗ ██████╗       ██████╗  ██████╗ ██╗   ██╗████████╗███████╗██████╗
  ██║   ██║██║██║   ██║██╔═══██╗      ██╔══██╗██╔═══██╗██║   ██║╚══██╔══╝██╔════╝██╔══██╗
  ██║   ██║██║╚██╗ ██╔╝██║   ██║█████╗██████╔╝██║   ██║██║   ██║   ██║   █████╗  ██████╔╝
  ╚██╗ ██╔╝██║ ╚████╔╝ ██║   ██║╚════╝██╔══██╗██║   ██║██║   ██║   ██║   ██╔══╝  ██╔══██╗
   ╚████╔╝ ██║  ╚██╔╝  ╚██████╔╝      ██║  ██║╚██████╔╝╚██████╔╝   ██║   ███████╗██║  ██║
    ╚═══╝  ╚═╝   ╚═╝    ╚═════╝       ╚═╝  ╚═╝ ╚═════╝  ╚═════╝    ╚═╝   ╚══════╝╚═╝  ╚═╝

  Linux Router + GUI Dashboard  —  Archer BE11000 Pro AP Edition
BANNER
hr
echo ""

# ── helpers ───────────────────────────────────────────────────────────────────
prompt() {
    # Usage: prompt VARNAME "Prompt text" [default]
    local varname="$1" prompt_text="$2" default="${3:-}"
    local display_default=""
    [[ -n "$default" ]] && display_default=" [${default}]"
    read -rp "$(echo -e "${BOLD}${prompt_text}${display_default}: ${NC}")" value
    value="${value:-$default}"
    printf -v "$varname" '%s' "$value"
}

prompt_yn() {
    # Usage: prompt_yn "Question?" [y|n]  → returns 0=yes 1=no
    local question="$1" default="${2:-y}"
    local yn_display
    [[ "$default" == "y" ]] && yn_display="[Y/n]" || yn_display="[y/N]"
    read -rp "$(echo -e "${BOLD}${question} ${yn_display}: ${NC}")" answer
    answer="${answer:-$default}"
    [[ "${answer,,}" =~ ^y ]]
}

list_interfaces() {
    ip -o link show | awk '$2 != "lo:" {gsub(":", "", $2); print $2}'
}

# ── step 1 – gather configuration ────────────────────────────────────────────
hr
echo -e "${BOLD}STEP 1 — Network Configuration${NC}"
hr
echo ""

echo "Detected network interfaces:"
list_interfaces | while read -r iface; do
    local_ip=$(ip -o -4 addr show "$iface" 2>/dev/null | awk '{print $4}' | head -1)
    echo "  • $iface  ${local_ip:-(no IPv4)}"
done
echo ""

prompt WAN_IFACE  "WAN interface (connected to modem/ISP)"  "$(list_interfaces | head -1)"
prompt LAN_IFACE  "LAN interface (connected to TP-Link AP)" "$(list_interfaces | tail -1)"

[[ "$WAN_IFACE" == "$LAN_IFACE" ]] && die "WAN and LAN interfaces must be different."

prompt LAN_IP     "LAN gateway IP for this machine"          "192.168.1.1"
prompt LAN_SUBNET "LAN subnet (CIDR)"                        "192.168.1.0/24"
prompt DHCP_START "DHCP range start"                         "192.168.1.100"
prompt DHCP_END   "DHCP range end"                           "192.168.1.200"
prompt DHCP_LEASE "DHCP lease time"                          "24h"
prompt DNS1       "Primary upstream DNS"                     "1.1.1.1"
prompt DNS2       "Secondary upstream DNS"                   "8.8.8.8"

echo ""
hr
echo -e "${BOLD}STEP 2 — Optional Components${NC}"
hr
echo ""

INSTALL_PIHOLE=false
prompt_yn "Install Pi-hole (DNS-level ad blocker)?" "y" && INSTALL_PIHOLE=true

INSTALL_DASHBOARD=true   # always install dashboard

KIOSK_MODE=false
if prompt_yn "Open dashboard in fullscreen kiosk mode on this screen at boot?" "y"; then
    KIOSK_MODE=true
    prompt DISPLAY_ENV "Display variable for kiosk (usually :0)" ":0"
fi

echo ""
hr
echo -e "${BOLD}Configuration Summary${NC}"
hr
echo ""
echo -e "  WAN interface : ${GREEN}${WAN_IFACE}${NC}"
echo -e "  LAN interface : ${GREEN}${LAN_IFACE}${NC}"
echo -e "  LAN gateway   : ${GREEN}${LAN_IP}${NC}"
echo -e "  LAN subnet    : ${GREEN}${LAN_SUBNET}${NC}"
echo -e "  DHCP range    : ${GREEN}${DHCP_START} – ${DHCP_END}${NC}  (${DHCP_LEASE})"
echo -e "  Upstream DNS  : ${GREEN}${DNS1}, ${DNS2}${NC}"
echo -e "  Pi-hole       : ${GREEN}${INSTALL_PIHOLE}${NC}"
echo -e "  Kiosk mode    : ${GREEN}${KIOSK_MODE}${NC}"
echo ""

prompt_yn "Proceed with installation?" "y" || { info "Aborted by user."; exit 0; }

# ── step 3 – install packages ─────────────────────────────────────────────────
echo ""
hr
echo -e "${BOLD}STEP 3 — Installing Packages${NC}"
hr
echo ""

info "Updating package lists…"
apt-get update -qq

PKGS=(
    iptables
    dnsmasq curl wget git
    net-tools iproute2 nftables
    python3 python3-pip python3-venv
    vnstat
    lm-sensors
    jq
)

# Chromium for kiosk (installed separately to avoid pulling a GUI stack)
KIOSK_PKGS=(chromium-browser xorg openbox xterm)

info "Installing core packages: ${PKGS[*]}"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${PKGS[@]}" \
    || die "Package installation failed."

if $KIOSK_MODE; then
    info "Installing kiosk packages: ${KIOSK_PKGS[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${KIOSK_PKGS[@]}" \
        || warn "Some kiosk packages failed to install; kiosk mode may not work."
fi

success "Packages installed."

# ── step 4 – configure static IP on LAN interface ────────────────────────────
echo ""
hr
echo -e "${BOLD}STEP 4 — LAN Interface Configuration${NC}"
hr
echo ""

NETPLAN_FILE="/etc/netplan/01-vivo-router-lan.yaml"
info "Writing netplan config to ${NETPLAN_FILE}…"

# Derive prefix length from the CIDR notation
PREFIX="${LAN_SUBNET##*/}"

cat > "$NETPLAN_FILE" <<EOF
network:
  version: 2
  renderer: networkd
  ethernets:
    ${LAN_IFACE}:
      dhcp4: no
      addresses:
        - ${LAN_IP}/${PREFIX}
EOF

info "Applying netplan…"
netplan apply 2>/dev/null || warn "netplan apply returned non-zero; interface may need a manual restart."
success "LAN interface configured."

# ── step 5 – IP forwarding + NAT ──────────────────────────────────────────────
echo ""
hr
echo -e "${BOLD}STEP 5 — IP Forwarding & NAT${NC}"
hr
echo ""

info "Enabling IP forwarding and ipv6 forwarding…"
grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf || echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
grep -q '^net.ipv6.conf.all.forwarding=1' /etc/sysctl.conf || echo "net.ipv6.conf.all.forwarding=1" >> /etc/sysctl.conf

sysctl -w net.ipv4.ip_forward=1 >/dev/null
sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null

info "Configuring nftables firewall and NAT…"

NFT_CONF="/etc/nftables.conf"

cat > "$NFT_CONF" <<EOF
#!/usr/sbin/nft -f

flush ruleset

table inet filter {

    # ── Flowtable: bypasses netfilter for established flows ──────────────────
    flowtable ft {
        hook ingress priority 0;
        devices = { ${LAN_IFACE}, ${WAN_IFACE} };
    }

    chain input {
        type filter hook input priority 0;
        policy drop;

        # Loopback
        iif lo accept

        # Established/related
        ct state established,related accept

        # Drop invalid packets early
        ct state invalid drop

        # Allow LAN to router
        iifname "${LAN_IFACE}" accept

        # ICMP (rate-limited to prevent flood)
        ip protocol icmp limit rate 10/second accept
        ip6 nexthdr icmpv6 limit rate 10/second accept

        # SSH from LAN only
        tcp dport 22 iifname "${LAN_IFACE}" accept
    }

    chain forward {
        type filter hook forward priority 0;
        policy drop;

        # Flowtable offload: established TCP/UDP skips the rest of this chain
        ip protocol { tcp, udp } flow add @ft

        # Established/related (catches non-offloaded or first packets)
        ct state established,related accept

        # Drop invalid
        ct state invalid drop

        # LAN -> WAN
        iifname "${LAN_IFACE}" oifname "${WAN_IFACE}" accept

        # LAN -> LAN (future segments)
        iifname "${LAN_IFACE}" oifname "${LAN_IFACE}" accept

        # Port forward targets - allow forwarded traffic to reach LAN hosts
        ip daddr 192.168.1.54 tcp dport { 2323, 8920 } accept
        ip daddr 192.168.1.88 tcp dport 2222 accept
    }

    chain output {
        type filter hook output priority 0;
        policy accept;

        # Drop invalid outbound
        ct state invalid drop
    }
}

# ── IPv4 NAT ─────────────────────────────────────────────────────────────────
table ip nat {
    chain prerouting {
        type nat hook prerouting priority -100;

        # Port forwards from WAN
        iifname "${WAN_IFACE}" tcp dport 2323 dnat to 192.168.1.54
        iifname "${WAN_IFACE}" tcp dport 8920 dnat to 192.168.1.54
        iifname "${WAN_IFACE}" tcp dport 2222 dnat to 192.168.1.88
    }

    chain postrouting {
        type nat hook postrouting priority 100;
        policy accept;

        # IPv4 masquerade LAN -> WAN
        oifname "${WAN_IFACE}" ip saddr ${LAN_SUBNET} masquerade
    }
}
EOF

systemctl enable nftables >/dev/null 2>&1 || true
systemctl restart nftables || warn "nftables failed to start; check 'journalctl -u nftables'."

success "nftables firewall and NAT configured."



# ── step 6 – dnsmasq ──────────────────────────────────────────────────────────
echo ""
hr
echo -e "${BOLD}STEP 6 — DHCP / DNS (dnsmasq)${NC}"
hr
echo ""

DNSMASQ_CONF="/etc/dnsmasq.d/vivo-router.conf"
info "Writing dnsmasq config to ${DNSMASQ_CONF}…"

# If Pi-hole is being installed it will handle DNS on port 53;
# dnsmasq only needs to serve DHCP in that case.
PIHOLE_DNS_PORT=""
$INSTALL_PIHOLE && PIHOLE_DNS_PORT="port=0"

cat > "$DNSMASQ_CONF" <<EOF
# Vivo-Router dnsmasq config  (generated by setup.sh)
interface=${LAN_IFACE}
bind-interfaces
${PIHOLE_DNS_PORT}

# DHCP
dhcp-range=${DHCP_START},${DHCP_END},${DHCP_LEASE}
dhcp-option=option:router,${LAN_IP}
dhcp-option=option:dns-server,${LAN_IP}

# Upstream DNS (used when Pi-hole is not installed)
server=${DNS1}
server=${DNS2}

# Logging (used by the dashboard to show errors)
log-dhcp
log-queries
log-facility=/var/log/dnsmasq.log
EOF

# Disable the stub-resolver that often holds port 53
if systemctl is-active --quiet systemd-resolved 2>/dev/null; then
    info "Disabling systemd-resolved stub listener…"
    mkdir -p /etc/systemd/resolved.conf.d
    cat > /etc/systemd/resolved.conf.d/no-stub.conf <<EOF
[Resolve]
DNSStubListener=no
EOF
    systemctl restart systemd-resolved || true
fi

systemctl enable dnsmasq >/dev/null 2>&1 || true
systemctl restart dnsmasq || warn "dnsmasq failed to start; check 'journalctl -u dnsmasq'."
success "dnsmasq configured."

# ── step 7 – Pi-hole ──────────────────────────────────────────────────────────
if $INSTALL_PIHOLE; then
    echo ""
    hr
    echo -e "${BOLD}STEP 7 — Pi-hole Installation${NC}"
    hr
    echo ""
    info "Installing Pi-hole unattended…"

    mkdir -p /etc/pihole

    cat > /etc/pihole/setupVars.conf <<EOF
PIHOLE_INTERFACE=${LAN_IFACE}
IPV4_ADDRESS=${LAN_IP}/${PREFIX}
IPV6_ADDRESS=
QUERY_LOGGING=true
INSTALL_WEB_SERVER=true
INSTALL_WEB_INTERFACE=true
LIGHTTPD_ENABLED=true
CACHE_SIZE=10000
DNS_FQDN_REQUIRED=false
DNS_BOGUS_PRIV=true
DNSMASQ_LISTENING=local
WEBPASSWORD=
BLOCKING_ENABLED=true
PIHOLE_DNS_1=${DNS1}
PIHOLE_DNS_2=${DNS2}
EOF

    curl -sSL https://install.pi-hole.net | bash /dev/stdin --unattended \
        || warn "Pi-hole installation encountered errors; you may need to install it manually."

    success "Pi-hole installed. Admin interface: http://${LAN_IP}/admin"
fi

# ── step 8 – vnstat bandwidth monitoring ─────────────────────────────────────
echo ""
hr
echo -e "${BOLD}STEP 8 — vnstat Bandwidth Monitoring${NC}"
hr
echo ""

info "Configuring vnstat for WAN interface (${WAN_IFACE})…"
vnstat --add -i "$WAN_IFACE" 2>/dev/null || true
vnstat --add -i "$LAN_IFACE" 2>/dev/null || true
systemctl enable vnstat >/dev/null 2>&1 || true
systemctl restart vnstat || warn "vnstat failed to start."
success "vnstat configured."

# ── step 9 – dashboard ────────────────────────────────────────────────────────
echo ""
hr
echo -e "${BOLD}STEP 9 — Vivo-Router Dashboard${NC}"
hr
echo ""

DASH_DIR="${SCRIPT_DIR}/dashboard"
[[ -d "$DASH_DIR" ]] || die "Dashboard directory not found at ${DASH_DIR}. Ensure you cloned the full repo."

info "Creating Python virtual environment…"
python3 -m venv "${DASH_DIR}/.venv"
"${DASH_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${DASH_DIR}/.venv/bin/pip" install --quiet -r "${DASH_DIR}/requirements.txt" \
    || die "Failed to install dashboard Python dependencies."

# Write runtime config read by dashboard/app.py
cat > "${DASH_DIR}/router.conf" <<EOF
WAN_IFACE=${WAN_IFACE}
LAN_IFACE=${LAN_IFACE}
LAN_IP=${LAN_IP}
PIHOLE_ENABLED=${INSTALL_PIHOLE}
PIHOLE_API=http://localhost/admin/api
EOF

info "Installing systemd service for dashboard…"
DASHBOARD_SERVICE="/etc/systemd/system/vivo-router-dashboard.service"
cat > "$DASHBOARD_SERVICE" <<EOF
[Unit]
Description=Vivo-Router GUI Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${DASH_DIR}
ExecStart=${DASH_DIR}/.venv/bin/python ${DASH_DIR}/app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable vivo-router-dashboard >/dev/null 2>&1
systemctl restart vivo-router-dashboard || warn "Dashboard service failed to start; check 'journalctl -u vivo-router-dashboard'."
success "Dashboard service started on http://${LAN_IP}:5000"

# ── step 10 – kiosk mode ──────────────────────────────────────────────────────
if $KIOSK_MODE; then
    echo ""
    hr
    echo -e "${BOLD}STEP 10 — Kiosk Mode${NC}"
    hr
    echo ""

    KIOSK_SERVICE="/etc/systemd/system/vivo-router-kiosk.service"
    info "Installing kiosk systemd service…"
    cat > "$KIOSK_SERVICE" <<EOF
[Unit]
Description=Vivo-Router Dashboard Kiosk Browser
After=graphical.target vivo-router-dashboard.service
Requires=vivo-router-dashboard.service

[Service]
Type=simple
User=root
Environment=DISPLAY=${DISPLAY_ENV}
Environment=XAUTHORITY=/root/.Xauthority
ExecStartPre=/bin/sleep 5
ExecStart=/usr/bin/chromium-browser \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --kiosk \
    --no-first-run \
    --disable-extensions \
    --check-for-update-interval=31536000 \
    http://localhost:5000
Restart=always
RestartSec=10

[Install]
WantedBy=graphical.target
EOF

    systemctl daemon-reload
    systemctl enable vivo-router-kiosk >/dev/null 2>&1
    success "Kiosk service installed (starts on next graphical boot)."
fi



# ── done ──────────────────────────────────────────────────────────────────────
echo ""
hr
echo -e "${BOLD}${GREEN}  ✓  Setup Complete!${NC}"
hr
echo ""
echo -e "  Dashboard URL  : ${CYAN}http://${LAN_IP}:5000${NC}"
$INSTALL_PIHOLE && echo -e "  Pi-hole admin  : ${CYAN}http://${LAN_IP}/admin${NC}"
echo ""
echo -e "  Useful commands:"
echo -e "    systemctl status vivo-router-dashboard"
echo -e "    journalctl -fu vivo-router-dashboard"
echo -e "    journalctl -fu dnsmasq"
echo ""
echo -e "  ${YELLOW}NOTE:${NC} Reboot the server to activate all changes cleanly."
echo ""
