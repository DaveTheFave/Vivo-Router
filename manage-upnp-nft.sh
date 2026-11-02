#!/usr/bin/env bash
# =============================================================================
#  Vivo-Router — miniupnpd nftables bootstrap
#
#  miniupnpd-nftables' own init script (/etc/miniupnpd/nft_init.sh) only
#  checks whether "table inet filter" exists before deciding whether to
#  create the jump chains it needs. Vivo-Router's own firewall already owns
#  that table (setup.sh creates it), so the vendor script always finds it
#  "already there" and skips creating miniupnpd's chains entirely. The
#  daemon then accepts UPnP/PCP/NAT-PMP requests over SOAP just fine, but
#  every AddPortMapping silently fails at the netlink layer because the
#  chains it tries to write rules into were never created
#  (nft_send_rule(...) send_batch failed / mnl_cb_run returned -1 in
#  `journalctl -u miniupnpd`).
#
#  This script creates exactly the structure miniupnpd expects — additive
#  only, never touches existing rules — and is idempotent, so it's safe to
#  run on every service start. Wired in as an ExecStartPre for
#  miniupnpd.service via services/miniupnpd.service.d/override.conf, which
#  runs it before the vendor's own (broken) init script.
# =============================================================================

set -euo pipefail

NFT=nft

ensure_jump() {
    # Optional 5th arg: a pattern to insert the jump before (e.g. a log
    # rule), so accepted traffic doesn't get mislabeled in a drop log.
    # Falls back to appending at the end of the chain if not given/found.
    local family="$1" table="$2" chain="$3" target="$4" before_pattern="${5:-}"
    if $NFT -j list chain "$family" "$table" "$chain" 2>/dev/null | grep -q "\"target\":[[:space:]]*\"$target\""; then
        return 0
    fi
    local before_handle=""
    if [[ -n "$before_pattern" ]]; then
        before_handle=$($NFT -a list chain "$family" "$table" "$chain" 2>/dev/null \
            | grep "$before_pattern" | grep -oE 'handle [0-9]+' | awk '{print $2}' | head -1)
    fi
    if [[ -n "$before_handle" ]]; then
        $NFT insert rule "$family" "$table" "$chain" position "$before_handle" jump "$target"
    else
        $NFT add rule "$family" "$table" "$chain" jump "$target"
    fi
}

# ── forward-chain jump for miniupnpd's dynamic accept rules ────────────────
$NFT add table inet filter
$NFT list chain inet filter miniupnpd &>/dev/null || $NFT add chain inet filter miniupnpd
ensure_jump inet filter forward miniupnpd 'log prefix "PSAD_DROP_FORWARD'

# ── dedicated nat table for miniupnpd's dynamic DNAT entries ───────────────
# Kept separate from the hand-maintained "ip nat" table (setup.sh /
# manage-portfwd.sh) so this never touches the static port-forward rules —
# both tables' prerouting/postrouting hooks run independently for every
# IPv4 packet, so dynamic UPnP mappings take effect regardless.
if ! $NFT list table inet nat &>/dev/null; then
    $NFT -f - <<'EOF'
table inet nat {
    chain prerouting {
        type nat hook prerouting priority -100; policy accept;
    }
    chain postrouting {
        type nat hook postrouting priority 100; policy accept;
    }
}
EOF
fi
$NFT list chain inet nat prerouting_miniupnpd  &>/dev/null || $NFT add chain inet nat prerouting_miniupnpd
$NFT list chain inet nat postrouting_miniupnpd &>/dev/null || $NFT add chain inet nat postrouting_miniupnpd
ensure_jump inet nat prerouting  prerouting_miniupnpd
ensure_jump inet nat postrouting postrouting_miniupnpd

echo "miniupnpd nftables structure OK"
