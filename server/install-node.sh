#!/usr/bin/env bash
# SSJ VPN node installer. Runs on the server as root (deploy.sh pipes it over SSH).
# Idempotent: keys/UUIDs are generated once and kept in $STATE so re-runs never break clients.
set -euo pipefail

PROFILE="${SSJ_PROFILE:?SSJ_PROFILE must be 'single' or 'triple'}"
PUBLIC_IP="${SSJ_PUBLIC_IP:?SSJ_PUBLIC_IP is required}"
SNI="${SSJ_SNI:-www.nvidia.com}"
LOW_DISK="${SSJ_LOW_DISK:-0}"
XRAY_VERSION="${SSJ_XRAY_VERSION:-}"
NAME_VISION="${SSJ_NAME_VISION:-SSJ VPN}"
NAME_XHTTP="${SSJ_NAME_XHTTP:-SSJ VPN XHTTP}"
NAME_RESERVE="${SSJ_NAME_RESERVE:-SSJ VPN Reserve}"

PORT_VISION=443
PORT_RESERVE=8443

STATE_DIR=/usr/local/etc/ssj
STATE="$STATE_DIR/state.env"
XRAY_CFG=/usr/local/etc/xray/config.json

log() { printf '[ssj] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root"
[[ $PROFILE == single || $PROFILE == triple ]] || die "unknown profile: $PROFILE"

urlencode() {
  local LC_ALL=C s="$1" out="" c i
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) out+="$c" ;;
      *) printf -v c '%%%02X' "'$c"; out+="${c: -3}" ;;
    esac
  done
  printf '%s' "$out"
}

port_busy() {
  ss -Hltn "sport = :$1" 2>/dev/null | grep -q . && ! ss -Hltnp "sport = :$1" 2>/dev/null | grep -q '"xray"'
}

