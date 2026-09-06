#!/bin/bash
set -euo pipefail

WP_PATH="/app"
WP_CONFIG="${WP_PATH}/wp-config.php"
WP_CONTENT="${WP_PATH}/wp-content"
CONTAINER_NAME="${CONTAINER_NAME:-wordpress}"
WP_DB_HOST_DEFAULT="${WORDPRESS_DB_HOST:-mariadb:3306}"

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
  chmod 644 "${WP_CONFIG}"
fi

fix_mount_file() {
  f=$1
  if [ -f "$f" ]; then
    chown application:application "$f"
    chmod 644 "$f"
  fi
}

# Host bind-mount for full wp-content (themes, plugins, uploads, W3TC, languages)
mkdir -p \
  "${WP_CONTENT}/themes" \
  "${WP_CONTENT}/plugins" \
  "${WP_CONTENT}/uploads" \
  "${WP_CONTENT}/languages"

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

chown -R application:application "${WP_CONTENT}" || true
fix_mount_file "$OC"
fix_mount_file "${WP_PATH}/.htaccess"
fix_mount_file "${WP_CONFIG}"

if [ "$#" -eq 0 ]; then
  set -- supervisord
fi

exec /entrypoint "$@"
