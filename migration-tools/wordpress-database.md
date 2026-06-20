# WordPress database export / import

CLI only — run inside the site container with `docker exec`. No browser URLs.

Use your site values from `docker-compose.yml` (`container_name`) and `$SITES_DIR/<site-folder>/` on the host:

```bash
CONTAINER_NAME=my-site          # container_name in the site compose file
SITE_DIR=/sites/my-site         # or $SITES_DIR/my-site
```

Copy migration scripts and zip dumps into `/app/` inside the container (WordPress root, next to `wp-config.php`):

```bash
docker cp migration-tools/wp-export-db.php "$CONTAINER_NAME":/app/
docker cp migration-tools/wp-import-db.php "$CONTAINER_NAME":/app/
docker cp "$SITE_DIR"/db_migrate_<random>.sql.zip "$CONTAINER_NAME":/app/
```

On this stack, `/app/` exists only inside the container — not as a folder on the host.

## Source server — export

1. `docker compose up -d` (from the project root).
2. Copy `wp-export-db.php` to `/app/`.
3. Export:

```bash
docker exec "$CONTAINER_NAME" php /app/wp-export-db.php
```

4. Copy the zip to the host:

```bash
docker cp "$CONTAINER_NAME":/app/db_migrate_<random>.sql.zip "$SITE_DIR"/
```

5. Clean up:

```bash
docker exec "$CONTAINER_NAME" rm -rf /app/wp-export-db.php /app/db_migrate_*.sql*
```

For large shops, raise PHP and container memory if needed:

```bash
docker update --memory 3g --memory-swap 3g "$CONTAINER_NAME"
docker exec "$CONTAINER_NAME" php -d memory_limit=2G -d max_execution_time=0 /app/wp-export-db.php
docker update --memory 1g --memory-swap 1g "$CONTAINER_NAME"
```

Alternative via the shared MariaDB container:

```bash
docker compose exec mariadb mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" your_db_name > dump.sql
zip dump.sql.zip dump.sql
```

---

## Destination server — import

**Before import:** edit `$SITE_DIR/wp-config.php`. `DB_HOST` must be the shared MariaDB service:

```php
define( 'DB_HOST', 'mariadb:3306' );
```

`DB_NAME`, `DB_USER`, and `DB_PASSWORD` must exist on the new MariaDB (create with `database.sql` if needed). Set `WP_HOME` / `WP_SITEURL` to the **new** domain.

1. Put the zip on the host (`$SITE_DIR/`).
2. `docker compose up -d`
3. Copy zip and `wp-import-db.php` to `/app/`.
4. Import:

```bash
docker cp "$SITE_DIR"/db_migrate_<random>.sql.zip "$CONTAINER_NAME":/app/
docker cp migration-tools/wp-import-db.php "$CONTAINER_NAME":/app/

docker exec "$CONTAINER_NAME" php /app/wp-import-db.php db_migrate_<random>.sql.zip
```

The script unpacks into `.wp-migrate-<random>/` next to the zip, imports, then deletes that folder.

5. Clean up:

```bash
docker exec "$CONTAINER_NAME" rm -rf /app/wp-import-db.php /app/db_migrate_*.sql* /app/.wp-migrate-*
rm -f "$SITE_DIR"/wp-import-db.php "$SITE_DIR"/db_migrate_*.sql*
```

**Alternative — pipe into MariaDB** (best for very large dumps):

```bash
unzip -p "$SITE_DIR"/db_migrate_<random>.sql.zip | \
  docker exec -i mariadb mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" your_db_name
```

**`MySQL server has gone away`:** dump is too large for PHP `mysqli` streaming. Use the MariaDB container directly (fastest, no image rebuild):

```bash
DB_NAME=$(grep DB_NAME "$SITE_DIR/wp-config.php" | sed -E "s/.*['\"]([^'\"]+)['\"].*/\1/")

unzip -p "$SITE_DIR"/db_migrate_<random>.sql.zip | \
  docker exec -i mariadb mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" "$DB_NAME"
```

After rebuilding the WordPress image (`mariadb-client` is included), `wp-import-db.php` uses the client automatically and the command above is only a fallback.

If it still fails, restart MariaDB so `max_allowed_packet` from `mariadb/conf.d/99-server.cnf` applies:

```bash
docker compose restart mariadb
```

**Warning:** Import replaces all tables in the database configured in `wp-config.php`. Take a backup first.

[← Back to index](README.md)
