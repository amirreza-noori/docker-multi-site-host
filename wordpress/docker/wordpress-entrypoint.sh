#!/bin/bash
set -euo pipefail

WP_PATH="/app"
WP_CONFIG="${WP_PATH}/wp-config.php"
WP_CONTENT="${WP_PATH}/wp-content"
CONTAINER_NAME="${CONTAINER_NAME:-wordpress}"
WP_DB_HOST_DEFAULT="${WORDPRESS_DB_HOST:-mariadb:3306}"

UPLOADS_HTACCESS_BODY='# Deny script execution under uploads (webshells)
<FilesMatch "\.ph(p[0-9]?|tml|ar|ps)$">
	<IfModule mod_authz_core.c>
		Require all denied
	</IfModule>
	<IfModule !mod_authz_core.c>
		Order deny,allow
		Deny from all
	</IfModule>
</FilesMatch>
Options -ExecCGI
RemoveHandler .php .phtml .php3 .php4 .php5 .php7 .php8 .phar
RemoveType .php .phtml .php3 .php4 .php5 .php7 .php8 .phar
'

if [ ! -f "${WP_CONFIG}" ] && [ -f "${WP_PATH}/wp-config-sample.php" ]; then
  cp "${WP_PATH}/wp-config-sample.php" "${WP_CONFIG}"

  sed -i "s/database_name_here/${WORDPRESS_DB_NAME:-wordpress}/" "${WP_CONFIG}"
  sed -i "s/username_here/${WORDPRESS_DB_USER:-wordpress}/" "${WP_CONFIG}"
  sed -i "s/password_here/${WORDPRESS_DB_PASSWORD:-wordpress}/" "${WP_CONFIG}"
  sed -i "s/localhost/${WORDPRESS_DB_HOST:-${WP_DB_HOST_DEFAULT}}/" "${WP_CONFIG}"

  if [ -n "${WORDPRESS_CONFIG_EXTRA:-}" ]; then
    awk -v extra="${WORDPRESS_CONFIG_EXTRA}" '
      /\/\* That.s all, stop editing! Happy publishing. \*\// {
        print extra
      }
      { print }
    ' "${WP_CONFIG}" > "${WP_CONFIG}.tmp"
    mv "${WP_CONFIG}.tmp" "${WP_CONFIG}"
  fi

  chown application:application "${WP_CONFIG}"
  chmod 444 "${WP_CONFIG}"
fi

# Bind-mounted config must be readable by Apache/PHP (writes blocked via :ro mounts)
ensure_readable_mount() {
  f=$1
  if [ -f "$f" ]; then
    chown application:application "$f" 2>/dev/null || true
    chmod 644 "$f" 2>/dev/null || true
  fi
}

# Host bind-mount for full wp-content (themes, plugins, uploads, W3TC, languages)
mkdir -p \
  "${WP_CONTENT}/themes" \
  "${WP_CONTENT}/plugins" \
  "${WP_CONTENT}/uploads" \
  "${WP_CONTENT}/languages" \
  "${WP_CONTENT}/cache" \
  "${WP_CONTENT}/upgrade"

# Locale pack from image when the mounted languages dir is still empty
if [ -d /opt/wordpress-languages ] \
  && [ -n "$(ls -A /opt/wordpress-languages 2>/dev/null || true)" ] \
  && [ -z "$(ls -A "${WP_CONTENT}/languages" 2>/dev/null || true)" ]; then
  cp -a /opt/wordpress-languages/. "${WP_CONTENT}/languages/"
fi

OC="${WP_CONTENT}/object-cache.php"
if [ -d "$OC" ]; then
  echo "ERROR: $OC is a directory — remove it so a cache plugin can install object-cache.php as a file" >&2
  exit 1
fi

# uploads is bind-mounted — enforce at every start (Dockerfile cannot write the host mount).
# 1) remove nested/infected .htaccess + any PHP/scripts under uploads (webshells)
# 2) install a known-good root .htaccess owned by root (Apache also denies PHP here)
UPLOADS_HTACCESS="${WP_CONTENT}/uploads/.htaccess"
find "${WP_CONTENT}/uploads" -type f -name '.htaccess' -delete 2>/dev/null || true
find "${WP_CONTENT}/uploads" -type f \( \
  -iname '*.php' -o -iname '*.phtml' -o -iname '*.phar' \
  -o -iname '*.php3' -o -iname '*.php4' -o -iname '*.php5' -o -iname '*.php7' -o -iname '*.php8' \
\) -delete 2>/dev/null || true
printf '%s\n' "${UPLOADS_HTACCESS_BODY}" > "${UPLOADS_HTACCESS}"

# PHP must be able to create a file in wp-content or WordPress selects FTP and admin calls 500.
# Plugins and themes stay root-owned so wp-admin cannot change code.
chown application:application "${WP_CONTENT}" || true
chmod 755 "${WP_CONTENT}" || true
for path in "${WP_CONTENT}"/*; do
  [ -e "${path}" ] || continue
  base="$(basename "${path}")"
  case "${base}" in
    uploads|cache|w3tc-config) continue ;;
    advanced-cache.php|object-cache.php|db.php|sunrise.php) continue ;;
  esac
  chown -R root:root "${path}" || true
  chmod -R u=rwX,go=rX "${path}" || true
done

# Media library, disk cache files, and W3TC config (master.php)
for d in uploads cache w3tc-config; do
  target="${WP_CONTENT}/${d}"
  mkdir -p "${target}"
  chown -R application:application "${target}" || true
  chmod -R u=rwX,g=rwX,o=rX "${target}" || true
done
for f in advanced-cache.php object-cache.php db.php sunrise.php; do
  if [ -f "${WP_CONTENT}/${f}" ]; then
    chown application:application "${WP_CONTENT}/${f}" || true
    chmod 644 "${WP_CONTENT}/${f}" || true
  fi
done
# Sticky on uploads: PHP can replace its own files, not the root-owned script block
if [ -f "${UPLOADS_HTACCESS}" ]; then
  chown root:root "${UPLOADS_HTACCESS}" || true
  chmod 644 "${UPLOADS_HTACCESS}" || true
fi
chmod 1775 "${WP_CONTENT}/uploads" || true

ensure_readable_mount "${WP_CONFIG}"
ensure_readable_mount "${WP_PATH}/.htaccess"

if [ "$#" -eq 0 ]; then
  set -- supervisord
fi

exec /entrypoint "$@"
