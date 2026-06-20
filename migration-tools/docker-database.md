# Database via Docker stack

When both hosts run this project and MariaDB is reachable from the backup container.

```bash
# Export on source
docker compose exec mariadb mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" example_com_wp > example_com_wp.sql
zip example_com_wp.sql.zip example_com_wp.sql

# Copy zip to destination, then import
scp example_com_wp.sql.zip user@dest:/tmp/
ssh user@dest 'cd /path/to/project && unzip -p /tmp/example_com_wp.sql.zip | docker compose exec -T mariadb mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" example_com_wp'
```

If source and destination cannot see each other, copy the zip [via jump host](file-transfer-via-jump-host.md) instead of direct `scp`.

[← Back to index](README.md)
