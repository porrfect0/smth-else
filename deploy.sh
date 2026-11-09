#!/usr/bin/env bash
# SSJ VPN: deploys both nodes and the subscription from your computer over SSH.
# Usage: ./deploy.sh            (settings in ssj.conf)
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=ssj.conf
source ./ssj.conf

SSH_OPTS=(-p "$SSH_PORT" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=15)
out_dir="$(mktemp -d)"
trap 'rm -rf "$out_dir"' EXIT

# Runs server/install-node.sh on a host with the given SSJ_* variables; prints the links.
run_node() {
  local ip="$1"; shift
  local env_str="" kv
  for kv in "SSJ_PUBLIC_IP=$ip" "SSJ_SNI=$REALITY_SNI" "SSJ_XRAY_VERSION=$XRAY_VERSION" "$@"; do
    env_str+="$(printf '%q ' "$kv")"
  done
  echo "==> $ip" >&2
  ssh "${SSH_OPTS[@]}" "$SSH_USER@$ip" "env $env_str bash -s" <server/install-node.sh \
    | sed -n '/---SSJ-LINKS-BEGIN---/,/---SSJ-LINKS-END---/{//!p;}'
}

run_node "$NODE1_IP" SSJ_PROFILE=single "SSJ_NAME_VISION=$NODE1_NAME" >"$out_dir/node1.txt"
run_node "$NODE2_IP" SSJ_PROFILE=triple SSJ_LOW_DISK=1 \
  "SSJ_NAME_VISION=$NODE2_NAME_VISION" "SSJ_NAME_XHTTP=$NODE2_NAME_XHTTP" "SSJ_NAME_RESERVE=$NODE2_NAME_RESERVE" \
  >"$out_dir/node2.txt"

[[ $(wc -l <"$out_dir/node1.txt") -eq 1 ]] || { echo "node1: expected 1 link" >&2; exit 1; }
[[ $(wc -l <"$out_dir/node2.txt") -eq 3 ]] || { echo "node2: expected 3 links" >&2; exit 1; }
cat "$out_dir/node1.txt" "$out_dir/node2.txt" >"$out_dir/links.txt"

echo "==> subscription on $NODE1_IP" >&2
scp -q -P "$SSH_PORT" -o StrictHostKeyChecking=accept-new \
  server/sub-server.py "$SSH_USER@$NODE1_IP:/tmp/ssj-sub-server.py"
scp -q -P "$SSH_PORT" "$out_dir/links.txt" "$SSH_USER@$NODE1_IP:/tmp/ssj-links.txt"
sub_env="$(printf '%q ' "SSJ_PUBLIC_IP=$NODE1_IP" "SSJ_SUB_PORT=$SUB_PORT" \
  "SSJ_SUB_TITLE=$VPN_NAME" "SSJ_SUB_UPDATE_HOURS=$SUB_UPDATE_HOURS")"
sub_url="$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$NODE1_IP" "env $sub_env bash -s" <server/install-sub.sh \
  | sed -n 's/^---SSJ-SUB-URL--- //p')"

echo
echo "Links:"
cat "$out_dir/links.txt"
echo
echo "Subscription ($VPN_NAME) for Happ / V2RayTun:"
echo "  $sub_url"