# --- packages ---------------------------------------------------------------
if command -v apt-get >/dev/null; then
  missing=()
  for bin in curl openssl ss; do command -v "$bin" >/dev/null || missing+=("$bin"); done
  if ((${#missing[@]})); then
    log "installing: curl ca-certificates openssl iproute2"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends curl ca-certificates openssl iproute2 >/dev/null
  fi
fi
for bin in curl openssl ss systemctl; do command -v "$bin" >/dev/null || die "missing $bin"; done

ports=("$PORT_VISION")
[[ $PROFILE == triple ]] && ports+=("$PORT_RESERVE")
for p in "${ports[@]}"; do
  port_busy "$p" && die "port $p is used by another program (nginx/apache?) — free it first"
done

# --- Xray ---------------------------------------------------------------------
log "installing/updating Xray"
install_args=(install)
[[ -n $XRAY_VERSION ]] && install_args+=(--version "$XRAY_VERSION")
# Geo databases (~20 MB) are not used by this config.
[[ $LOW_DISK == 1 ]] && install_args+=(--without-geodata)
installer="$(mktemp)"
curl -fsSL --retry 3 -o "$installer" https://github.com/XTLS/Xray-install/raw/main/install-release.sh
bash "$installer" "${install_args[@]}" >/dev/null
rm -f "$installer"
XRAY=/usr/local/bin/xray
[[ -x $XRAY ]] || die "xray binary not found after install"

# --- persistent secrets -------------------------------------------------------
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
if [[ -f $STATE ]]; then
  # shellcheck disable=SC1090
  source "$STATE"
fi
if [[ -z ${UUID:-} || -z ${PRIVATE_KEY:-} || -z ${PUBLIC_KEY:-} ]]; then
  log "generating keys"
  UUID="$($XRAY uuid)"
  # Output format varies between versions ("Private key:/Public key:" vs "PrivateKey:/Password:");
  # the first two lines are always private then public.
  keys="$($XRAY x25519)"
  PRIVATE_KEY="$(printf '%s\n' "$keys" | awk -F': *' 'NR==1{print $2}')"
  PUBLIC_KEY="$(printf '%s\n' "$keys" | awk -F': *' 'NR==2{print $2}')"
  SHORT_ID="$(openssl rand -hex 8)"
  XHTTP_PATH="/$(openssl rand -hex 6)"
  [[ -n $PRIVATE_KEY && -n $PUBLIC_KEY ]] || die "could not parse 'xray x25519' output"
  umask 077
  cat >"$STATE" <<EOF
UUID='$UUID'
PRIVATE_KEY='$PRIVATE_KEY'
PUBLIC_KEY='$PUBLIC_KEY'
SHORT_ID='$SHORT_ID'
XHTTP_PATH='$XHTTP_PATH'
EOF
fi

# --- config -------------------------------------------------------------------
reality() {
  cat <<EOF
"security": "reality",
        "realitySettings": {
          "show": false,
          "target": "$SNI:443",
          "xver": 0,
          "serverNames": ["$SNI"],
          "privateKey": "$PRIVATE_KEY",
          "shortIds": ["$SHORT_ID"]
        }
EOF
}
SNIFF='"sniffing": { "enabled": true, "destOverride": ["http", "tls", "quic"], "routeOnly": true }'

# XHTTP shares port 443 with Vision: non-VLESS traffic inside Reality falls back to this socket.
FALLBACKS=""
[[ $PROFILE == triple ]] && FALLBACKS=', "fallbacks": [{ "dest": "@ssj-xhttp" }]'

inbounds="$(cat <<EOF
    {
      "tag": "vision",
      "listen": "0.0.0.0",
      "port": $PORT_VISION,
      "protocol": "vless",
      "settings": {
        "clients": [{ "id": "$UUID", "flow": "xtls-rprx-vision" }],
        "decryption": "none"$FALLBACKS
      },
      "streamSettings": {
        "network": "raw",
        $(reality)
      },
      $SNIFF
    }
EOF
)"

if [[ $PROFILE == triple ]]; then
  inbounds+="$(cat <<EOF
,
    {
      "tag": "xhttp",
      "listen": "@ssj-xhttp",
      "protocol": "vless",
      "settings": { "clients": [{ "id": "$UUID" }], "decryption": "none" },
      "streamSettings": {
        "network": "xhttp",
        "xhttpSettings": { "path": "$XHTTP_PATH", "mode": "auto" }
      },
      $SNIFF
    },
    {
      "tag": "reserve",
      "listen": "0.0.0.0",
      "port": $PORT_RESERVE,
      "protocol": "vless",
      "settings": { "clients": [{ "id": "$UUID" }], "decryption": "none" },
      "streamSettings": {
        "network": "xhttp",
        "xhttpSettings": { "path": "$XHTTP_PATH", "mode": "packet-up" },
        $(reality)
      },
      $SNIFF
    }
EOF
)"
fi

new_cfg="$(mktemp --suffix=.json)"
cat >"$new_cfg" <<EOF
{
  "log": { "loglevel": "warning", "access": "none" },
  "inbounds": [
$inbounds
  ],
  "outbounds": [
    { "tag": "direct", "protocol": "freedom" },
    { "tag": "block", "protocol": "blackhole" }
  ],
  "routing": {
    "domainStrategy": "AsIs",
    "rules": [
      {
        "type": "field",
        "ip": ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
               "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "fe80::/10", "::1/128"],
        "outboundTag": "block"
      },
      { "type": "field", "protocol": ["bittorrent"], "outboundTag": "block" }
    ]
  }
}
EOF
"$XRAY" run -test -c "$new_cfg" >&2 || die "generated config failed 'xray run -test'"
install -m 644 "$new_cfg" "$XRAY_CFG"
rm -f "$new_cfg"

# --- stability tuning ---------------------------------------------------------
mkdir -p /etc/systemd/system/xray.service.d
cat >/etc/systemd/system/xray.service.d/10-ssj.conf <<'EOF'
[Unit]
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=3
LimitNOFILE=1048576
EOF

cat >/etc/sysctl.d/99-ssj.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.tcp_keepalive_time = 600
net.ipv4.tcp_keepalive_intvl = 30
net.ipv4.tcp_keepalive_probes = 5
net.core.somaxconn = 4096
EOF
sysctl --system >/dev/null 2>&1 || log "warning: sysctl apply failed (container/VPS kernel?)"

if [[ $LOW_DISK == 1 ]]; then
  mkdir -p /etc/systemd/journald.conf.d
  printf '[Journal]\nSystemMaxUse=30M\n' >/etc/systemd/journald.conf.d/10-ssj.conf
  systemctl restart systemd-journald || true
  journalctl --vacuum-size=30M >/dev/null 2>&1 || true
  command -v apt-get >/dev/null && apt-get clean || true
fi

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q 'Status: active'; then
  for p in "${ports[@]}"; do ufw allow "$p/tcp" >/dev/null; done
fi

systemctl daemon-reload
systemctl enable xray >/dev/null 2>&1
systemctl restart xray
sleep 2
systemctl is-active --quiet xray || { journalctl -u xray -n 30 --no-pager >&2; die "xray failed to start"; }
log "xray is running on ports: ${ports[*]}"

# --- client links -------------------------------------------------------------
reality_q="security=reality&sni=$SNI&fp=chrome&pbk=$PUBLIC_KEY&sid=$SHORT_ID"
echo "---SSJ-LINKS-BEGIN---"
echo "vless://$UUID@$PUBLIC_IP:$PORT_VISION?encryption=none&$reality_q&type=tcp&flow=xtls-rprx-vision#$(urlencode "$NAME_VISION")"
if [[ $PROFILE == triple ]]; then
  echo "vless://$UUID@$PUBLIC_IP:$PORT_VISION?encryption=none&$reality_q&type=xhttp&path=$(urlencode "$XHTTP_PATH")&mode=auto#$(urlencode "$NAME_XHTTP")"
  echo "vless://$UUID@$PUBLIC_IP:$PORT_RESERVE?encryption=none&$reality_q&type=xhttp&path=$(urlencode "$XHTTP_PATH")&mode=packet-up#$(urlencode "$NAME_RESERVE")"
fi
echo "---SSJ-LINKS-END---"
