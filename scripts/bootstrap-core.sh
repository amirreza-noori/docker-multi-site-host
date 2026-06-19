#!/bin/sh
# Creates host directories and default config files when missing.
set -e
set -u

REPO="${REPO:?REPO is required}"
SITES_DIR="${SITES_DIR:?SITES_DIR is required}"
MARIADB_DATA_DIR="${MARIADB_DATA_DIR:?MARIADB_DATA_DIR is required}"
LETSENCRYPT_DIR="${LETSENCRYPT_DIR:?LETSENCRYPT_DIR is required}"
BACKUP_DIR="${BACKUP_DIR:?BACKUP_DIR is required}"

mkdir -p "$MARIADB_DATA_DIR" "$LETSENCRYPT_DIR" "$BACKUP_DIR"

install_site_template() {
  src_name=$1
  dst="$SITES_DIR/$src_name"
  if [ ! -f "$dst/docker-compose.yml" ]; then
    mkdir -p "$SITES_DIR"
    cp -a "$REPO/sites/$src_name/." "$dst/"
    echo "Installed $src_name at $dst"
  fi
}

install_site_template "_admintools"
install_site_template "_template_wordpress"
install_site_template "_template_static"

if [ ! -f "$SITES_DIR/_template_wordpress/wp-config.php" ] && [ -f "$SITES_DIR/_template_wordpress/wp-config.php.example" ]; then
  cp "$SITES_DIR/_template_wordpress/wp-config.php.example" "$SITES_DIR/_template_wordpress/wp-config.php"
  echo "Created $SITES_DIR/_template_wordpress/wp-config.php from example"
fi

if [ ! -f "$REPO/docker-compose.sites.yml" ] && [ -f "$REPO/docker-compose.sites.yml.example" ]; then
  cp "$REPO/docker-compose.sites.yml.example" "$REPO/docker-compose.sites.yml"
  echo "Created $REPO/docker-compose.sites.yml from example"
fi

if [ ! -f "$REPO/backup/sites.conf" ] && [ -f "$REPO/backup/sites.conf.example" ]; then
  cp "$REPO/backup/sites.conf.example" "$REPO/backup/sites.conf"
  echo "Created $REPO/backup/sites.conf from example"
fi

echo "Bootstrap complete."
