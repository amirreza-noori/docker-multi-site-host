#!/usr/bin/env bash
MAIL_SMTP_SRC="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"; export MAIL_SMTP_SRC; grep -q $'\r' "$0" 2>/dev/null && { t=$(mktemp); sed 's/\r$//' "$0" >"$t"; exec bash "$t" "$@"; }
# Remove mail-smtp units/files. Does not purge Postfix/Dovecot packages unless --purge-packages.
set -euo pipefail

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "uninstall: run as root" >&2
    exit 1
  fi
}

purge_packages=0
for arg in "$@"; do
  case "${arg}" in
    --purge-packages) purge_packages=1 ;;
    *) echo "usage: sudo bash uninstall.sh [--purge-packages]" >&2; exit 1 ;;
  esac
done

require_root

rm -f /usr/local/bin/mail-smtp-account /usr/local/bin/mail-smtp-apply /usr/local/bin/mail-smtp-policy /usr/local/bin/mail-smtp-cert
rm -f /usr/local/bin/mail-smtp-acme-http
rm -f /etc/fail2ban/jail.d/mail-smtp.conf /etc/fail2ban/filter.d/mail-smtp-sasl.conf
rm -f /etc/dovecot/conf.d/99-mail-smtp-auth.conf
rm -f /etc/letsencrypt/renewal-hooks/deploy/mail-smtp.sh
systemctl disable --now mail-smtp-acme-http.service 2>/dev/null || true
rm -f /etc/systemd/system/mail-smtp-acme-http.service
systemctl daemon-reload 2>/dev/null || true

if [[ -f /etc/postfix/master.cf.mail-smtp.bak ]]; then
  cp -a /etc/postfix/master.cf.mail-smtp.bak /etc/postfix/master.cf
  echo "uninstall: restored /etc/postfix/master.cf from backup"
fi

if [[ "${purge_packages}" -eq 1 ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get remove -y --purge postfix opendkim opendkim-tools dovecot-core || true
  rm -rf /etc/mail-smtp /var/lib/mail-smtp
  echo "uninstall: packages and /etc/mail-smtp removed"
else
  echo "uninstall: scripts removed; left Postfix/Dovecot packages and /etc/mail-smtp"
  echo "uninstall: re-run with --purge-packages to remove packages and config data"
fi

systemctl restart fail2ban 2>/dev/null || true
