set -euo pipefail

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

strict_host_key_checking="yes"
if ! ssh-keyscan -p "${SECOND_IP_SSH_PORT}" -T 15 -t rsa,ecdsa,ed25519 "${SECOND_IP_SSH_HOST}" > /root/.ssh/known_hosts 2>/tmp/ssh-keyscan.err; then
  echo "secondary-egress: ssh-keyscan failed for ${SECOND_IP_SSH_HOST}:${SECOND_IP_SSH_PORT}" >&2
  cat /tmp/ssh-keyscan.err >&2 || true
  echo "secondary-egress: falling back to accept-new host key on first SSH connect" >&2
  : > /root/.ssh/known_hosts
  strict_host_key_checking="accept-new"
fi

ssh_common_args=(
  -p "${SECOND_IP_SSH_PORT}"
  -i /root/.ssh/id_rsa
  -o "BatchMode=yes"
  -o "ConnectTimeout=15"
  -o "StrictHostKeyChecking=${strict_host_key_checking}"
  -o "UserKnownHostsFile=/root/.ssh/known_hosts"
)

echo "secondary-egress: testing SSH to ${SECOND_IP_SSH_USER}@${SECOND_IP_SSH_HOST}:${SECOND_IP_SSH_PORT}"
if ! ssh "${ssh_common_args[@]}" "${SECOND_IP_SSH_USER}@${SECOND_IP_SSH_HOST}" true; then
  echo "secondary-egress: SSH login failed" >&2
  echo "secondary-egress: check SECOND_IP_SSH_HOST, SECOND_IP_SSH_PORT, SECOND_IP_SSH_USER, SECOND_IP_SSH_KEY_PATH, and authorized_keys on the target server" >&2
  exit 1
fi

base_ssh_args=(
  -N
  -D
  "__SOCKS_PORT__"
  -p "${SECOND_IP_SSH_PORT}"
  -o "ExitOnForwardFailure=yes"
  -o "ServerAliveInterval=20"
  -o "ServerAliveCountMax=3"
  -o "StrictHostKeyChecking=${strict_host_key_checking}"
  -o "UserKnownHostsFile=/root/.ssh/known_hosts"
  -o "GatewayPorts=no"
  -i /root/.ssh/id_rsa
  "${SECOND_IP_SSH_USER}@${SECOND_IP_SSH_HOST}"
)

start_tunnel() {
  local socks_port="$1"
  local monitor_port="$2"
  local -a ssh_args=("${base_ssh_args[@]}")
  ssh_args[2]="${socks_port}"

  AUTOSSH_GATETIME=0 \
  AUTOSSH_LOGLEVEL=0 \
  AUTOSSH_POLL=10 \
  AUTOSSH_PORT="${monitor_port}" \
  autossh "${ssh_args[@]}" &
}

wait_for_port() {
  local port="$1"
  local tries=60

  while (( tries > 0 )); do
    if (echo > /dev/tcp/127.0.0.1/"${port}") 2>/dev/null; then
      return 0
    fi
    sleep 1
    ((tries--))
  done

  echo "secondary-egress: SOCKS tunnel on port ${port} did not become ready" >&2
  return 1
}

start_tunnel 1081 20001
start_tunnel 1082 20002

wait_for_port 1081
wait_for_port 1082

haproxy -f /etc/haproxy/haproxy.cfg &
echo "secondary-egress: proxy ready at http://0.0.0.0:8118"
exec privoxy --no-daemon /etc/privoxy/config
