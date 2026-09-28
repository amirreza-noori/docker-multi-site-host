#!/usr/bin/env bash
# CRLF-safe re-exec: must stay one physical line (Windows checkouts break {/fi otherwise).
MAIL_ROUTER_SRC="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"; export MAIL_ROUTER_SRC; if grep -q "$(printf '\r')" "$0" 2>/dev/null; then t=$(mktemp); tr -d '\r' <"$0" >"$t"; exec bash "$t" "$@"; fi
# Start / stop / install mail-router (single-process SMTP + admin panel).
set -euo pipefail

SCRIPT_DIR="${MAIL_ROUTER_SRC}"
PY_SCRIPT="${SCRIPT_DIR}/mail_router.py"
ENV_FILE_DEFAULT="${SCRIPT_DIR}/mail-router.env"
INSTALL_DIR="${MAIL_ROUTER_INSTALL_DIR:-/opt/mail-router}"
SYSTEMD_UNIT=/etc/systemd/system/mail-router.service

strip_crlf() { sed 's/\r$//' ; }

load_env_path() {
  if [[ -n "${MAIL_ROUTER_ENV:-}" && -f "${MAIL_ROUTER_ENV}" ]]; then
    echo "${MAIL_ROUTER_ENV}"
  elif [[ -f /etc/mail-router/mail-router.env ]]; then
    echo /etc/mail-router/mail-router.env
  elif [[ -f "${ENV_FILE_DEFAULT}" ]]; then
    echo "${ENV_FILE_DEFAULT}"
  else
    echo "${ENV_FILE_DEFAULT}"
  fi
}

read_data_dir() {
  local envf="$1" raw
  raw="$(sed -n 's/^DATA_DIR=//p' "${envf}" 2>/dev/null | head -n1 | tr -d '\r' | tr -d '"' | tr -d "'")"
  if [[ -n "${raw}" ]]; then
    echo "${raw}"
  else
    echo "/var/lib/mail-router"
  fi
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "mail-router: run as root for this command" >&2
    exit 1
  fi
}

pid_file() {
  local data_dir="$1"
  echo "${data_dir}/mail-router.pid"
}

is_running() {
  local pf="$1" pid
  [[ -f "${pf}" ]] || return 1
  pid="$(tr -d ' \r\n' < "${pf}")"
  [[ -n "${pid}" ]] || return 1
  kill -0 "${pid}" 2>/dev/null
}

have_systemd_unit() {
  [[ -f "${SYSTEMD_UNIT}" ]] && command -v systemctl >/dev/null 2>&1
}

cmd_start() {
  if have_systemd_unit; then
    systemctl start mail-router.service
    systemctl --no-pager --full status mail-router.service || true
    echo "mail-router: panel http://127.0.0.1:8088 (SSH tunnel if remote)"
    return 0
  fi
  local envf data_dir pf logf
  envf="$(load_env_path)"
  if [[ ! -f "${envf}" ]]; then
    echo "mail-router: missing env file. Copy mail-router.env.example to mail-router.env" >&2
    exit 1
  fi
  if [[ ! -f "${PY_SCRIPT}" ]]; then
    echo "mail-router: missing ${PY_SCRIPT}" >&2
    exit 1
  fi
  data_dir="$(read_data_dir "${envf}")"
  mkdir -p "${data_dir}"
  pf="$(pid_file "${data_dir}")"
  logf="${data_dir}/mail-router.log"
  if is_running "${pf}"; then
    echo "mail-router: already running (pid $(cat "${pf}"))"
    exit 0
  fi
  export MAIL_ROUTER_ENV="${envf}"
  nohup python3 "${PY_SCRIPT}" >>"${logf}" 2>&1 &
  echo $! > "${pf}"
  sleep 0.4
  if is_running "${pf}"; then
    echo "mail-router: started (pid $(cat "${pf}"))"
    echo "mail-router: log ${logf}"
    echo "mail-router: panel http://127.0.0.1:8088"
  else
    echo "mail-router: failed to start — see ${logf}" >&2
    rm -f "${pf}"
    exit 1
  fi
}

