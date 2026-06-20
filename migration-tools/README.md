# Migration tools

Standalone helpers for moving WordPress sites (files + database) between servers. Not wired into the Docker stack; copy scripts to the source or destination host as needed.

## Guides

| Guide | When to use |
|-------|-------------|
| [WordPress database export / import](wordpress-database.md) | One-off full DB dump and restore via `docker exec` + PHP scripts |
| [File transfer via jump host](file-transfer-via-jump-host.md) | Source and destination **cannot** reach each other; traffic goes through a middle server (`ProxyCommand`) |
| [Direct file transfer](file-transfer-direct.md) | Source and destination are reachable over SSH (`rsync`, `scp`) |
| [Database via Docker stack](docker-database.md) | Both hosts run this project; `mariadb-dump` through `docker compose` |

## Typical migration checklist

1. [Export database](wordpress-database.md) on the source (PHP script or `mariadb-dump`).
2. Transfer files — [via jump host](file-transfer-via-jump-host.md) or [direct](file-transfer-direct.md).
3. [Import database](wordpress-database.md) on the destination.
4. Update `wp-config.php`, `haproxy.cfg`, and URL constants (`WP_HOME`, `WP_SITEURL`) for the new domain.
5. `docker compose up -d` and `docker compose restart haproxy`.
6. Remove migration scripts and dump/archive files from all servers involved.

## Files in this folder

| File | Purpose |
|------|---------|
| `wp-export-db.php` | Export DB to a randomly named `.sql.zip` (CLI only) |
| `wp-import-db.php` | Import from a zip produced by `wp-export-db.php` (CLI only) |
