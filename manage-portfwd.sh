#!/usr/bin/env bash
# =============================================================================
#  Vivo-Router Port Forwarding Manager
#  Quickly add, remove, and list port forwarding rules in nftables.
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

# ── sanity checks ─────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && die "Please run this script as root: sudo bash manage-portfwd.sh"
command -v nft &>/dev/null || die "nftables not installed."

# ── config ───────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTER_CONF="${SCRIPT_DIR}/dashboard/router.conf"
PORTFWD_CONF="/etc/nftables.d/10-portfwd.conf"

# Load router config to get WAN interface
if [[ -f "$ROUTER_CONF" ]]; then
    source "$ROUTER_CONF"
else
    die "Router config not found at ${ROUTER_CONF}. Run setup.sh first."
fi

WAN_IFACE="${WAN_IFACE:-eth0}"

# ── helpers ───────────────────────────────────────────────────────────────────
prompt() {
    local varname="$1" prompt_text="$2" default="${3:-}"
    local display_default=""
    [[ -n "$default" ]] && display_default=" [${default}]"
    read -rp "$(echo -e "${BOLD}${prompt_text}${display_default}: ${NC}")" value
    value="${value:-$default}"
    printf -v "$varname" '%s' "$value"
}

prompt_yn() {
    local question="$1" default="${2:-y}"
    local yn_display
    [[ "$default" == "y" ]] && yn_display="[Y/n]" || yn_display="[y/N]"
    read -rp "$(echo -e "${BOLD}${question} ${yn_display}: ${NC}")" answer
    answer="${answer:-$default}"
    [[ "${answer,,}" =~ ^y ]]
}

validate_ip() {
    local ip="$1"
    if [[ $ip =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        return 0
    else
        return 1
    fi
}

validate_port() {
    local port="$1"
    if [[ $port =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )); then
        return 0
    else
        return 1
    fi
}

# ── ensure portfwd config directory exists ───────────────────────────────────
mkdir -p "$(dirname "$PORTFWD_CONF")"

# ── command: add ──────────────────────────────────────────────────────────────
cmd_add() {
    info "Adding port forward rule…"
    echo ""

    # Get external port
    while true; do
        prompt EXT_PORT "External port (WAN)" ""
        if validate_port "$EXT_PORT"; then
            break
        else
            error "Invalid port number. Must be 1–65535."
        fi
    done

    # Get internal IP
    while true; do
        prompt INT_IP "Internal IP address (LAN)" ""
        if validate_ip "$INT_IP"; then
            break
        else
            error "Invalid IP address."
        fi
    done

    # Get internal port
    while true; do
        prompt INT_PORT "Internal port (LAN)" "$EXT_PORT"
        if validate_port "$INT_PORT"; then
            break
        else
            error "Invalid port number. Must be 1–65535."
        fi
    done

    # Get protocol
    prompt PROTOCOL "Protocol (tcp, udp, or both)" "tcp"
    case "$PROTOCOL" in
        tcp|udp) ;;
        both)
            PROTOCOL="tcp,udp"
            ;;
        *)
            error "Invalid protocol. Use: tcp, udp, or both"
            return 1
            ;;
    esac

    echo ""
    echo -e "  External : ${GREEN}${EXT_PORT}/${PROTOCOL}${NC}"
    echo -e "  Internal : ${GREEN}${INT_IP}:${INT_PORT}${NC}"
    echo ""

    prompt_yn "Add this port forward?" "y" || { info "Aborted."; return 0; }

    # Add to nftables dynamically
    info "Adding rule to nftables…"

    # Add prerouting rule (DNAT)
    nft add rule ip nat prerouting \
        "iifname \"${WAN_IFACE}\" ${PROTOCOL} dport ${EXT_PORT} dnat to ${INT_IP}:${INT_PORT}" \
        2>/dev/null || warn "Rule may already exist."

    # Add forward rule (to allow traffic to reach the internal host)
    nft add rule inet filter forward \
        "ip daddr ${INT_IP} ${PROTOCOL} dport ${INT_PORT} accept" \
        2>/dev/null || warn "Forward rule may already exist."

    # Persist to config file
    _persist_rules

    success "Port forward added: ${EXT_PORT}/${PROTOCOL} → ${INT_IP}:${INT_PORT}"
}