cmd_stop() {
  if have_systemd_unit; then
    systemctl stop mail-router.service
    echo "mail-router: stopped (systemd)"
    return 0
  fi
  local envf data_dir pf pid
  envf="$(load_env_path)"
  data_dir="$(read_data_dir "${envf}")"
  pf="$(pid_file "${data_dir}")"
  if ! is_running "${pf}"; then
    echo "mail-router: not running"
    rm -f "${pf}"
    exit 0
  fi
  pid="$(tr -d ' \r\n' < "${pf}")"
  kill "${pid}" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "${pid}" 2>/dev/null || break
    sleep 0.3
  done
  if kill -0 "${pid}" 2>/dev/null; then
    kill -9 "${pid}" 2>/dev/null || true
  fi
  rm -f "${pf}"
  echo "mail-router: stopped"
}

cmd_restart() {
  if have_systemd_unit; then
    systemctl restart mail-router.service
    systemctl --no-pager --full status mail-router.service || true
    return 0
  fi
  cmd_stop || true
  cmd_start
}

cmd_status() {
  if have_systemd_unit; then
    systemctl --no-pager --full status mail-router.service || true
    return 0
  fi
  local envf data_dir pf
  envf="$(load_env_path)"
  data_dir="$(read_data_dir "${envf}")"
  pf="$(pid_file "${data_dir}")"
  if is_running "${pf}"; then
    echo "mail-router: running (pid $(cat "${pf}"))"
    echo "mail-router: env ${envf}"
    echo "mail-router: data ${data_dir}"
    exit 0
  fi
  echo "mail-router: stopped"
  exit 1
}

