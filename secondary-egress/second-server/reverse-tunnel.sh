#!/bin/bash
grep -q $'\r' "$0" 2>/dev/null && exec /bin/bash <(sed 's/\r$//' "$0") "$@"
# Run on the second server (initiates SSH to the Docker host).
set -euo pipefail

CONFIG="${REVERSE_TUNNEL_CONFIG:-/etc/reverse-tunnel.env}"
if [[ -f "$CONFIG" ]]; then
  # shellcheck source=/dev/null
  source "$CONFIG"
fi

TUNNEL_SSH_HOST="${TUNNEL_SSH_HOST:?Set TUNNEL_SSH_HOST (Docker host public IP)}"
TUNNEL_SSH_PORT="${TUNNEL_SSH_PORT:-22}"
TUNNEL_SSH_USER="${TUNNEL_SSH_USER:?Set TUNNEL_SSH_USER}"
TUNNEL_SSH_KEY="${TUNNEL_SSH_KEY:?Set TUNNEL_SSH_KEY (path to private key)}"

# Ports published on the Docker host (backup-runner / secondary-egress inbound)
FTP_REMOTE_PORT="${FTP_REMOTE_PORT:-8021}"
FTP_LOCAL_PORT="${FTP_LOCAL_PORT:-21}"
# FTP passive data ports published on the Docker host (must match vsftpd pasv_min/max_port)
FTP_PASV_PORT_START="${FTP_PASV_PORT_START:-40000}"
FTP_PASV_PORT_END="${FTP_PASV_PORT_END:-40000}"
FTP_PASV_PORT_START="${FTP_PASV_PORT_START//[$'\r\n\t ]/}"
FTP_PASV_PORT_END="${FTP_PASV_PORT_END//[$'\r\n\t ]/}"
FTP_PASV_PORT_START=$((FTP_PASV_PORT_START + 0))
FTP_PASV_PORT_END=$((FTP_PASV_PORT_END + 0))
PROXY_REMOTE_PORT="${PROXY_REMOTE_PORT:-18118}"
PROXY_LOCAL_PORT="${PROXY_LOCAL_PORT:-8118}"

# Same syntax as SECOND_IP_REVERSE_PORTS in the Docker host .env
TUNNEL_REVERSE_PORTS="${TUNNEL_REVERSE_PORTS:-}"

if [[ ! -f "$TUNNEL_SSH_KEY" ]]; then
  echo "reverse-tunnel: missing key at $TUNNEL_SSH_KEY" >&2
  exit 1
fi

forward_args=(
  -R "0.0.0.0:${FTP_REMOTE_PORT}:127.0.0.1:${FTP_LOCAL_PORT}"
)

if [[ -n "$PROXY_REMOTE_PORT" ]]; then
  forward_args+=( -R "0.0.0.0:${PROXY_REMOTE_PORT}:127.0.0.1:${PROXY_LOCAL_PORT}" )
fi

echo "reverse-tunnel: publish FTP passive :${FTP_PASV_PORT_START}-${FTP_PASV_PORT_END} on Docker host"
while IFS= read -r pasv_port; do
  forward_args+=( -R "0.0.0.0:${pasv_port}:127.0.0.1:${pasv_port}" )
done < <(seq "${FTP_PASV_PORT_START}" "${FTP_PASV_PORT_END}")

build_local_forward_args() {
  local -a entries
  IFS=', ' read -ra entries <<< "${TUNNEL_REVERSE_PORTS}"

  local entry listen_port target_host target_port
  for entry in "${entries[@]}"; do
    [[ -z "${entry}" ]] && continue
    IFS=':' read -r listen_port target_host target_port <<< "${entry}"
    target_host="${target_host:-127.0.0.1}"
    target_port="${target_port:-${listen_port}}"
    forward_args+=( -L "0.0.0.0:${listen_port}:${target_host}:${target_port}" )
    echo "reverse-tunnel: publish :${listen_port} -> ${target_host}:${target_port} (via Docker host sshd)"
  done
}

build_local_forward_args

echo "reverse-tunnel: ${TUNNEL_SSH_USER}@${TUNNEL_SSH_HOST}:${TUNNEL_SSH_PORT}"
echo "reverse-tunnel: publish FTP on Docker host :${FTP_REMOTE_PORT} -> local :${FTP_LOCAL_PORT}"
if [[ -n "$PROXY_REMOTE_PORT" ]]; then
  echo "reverse-tunnel: publish proxy on Docker host :${PROXY_REMOTE_PORT} -> local :${PROXY_LOCAL_PORT}"
fi

AUTOSSH_BIN="$(command -v autossh || true)"
if [[ -z "$AUTOSSH_BIN" ]]; then
  echo "reverse-tunnel: autossh not found (install: apt install autossh)" >&2
  exit 127
fi

exec "$AUTOSSH_BIN" -M 0 -N \
  "${forward_args[@]}" \
  -p "${TUNNEL_SSH_PORT}" \
  -i "${TUNNEL_SSH_KEY}" \
  -o "BatchMode=yes" \
  -o "ExitOnForwardFailure=yes" \
  -o "ServerAliveInterval=30" \
  -o "ServerAliveCountMax=3" \
  -o "TCPKeepAlive=yes" \
  -o "StrictHostKeyChecking=accept-new" \
  "${TUNNEL_SSH_USER}@${TUNNEL_SSH_HOST}"