# ── command: remove ───────────────────────────────────────────────────────────
cmd_remove() {
    info "Removing port forward rule…"
    echo ""

    prompt EXT_PORT "External port to remove" ""
    prompt INT_IP "Internal IP address" ""
    prompt INT_PORT "Internal port" "$EXT_PORT"
    prompt PROTOCOL "Protocol (tcp, udp, or both)" "tcp"

    case "$PROTOCOL" in
        tcp|udp) ;;
        both)
            PROTOCOL="tcp,udp"
            ;;
        *)
            error "Invalid protocol."
            return 1
            ;;
    esac

    echo ""
    prompt_yn "Remove rule: ${EXT_PORT}/${PROTOCOL} → ${INT_IP}:${INT_PORT}?" "n" || {
        info "Aborted."
        return 0
    }

    info "Removing rules from nftables…"

    # Remove from nftables is more complex; we'll rebuild the entire ruleset
    nft delete rule ip nat prerouting \
        "iifname \"${WAN_IFACE}\" ${PROTOCOL} dport ${EXT_PORT} dnat to ${INT_IP}:${INT_PORT}" \
        2>/dev/null || warn "Prerouting rule not found."

    nft delete rule inet filter forward \
        "ip daddr ${INT_IP} ${PROTOCOL} dport ${INT_PORT} accept" \
        2>/dev/null || warn "Forward rule not found."

    _persist_rules

    success "Port forward removed."
}

# ── command: list ─────────────────────────────────────────────────────────────
cmd_list() {
    info "Current port forward rules:"
    echo ""

    # Show prerouting rules (DNAT)
    echo -e "${BOLD}Inbound (DNAT):${NC}"
    nft list chain ip nat prerouting 2>/dev/null | grep -E "dport|dnat" || echo "  (none)"

    echo ""
    echo -e "${BOLD}Forward rules:${NC}"
    nft list chain inet filter forward 2>/dev/null | grep -E "dport|daddr" || echo "  (none)"

    echo ""
}

# ── persist rules to config file ───────────────────────────────────────────────
_persist_rules() {
    info "Persisting rules…"

    # Export current rules to the portfwd config
    cat > "$PORTFWD_CONF" <<'EOF'
#!/usr/sbin/nft -f
# Port forwarding rules (generated by manage-portfwd.sh)
# WARNING: Do not edit manually; use manage-portfwd.sh instead

EOF

    # Extract and save prerouting rules
    echo "table ip nat {" >> "$PORTFWD_CONF"
    echo "  chain prerouting {" >> "$PORTFWD_CONF"
    nft list chain ip nat prerouting 2>/dev/null | grep -E "iifname|dport|dnat" | sed 's/^/    /' >> "$PORTFWD_CONF" || true
    echo "  }" >> "$PORTFWD_CONF"
    echo "}" >> "$PORTFWD_CONF"
    echo "" >> "$PORTFWD_CONF"

    # Extract and save forward rules
    echo "table inet filter {" >> "$PORTFWD_CONF"
    echo "  chain forward {" >> "$PORTFWD_CONF"
    nft list chain inet filter forward 2>/dev/null | grep -E "daddr|dport|accept" | sed 's/^/    /' >> "$PORTFWD_CONF" || true
    echo "  }" >> "$PORTFWD_CONF"
    echo "}" >> "$PORTFWD_CONF"

    success "Rules persisted to ${PORTFWD_CONF}"
}

# ── command: reload ───────────────────────────────────────────────────────────
cmd_reload() {
    if [[ ! -f "$PORTFWD_CONF" ]]; then
        warn "No persisted port forward rules found."
        return 0
    fi

    info "Reloading port forward rules…"
    nft -f "$PORTFWD_CONF" || die "Failed to load rules."
    success "Rules reloaded."
}

# ── main ──────────────────────────────────────────────────────────────────────
main() {
    local cmd="${1:-help}"

    case "$cmd" in
        add)
            cmd_add
            ;;
        remove)
            cmd_remove
            ;;
        list|ls)
            cmd_list
            ;;
        reload)
            cmd_reload
            ;;
        help|--help|-h)
            cat <<'HELP'
Vivo-Router Port Forwarding Manager

Usage:
  manage-portfwd.sh add      Interactive: add a new port forward
  manage-portfwd.sh remove   Interactive: remove a port forward
  manage-portfwd.sh list     Show all active port forwards
  manage-portfwd.sh reload   Reload persisted rules (after reboot)

Examples:
  sudo bash manage-portfwd.sh add
  sudo bash manage-portfwd.sh list
  sudo bash manage-portfwd.sh reload

Notes:
  • Rules are persisted in /etc/nftables.d/10-portfwd.conf
  • Reload rules after system reboot: sudo bash manage-portfwd.sh reload
  • All rules use nftables (modern iptables replacement)

HELP
            ;;
        *)
            error "Unknown command: $cmd"
            echo "Use: manage-portfwd.sh help"
            exit 1
            ;;
    esac
}

main "$@"