cmd_install() {
  require_root
  local src_env keep_env=0 panel_bind panel_port tmpf data_dir_install
  for arg in "$@"; do
    case "${arg}" in
      --keep-env) keep_env=1 ;;
      --update-env)
        # legacy alias: sync env (now the default)
        keep_env=0
        ;;
      -h|--help)
        usage
        exit 0
        ;;
    esac
  done

  install -d -m 755 "${INSTALL_DIR}" /etc/mail-router
  install -d -m 750 /var/lib/mail-router

  # Dedicated service user — process cannot read unrelated home dirs / other apps
  if ! id -u mail-router >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/mail-router --shell /usr/sbin/nologin \
      --comment "mail-router daemon" mail-router
    echo "install: created system user mail-router"
  fi
  # Let's Encrypt privkey is often group ssl-cert
  if getent group ssl-cert >/dev/null 2>&1; then
    usermod -a -G ssl-cert mail-router || true
  fi

  # Never redirect onto the same path being read (SRC==INSTALL_DIR truncates files to empty).
  tmpf="$(mktemp)"
  strip_crlf < "${SCRIPT_DIR}/mail_router.py" > "${tmpf}"
  install -m 755 "${tmpf}" "${INSTALL_DIR}/mail_router.py"
  strip_crlf < "${SCRIPT_DIR}/mail-router.sh" > "${tmpf}"
  install -m 755 "${tmpf}" "${INSTALL_DIR}/mail-router.sh"
  rm -f "${tmpf}"

  if [[ ! -s "${INSTALL_DIR}/mail_router.py" ]]; then
    echo "install: ERROR — ${INSTALL_DIR}/mail_router.py is empty." >&2
    echo "install: Re-copy the mail-router folder from your PC, then run install again." >&2
    exit 1
  fi

  # Prefer ./mail-router.env, else example. Default: always sync into /etc (use --keep-env to skip).
  src_env="${SCRIPT_DIR}/mail-router.env.example"
  if [[ -f "${SCRIPT_DIR}/mail-router.env" ]]; then
    src_env="${SCRIPT_DIR}/mail-router.env"
  fi
  if [[ "${keep_env}" -eq 1 && -f /etc/mail-router/mail-router.env ]]; then
    echo "install: keeping existing /etc/mail-router/mail-router.env (--keep-env)"
  else
    tmpf="$(mktemp)"
    strip_crlf < "${src_env}" > "${tmpf}"
    install -m 600 -o root -g mail-router "${tmpf}" /etc/mail-router/mail-router.env
    rm -f "${tmpf}"
    echo "install: synced ${src_env} → /etc/mail-router/mail-router.env"
  fi
  chown root:mail-router /etc/mail-router
  chmod 750 /etc/mail-router
  chmod 640 /etc/mail-router/mail-router.env 2>/dev/null || true
  chown root:mail-router /etc/mail-router/mail-router.env 2>/dev/null || true

  # Point runtime scripts at installed copy
  ln -sfn "${INSTALL_DIR}/mail-router.sh" /usr/local/bin/mail-router

  # Prefer Absolute DATA_DIR from env for systemd ReadWritePaths
  data_dir_install="$(read_data_dir /etc/mail-router/mail-router.env)"
  install -d -m 750 -o mail-router -g mail-router "${data_dir_install}"
  chown -R mail-router:mail-router "${data_dir_install}"
  chmod 750 "${data_dir_install}"

  local le_ok=0
  # STARTTLS needs to read Let's Encrypt keys as non-root.
  # certbot often uses 0700 on live/archive — ACL + execute on dirs required.
  if [[ -d /etc/letsencrypt ]]; then
    if command -v setfacl >/dev/null 2>&1; then
      setfacl -m u:mail-router:rx /etc/letsencrypt 2>/dev/null || true
      setfacl -R -m u:mail-router:rX /etc/letsencrypt/live /etc/letsencrypt/archive 2>/dev/null || true
      setfacl -R -d -m u:mail-router:rX /etc/letsencrypt/live /etc/letsencrypt/archive 2>/dev/null || true
    fi
    # Fallback if ACL unavailable: allow ssl-cert group to traverse + read
    if getent group ssl-cert >/dev/null 2>&1; then
      chgrp -R ssl-cert /etc/letsencrypt/live /etc/letsencrypt/archive 2>/dev/null || true
      find /etc/letsencrypt/live /etc/letsencrypt/archive -type d -exec chmod 750 {} \; 2>/dev/null || true
      find /etc/letsencrypt/live /etc/letsencrypt/archive -type f -name 'fullchain*.pem' -exec chmod 644 {} \; 2>/dev/null || true
      find /etc/letsencrypt/live /etc/letsencrypt/archive -type f -name 'cert*.pem' -exec chmod 644 {} \; 2>/dev/null || true
      find /etc/letsencrypt/live /etc/letsencrypt/archive -type f -name 'chain*.pem' -exec chmod 644 {} \; 2>/dev/null || true
      find /etc/letsencrypt/live /etc/letsencrypt/archive -type f -name 'privkey*.pem' -exec chmod 640 {} \; 2>/dev/null || true
    fi
    le_ok=0
    for f in /etc/letsencrypt/live/*/fullchain.pem; do
      [[ -e "${f}" ]] || continue
      if runuser -u mail-router -- test -r "${f}"; then
        le_ok=1
        break
      fi
    done
    if [[ "${le_ok}" -eq 0 ]]; then
      echo "install: WARNING — mail-router cannot read Let's Encrypt certs; STARTTLS may be off" >&2
      echo "install: fix with: setfacl -R -m u:mail-router:rX /etc/letsencrypt/live /etc/letsencrypt/archive" >&2
    else
      echo "install: Let's Encrypt certs readable by mail-router"
    fi
  fi

  panel_bind="$(sed -n 's/^PANEL_BIND=//p' /etc/mail-router/mail-router.env | head -n1 | tr -d '\r' | tr -d '"' | tr -d "'")"
  panel_port="$(sed -n 's/^PANEL_PORT=//p' /etc/mail-router/mail-router.env | head -n1 | tr -d '\r' | tr -d '"' | tr -d "'")"
  panel_bind="${panel_bind:-127.0.0.1}"
  panel_port="${panel_port:-8088}"

  local supp_groups=""
  if getent group ssl-cert >/dev/null 2>&1; then
    supp_groups="SupplementaryGroups=ssl-cert"
  fi

  cat > "${SYSTEMD_UNIT}" <<EOF
[Unit]
Description=mail-router lightweight SMTP router and admin panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mail-router
Group=mail-router
${supp_groups}
Environment=MAIL_ROUTER_ENV=/etc/mail-router/mail-router.env
Environment=PYTHONDONTWRITEBYTECODE=1
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 -u ${INSTALL_DIR}/mail_router.py
Restart=always
RestartSec=3
LimitNOFILE=65535
UMask=0077
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mail-router

# Bind privileged SMTP ports without full root
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true

# Filesystem isolation — process cannot read other users' homes or write outside data/config
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
ReadWritePaths=${data_dir_install} /etc/mail-router /var/lib/mail-router
ReadOnlyPaths=-/etc/letsencrypt

[Install]
WantedBy=multi-user.target
EOF

  # Preflight as service user (clear errors before restart loop)
  if ! runuser -u mail-router -- test -r /etc/mail-router/mail-router.env; then
    echo "install: ERROR — mail-router cannot read /etc/mail-router/mail-router.env" >&2
    ls -la /etc/mail-router/ >&2 || true
    exit 1
  fi
  if ! runuser -u mail-router -- test -w "${data_dir_install}"; then
    echo "install: ERROR — mail-router cannot write ${data_dir_install}" >&2
    ls -la "${data_dir_install}" >&2 || true
    exit 1
  fi

  systemctl daemon-reload
  systemctl enable mail-router.service
  systemctl restart mail-router.service
  sleep 2
  if ! systemctl is-active --quiet mail-router.service; then
    echo "install: ERROR — service failed to stay up. Recent logs:" >&2
    journalctl -u mail-router -n 80 --no-pager >&2 || true
    exit 1
  fi
  systemctl --no-pager --full status mail-router.service || true
  echo "install: mail-router.service enabled and started (user=mail-router)"
  echo "install: panel bind ${panel_bind}:${panel_port}"
  if [[ "${panel_bind}" == "127.0.0.1" || "${panel_bind}" == "localhost" || "${panel_bind}" == "::1" ]]; then
    echo "install: open via: ssh -L ${panel_port}:127.0.0.1:${panel_port} root@SERVER"
    echo "install: then browse http://127.0.0.1:${panel_port}"
  else
    echo "install: WARNING — PANEL_BIND is not localhost; admin UI is network-exposed"
    echo "install: try http://SERVER_IP:${panel_port} (ensure firewall allows it)"
  fi
  echo "install: logs: journalctl -u mail-router -n 50 --no-pager"
}

cmd_uninstall() {
  require_root
  systemctl disable --now mail-router.service 2>/dev/null || true
  rm -f "${SYSTEMD_UNIT}" /usr/local/bin/mail-router
  systemctl daemon-reload 2>/dev/null || true
  if [[ "${1:-}" == "--purge-data" ]]; then
    rm -rf /opt/mail-router /etc/mail-router /var/lib/mail-router
    echo "uninstall: removed install + config + data"
  else
    echo "uninstall: service removed; left /etc/mail-router and /var/lib/mail-router (use --purge-data)"
  fi
}

cmd_reset_admin() {
  require_root
  local envf="${MAIL_ROUTER_ENV:-/etc/mail-router/mail-router.env}"
  if [[ ! -f "${envf}" ]]; then
    envf="$(load_env_path)"
  fi
  export MAIL_ROUTER_ENV="${envf}"
  local py="${INSTALL_DIR}/mail_router.py"
  [[ -f "${py}" ]] || py="${SCRIPT_DIR}/mail_router.py"
  if [[ $# -ge 1 ]]; then
    python3 -u "${py}" reset-admin "$1"
  else
    python3 -u "${py}" reset-admin
  fi
}

usage() {
  cat <<EOF
Usage: bash mail-router.sh <command>

  start              Start (systemd if installed, else nohup)
  stop               Stop
  restart            Restart
  status             Show running state
  install            Install under /opt + systemd; always syncs ./mail-router.env → /etc
  install --keep-env Keep existing /etc/mail-router/mail-router.env
  reset-admin [pw]   Set admin panel password (default: ADMIN_PASSWORD from env)
  uninstall [--purge-data]

Env file search order at runtime:
  \$MAIL_ROUTER_ENV, /etc/mail-router/mail-router.env, ./mail-router.env

Does not modify mail-smtp. Do not bind the same SMTP ports on one host.
EOF
}

cmd="${1:-}"
shift || true
case "${cmd}" in
  start) cmd_start "$@" ;;
  stop) cmd_stop "$@" ;;
  restart) cmd_restart "$@" ;;
  status) cmd_status "$@" ;;
  install) cmd_install "$@" ;;
  uninstall) cmd_uninstall "$@" ;;
  reset-admin|passwd-admin) cmd_reset_admin "$@" ;;
  *) usage; exit 1 ;;
esac
