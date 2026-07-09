#!/bin/bash
grep -q $'\r' "$0" 2>/dev/null && exec /bin/bash <(sed 's/\r$//' "$0") "$@"
# Install reverse-tunnel files to fixed system paths. Safe to run from any working directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

strip_crlf() {
  sed 's/\r$//'
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "install: run as root (e.g. sudo bash ${SCRIPT_DIR}/install.sh)" >&2
    exit 1
  fi
}

install_privoxy() {
  if ! command -v privoxy >/dev/null 2>&1; then
    apt-get update
    apt-get install -y privoxy
  fi
  strip_crlf < "${SCRIPT_DIR}/privoxy.example.config" > /etc/privoxy/config
  chmod 644 /etc/privoxy/config
  systemctl enable privoxy
  systemctl restart privoxy
  echo "install: privoxy configured at 127.0.0.1:8118"
}

install_tunnel() {
  local env_source="${SCRIPT_DIR}/reverse-tunnel.example.env"
  [[ -f "${SCRIPT_DIR}/reverse-tunnel.env" ]] && env_source="${SCRIPT_DIR}/reverse-tunnel.env"

  local script_tmp service_tmp
  script_tmp="$(mktemp)"
  service_tmp="$(mktemp)"
  trap 'rm -f "${script_tmp}" "${service_tmp}"' RETURN

  install -d /usr/local/bin
  strip_crlf < "${SCRIPT_DIR}/reverse-tunnel.sh" > "${script_tmp}"
  install -m 755 "${script_tmp}" /usr/local/bin/reverse-tunnel.sh

  local src_lines dst_lines
  src_lines="$(wc -l < "${script_tmp}" | tr -d ' ')"
  dst_lines="$(wc -l < /usr/local/bin/reverse-tunnel.sh | tr -d ' ')"
  echo "install: installed /usr/local/bin/reverse-tunnel.sh (${dst_lines} lines)"

  if [[ "${src_lines}" != "${dst_lines}" ]]; then
    echo "install: ERROR — ${dst_lines} lines installed, ${src_lines} in ${SCRIPT_DIR}/reverse-tunnel.sh" >&2
    exit 1
  fi

  if grep -q 'FTP_PASV_PORT_START' "${SCRIPT_DIR}/reverse-tunnel.sh" \
     && ! grep -q 'FTP_PASV_PORT_START' /usr/local/bin/reverse-tunnel.sh; then
    echo "install: ERROR — /usr/local/bin/reverse-tunnel.sh was not updated; re-run install from ${SCRIPT_DIR}" >&2
    exit 1
  fi

  if [[ ! -f /etc/reverse-tunnel.env ]] || [[ "${update_env}" -eq 1 ]]; then
    strip_crlf < "${env_source}" > /etc/reverse-tunnel.env
    chmod 600 /etc/reverse-tunnel.env
    echo "install: installed /etc/reverse-tunnel.env from ${env_source##*/}"
  else
    echo "install: keeping existing /etc/reverse-tunnel.env (use --update-env to replace)"
  fi

  strip_crlf < "${SCRIPT_DIR}/reverse-tunnel.service.example" > "${service_tmp}"
  install -m 644 "${service_tmp}" /etc/systemd/system/reverse-tunnel.service
  systemctl daemon-reload
  echo "install: reverse-tunnel unit installed"
}

usage() {
  cat <<EOF
Usage: sudo bash install.sh [--privoxy] [--tunnel-only] [--update-env]

  --privoxy       Also install and configure Privoxy (127.0.0.1:8118)
  --tunnel-only   Install only the reverse SSH tunnel (default)
  --update-env    Replace /etc/reverse-tunnel.env from reverse-tunnel.env in this folder (if present)

Source directory: ${SCRIPT_DIR}

After install:
  1. Edit /etc/reverse-tunnel.env
  2. sudo systemctl enable --now reverse-tunnel
EOF
}

main() {
  local with_privoxy=0
  local update_env=0

  for arg in "$@"; do
    case "${arg}" in
      --privoxy) with_privoxy=1 ;;
      --tunnel-only) ;;
      --update-env) update_env=1 ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        echo "install: unknown option ${arg}" >&2
        usage >&2
        exit 1
        ;;
    esac
  done

  require_root

  if (( with_privoxy )); then
    install_privoxy
  fi

  install_tunnel

  echo
  echo "Next:"
  echo "  sudo nano /etc/reverse-tunnel.env"
  echo "  sudo systemctl enable --now reverse-tunnel"
  echo "  sudo systemctl status reverse-tunnel"
}

main "$@"
