#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
  echo "Missing .env — copy from .env.example and set all required paths." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

require_var() {
  if [ -z "${!1:-}" ]; then
    echo "Required variable $1 is not set in .env" >&2
    exit 1
  fi
}

for var in SITES_DIR MARIADB_DATA_DIR LETSENCRYPT_DIR BACKUP_DIR MARIADB_ROOT_PASSWORD; do
  require_var "$var"
done

export REPO="$ROOT"
export SITES_DIR MARIADB_DATA_DIR LETSENCRYPT_DIR BACKUP_DIR

exec sh "$ROOT/scripts/bootstrap-core.sh"
