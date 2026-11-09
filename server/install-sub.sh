#!/usr/bin/env bash
# Installs the subscription service. Expects /tmp/ssj-sub-server.py and /tmp/ssj-links.txt
# (uploaded by deploy.sh). Prints the subscription URL.
set -euo pipefail

PUBLIC_IP="${SSJ_PUBLIC_IP:?}"
PORT="${SSJ_SUB_PORT:-2096}"
TITLE="${SSJ_SUB_TITLE:-SSJ VPN}"
UPDATE_HOURS="${SSJ_SUB_UPDATE_HOURS:-12}"
STATE=/usr/local/etc/ssj/sub.env

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
if ! command -v python3 >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends python3-minimal >/dev/null
fi

mkdir -p /usr/local/etc/ssj /usr/local/lib/ssj /etc/ssj-sub
chmod 700 /usr/local/etc/ssj
# Token is kept across re-deploys so the subscription URL in the apps stays valid.
if [[ ! -f $STATE ]]; then
  (umask 077; echo "SSJ_SUB_TOKEN=$(openssl rand -hex 16)" >"$STATE")
fi
# shellcheck disable=SC1090
source "$STATE"

install -m 755 /tmp/ssj-sub-server.py /usr/local/lib/ssj/sub-server.py
install -m 644 /tmp/ssj-links.txt /etc/ssj-sub/links.txt
rm -f /tmp/ssj-sub-server.py /tmp/ssj-links.txt

cat >/etc/systemd/system/ssj-sub.service <<EOF
[Unit]
Description=SSJ VPN subscription server
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Environment=SSJ_SUB_TOKEN=$SSJ_SUB_TOKEN
Environment=SSJ_SUB_PORT=$PORT
Environment="SSJ_SUB_TITLE=$TITLE"
Environment=SSJ_SUB_UPDATE_HOURS=$UPDATE_HOURS
ExecStart=/usr/bin/env python3 /usr/local/lib/ssj/sub-server.py
Restart=always
RestartSec=3
DynamicUser=yes
ProtectSystem=strict
ProtectHome=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q 'Status: active'; then
  ufw allow "$PORT/tcp" >/dev/null
fi

systemctl daemon-reload
systemctl enable ssj-sub >/dev/null 2>&1
systemctl restart ssj-sub
sleep 1
systemctl is-active --quiet ssj-sub || { journalctl -u ssj-sub -n 30 --no-pager >&2; exit 1; }
echo "---SSJ-SUB-URL--- http://$PUBLIC_IP:$PORT/$SSJ_SUB_TOKEN"
