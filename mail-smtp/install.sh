#!/usr/bin/env bash
# One line only: multi-line CRLF fixers break before they run (then\r / fi\r).
MAIL_SMTP_SRC="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"; export MAIL_SMTP_SRC; grep -q $'\r' "$0" 2>/dev/null && { t=$(mktemp); sed 's/\r$//' "$0" >"$t"; exec bash "$t" "$@"; }
# Install mail-smtp (outbound submission only) on Ubuntu. Safe to re-run.
set -euo pipefail

echo "install: starting mail-smtp setup..."

if [[ -n "${MAIL_SMTP_SRC:-}" && -f "${MAIL_SMTP_SRC}/bin/mail-smtp-apply" ]]; then
  SCRIPT_DIR="${MAIL_SMTP_SRC}"
else
  SCRIPT_DIR="$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

strip_crlf() {
  sed 's/\r$//'
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "install: run as root (sudo bash install.sh)" >&2
    exit 1
  fi
}

usage() {
  cat <<EOF
Usage: sudo bash install.sh [--update-env]

  Installs Postfix + Dovecot auth + OpenDKIM + quotas.
  TLS: Let's Encrypt via Cloudflare DNS-01 (automatic issue + renew).
  Put MAIL_HOSTNAME, ACME_EMAIL, CF_API_TOKEN in mail-smtp.env before install.

This stack is for low-volume transactional mail with pre-created accounts.
It is not for bulk, SEO, or marketing blasts.
EOF
}

update_env=0
for arg in "$@"; do
  case "${arg}" in
    --update-env) update_env=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: ${arg}" >&2; usage; exit 1 ;;
  esac
done

require_root

export DEBIAN_FRONTEND=noninteractive

install -d -m 755 /etc/mail-smtp /usr/local/bin

env_source="${SCRIPT_DIR}/mail-smtp.env.example"
[[ -f "${SCRIPT_DIR}/mail-smtp.env" ]] && env_source="${SCRIPT_DIR}/mail-smtp.env"
if [[ ! -f /etc/mail-smtp/mail-smtp.env ]] || [[ "${update_env}" -eq 1 ]]; then
  strip_crlf < "${env_source}" > /etc/mail-smtp/mail-smtp.env
  chmod 600 /etc/mail-smtp/mail-smtp.env
  echo "install: wrote /etc/mail-smtp/mail-smtp.env from ${env_source##*/}"
else
  echo "install: keeping existing /etc/mail-smtp/mail-smtp.env (use --update-env to replace)"
fi

# Fail early if Cloudflare token missing
cf_token="$(sed -n 's/^CF_API_TOKEN=//p' /etc/mail-smtp/mail-smtp.env | head -n1 | tr -d '\r' | tr -d '"' | tr -d "'")"
acme_email="$(sed -n 's/^ACME_EMAIL=//p' /etc/mail-smtp/mail-smtp.env | head -n1 | tr -d '\r' | tr -d '"' | tr -d "'")"
if [[ -z "${cf_token}" ]]; then
  echo "install: CF_API_TOKEN is empty in /etc/mail-smtp/mail-smtp.env" >&2
  echo "  Create token (Zone → DNS → Edit), put it in mail-smtp.env, re-run:" >&2
  echo "  sudo bash install.sh --update-env" >&2
  exit 1
fi
if [[ -z "${acme_email}" || "${acme_email}" == *"example.com"* ]]; then
  echo "install: set a real ACME_EMAIL in mail-smtp.env" >&2
  exit 1
fi

mail_hostname="$(sed -n 's/^MAIL_HOSTNAME=//p' /etc/mail-smtp/mail-smtp.env | head -n1 | tr -d '\r' | tr -d '"' | tr -d "'")"
mail_hostname="${mail_hostname:-mail.example.com}"
echo "postfix postfix/main_mailer_type select Internet Site" | debconf-set-selections
echo "postfix postfix/mailname string ${mail_hostname}" | debconf-set-selections

echo "install: apt-get update + install packages (may take a few minutes)..."
apt-get update
apt-get install -y \
  postfix \
  opendkim \
  opendkim-tools \
  dovecot-core \
  ssl-cert \
  python3 \
  fail2ban \
  certbot \
  python3-certbot-dns-cloudflare
echo "install: packages ready"

install -d -o postfix -g postfix -m 750 /var/lib/mail-smtp

for name in mail-smtp-account mail-smtp-apply mail-smtp-policy mail-smtp-cert; do
  tmp="$(mktemp)"
  strip_crlf < "${SCRIPT_DIR}/bin/${name}" > "${tmp}"
  install -m 755 "${tmp}" "/usr/local/bin/${name}"
  rm -f "${tmp}"
done

# Legacy HTTP ACME helper (remove if previously installed)
systemctl disable --now mail-smtp-acme-http.service 2>/dev/null || true
rm -f /etc/systemd/system/mail-smtp-acme-http.service /usr/local/bin/mail-smtp-acme-http
systemctl daemon-reload 2>/dev/null || true

echo "install: issuing TLS certificate via Cloudflare DNS (automatic renew afterwards)..."
/usr/local/bin/mail-smtp-cert

if [[ -d /etc/fail2ban/jail.d ]]; then
  strip_crlf < "${SCRIPT_DIR}/fail2ban/jail.d/mail-smtp.conf" \
    > /etc/fail2ban/jail.d/mail-smtp.conf
  strip_crlf < "${SCRIPT_DIR}/fail2ban/filter.d/mail-smtp-sasl.conf" \
    > /etc/fail2ban/filter.d/mail-smtp-sasl.conf
  systemctl enable fail2ban
  systemctl restart fail2ban || true
fi

if [[ -f /etc/dovecot/conf.d/10-auth.conf ]]; then
  sed -i 's/^!include auth-system.conf.ext/#!include auth-system.conf.ext/' \
    /etc/dovecot/conf.d/10-auth.conf || true
fi

/usr/local/bin/mail-smtp-apply

echo
echo "install: done."
echo "  TLS renews automatically via certbot.timer (Cloudflare DNS) — no further cert commands."
echo "  Add a sender: mail-smtp-account add noreply@example.com"
echo "  Publish SPF / DKIM / DMARC / PTR (see README.md)"
echo "  Keep Cloudflare proxy OFF (DNS only) for MAIL_HOSTNAME"
echo "  Firewall: 587/465 from trusted IPs; inbound 25 closed"
