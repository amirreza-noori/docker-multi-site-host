#!/bin/bash
set -euo pipefail

SECOND_IP_EGRESS_MODE="${SECOND_IP_EGRESS_MODE:-outbound}"
SECOND_IP_INBOUND_PROXY_HOST="${SECOND_IP_INBOUND_PROXY_HOST:-host.docker.internal}"
SECOND_IP_INBOUND_PROXY_PORT="${SECOND_IP_INBOUND_PROXY_PORT:-18118}"

start_inbound_mode() {
  local upstream="${SECOND_IP_INBOUND_PROXY_HOST}:${SECOND_IP_INBOUND_PROXY_PORT}"
  local host="${SECOND_IP_INBOUND_PROXY_HOST}"
  local port="${SECOND_IP_INBOUND_PROXY_PORT}"
  local cfg=/tmp/haproxy-inbound.cfg

  echo "secondary-egress: inbound mode — http://0.0.0.0:8118 -> ${upstream}"
  echo "secondary-egress: waiting for reverse tunnel upstream at ${upstream}"

  while true; do
    if (echo > /dev/tcp/"${host}"/"${port}") 2>/dev/null; then
      echo "secondary-egress: inbound upstream ready at ${upstream}"
      break
    fi
    echo "secondary-egress: upstream not ready (${upstream}), retrying in 5s" >&2
    sleep 5
  done

  sed "s|__INBOUND_UPSTREAM__|${upstream}|" /etc/haproxy/haproxy-inbound.cfg > "${cfg}"
  echo "secondary-egress: proxy ready at http://0.0.0.0:8118"
  exec haproxy -f "${cfg}"
}

