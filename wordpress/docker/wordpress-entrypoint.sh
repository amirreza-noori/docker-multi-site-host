#!/bin/bash
set -euo pipefail

WP_PATH="/app"
WP_CONFIG="${WP_PATH}/wp-config.php"
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
fi

if [ "$#" -eq 0 ]; then
  set -- supervisord
fi

exec /entrypoint "$@"