start_outbound_mode() {
  local var_name

  required_vars=(
    SECOND_IP_SSH_HOST
    SECOND_IP_SSH_PORT
    SECOND_IP_SSH_USER
  )

  for var_name in "${required_vars[@]}"; do
    if [[ -z "${!var_name:-}" ]]; then
      echo "secondary-egress: missing environment variable ${var_name}" >&2
      exit 1
    fi
  done

  if [[ ! -f /run/secrets/ssh_key ]]; then
    echo "secondary-egress: missing SSH private key mount at /run/secrets/ssh_key" >&2
    echo "secondary-egress: set SECOND_IP_SSH_KEY_PATH in .env to the private key file on the Docker host" >&2
    exit 1
  fi

  mkdir -p /root/.ssh
  chmod 700 /root/.ssh
  cp /run/secrets/ssh_key /root/.ssh/id_rsa
  chmod 600 /root/.ssh/id_rsa
  touch /root/.ssh/known_hosts
  chmod 644 /root/.ssh/known_hosts

  local strict_host_key_checking="yes"
  if ! ssh-keyscan -p "${SECOND_IP_SSH_PORT}" -T 12 -t rsa,ecdsa,ed25519 "${SECOND_IP_SSH_HOST}" > /root/.ssh/known_hosts 2>/tmp/ssh-keyscan.err; then
    echo "secondary-egress: ssh-keyscan failed for ${SECOND_IP_SSH_HOST}:${SECOND_IP_SSH_PORT}" >&2
    cat /tmp/ssh-keyscan.err >&2 || true
    echo "secondary-egress: falling back to accept-new host key on first SSH connect" >&2
    : > /root/.ssh/known_hosts
    strict_host_key_checking="accept-new"
  fi

  local -a ssh_hardening_args=(
    -o "ConnectTimeout=90"
    -o "ConnectionAttempts=3"
    -o "TCPKeepAlive=yes"
    -o "ServerAliveInterval=20"
    -o "ServerAliveCountMax=3"
  )

  local -a ssh_common_args=(
    -p "${SECOND_IP_SSH_PORT}"
    -i /root/.ssh/id_rsa
    -o "BatchMode=yes"
    -o "StrictHostKeyChecking=${strict_host_key_checking}"
    -o "UserKnownHostsFile=/root/.ssh/known_hosts"
    "${ssh_hardening_args[@]}"
  )

  echo "secondary-egress: testing SSH to ${SECOND_IP_SSH_USER}@${SECOND_IP_SSH_HOST}:${SECOND_IP_SSH_PORT}"
  local login_ok=""
  local login_backoff=5
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if ssh "${ssh_common_args[@]}" "${SECOND_IP_SSH_USER}@${SECOND_IP_SSH_HOST}" true; then
      login_ok="yes"
      break
    fi
    echo "secondary-egress: SSH login attempt ${attempt} failed, retrying in ${login_backoff}s" >&2
    sleep "${login_backoff}"
    (( login_backoff < 30 )) && login_backoff=$(( login_backoff * 2 ))
  done
  if [[ -z "${login_ok}" ]]; then
    echo "secondary-egress: SSH login failed after retries" >&2
    echo "secondary-egress: check SECOND_IP_SSH_HOST, SECOND_IP_SSH_PORT, SECOND_IP_SSH_USER, SECOND_IP_SSH_KEY_PATH, and authorized_keys on the target server" >&2
    exit 1
  fi

  local -a base_ssh_args=(
    -N
    -D
    "__SOCKS_PORT__"
    -p "${SECOND_IP_SSH_PORT}"
    -o "ExitOnForwardFailure=yes"
    -o "StrictHostKeyChecking=${strict_host_key_checking}"
    -o "UserKnownHostsFile=/root/.ssh/known_hosts"
    -o "GatewayPorts=no"
    "${ssh_hardening_args[@]}"
    -i /root/.ssh/id_rsa
    "${SECOND_IP_SSH_USER}@${SECOND_IP_SSH_HOST}"
  )

  start_tunnel() {
    local socks_port="$1"
    local -a ssh_args=("${base_ssh_args[@]}")
    ssh_args[2]="${socks_port}"

    AUTOSSH_GATETIME=0 \
    AUTOSSH_LOGLEVEL=0 \
    AUTOSSH_POLL=10 \
    AUTOSSH_PORT=0 \
    autossh "${ssh_args[@]}" &
  }

  local -a reverse_forward_args=()
  build_reverse_forward_args() {
    local -a entries
    IFS=', ' read -ra entries <<< "${SECOND_IP_REVERSE_PORTS:-}"

    local entry remote_port target_host target_port
    for entry in "${entries[@]}"; do
      [[ -z "${entry}" ]] && continue
      IFS=':' read -r remote_port target_host target_port <<< "${entry}"
      target_host="${target_host:-host.docker.internal}"
      target_port="${target_port:-${remote_port}}"
      reverse_forward_args+=( -R "0.0.0.0:${remote_port}:${target_host}:${target_port}" )
      echo "secondary-egress: reverse forward ${SECOND_IP_SSH_HOST}:${remote_port} -> ${target_host}:${target_port}"
    done
  }

  start_reverse_tunnel() {
    local -a ssh_args=(
      -N
      "${reverse_forward_args[@]}"
      -p "${SECOND_IP_SSH_PORT}"
      -o "ExitOnForwardFailure=yes"
      -o "StrictHostKeyChecking=${strict_host_key_checking}"
      -o "UserKnownHostsFile=/root/.ssh/known_hosts"
      "${ssh_hardening_args[@]}"
      -i /root/.ssh/id_rsa
      "${SECOND_IP_SSH_USER}@${SECOND_IP_SSH_HOST}"
    )

    AUTOSSH_GATETIME=0 \
    AUTOSSH_LOGLEVEL=0 \
    AUTOSSH_POLL=10 \
    AUTOSSH_PORT=0 \
    autossh "${ssh_args[@]}" &
  }

  wait_for_any_socks_port() {
    local tries=120
    local ports=(1081 1082)
    local port

    while (( tries > 0 )); do
      for port in "${ports[@]}"; do
        if (echo > /dev/tcp/127.0.0.1/"${port}") 2>/dev/null; then
          echo "secondary-egress: SOCKS tunnel ready on port ${port}"
          return 0
        fi
      done
      sleep 1
      ((tries--))
    done

    echo "secondary-egress: no SOCKS tunnel became ready (1081/1082)" >&2
    return 1
  }

  start_tunnel 1081
  start_tunnel 1082

  wait_for_any_socks_port

  if [[ -n "${SECOND_IP_REVERSE_PORTS:-}" ]]; then
    build_reverse_forward_args
    if (( ${#reverse_forward_args[@]} > 0 )); then
      start_reverse_tunnel
    fi
  fi

  haproxy -f /etc/haproxy/haproxy.cfg &
  echo "secondary-egress: proxy ready at http://0.0.0.0:8118"
  exec privoxy --no-daemon /etc/privoxy/config
}

case "${SECOND_IP_EGRESS_MODE}" in
  inbound) start_inbound_mode ;;
  outbound) start_outbound_mode ;;
  *)
    echo "secondary-egress: invalid SECOND_IP_EGRESS_MODE=${SECOND_IP_EGRESS_MODE} (use outbound or inbound)" >&2
    exit 1
    ;;
esac
