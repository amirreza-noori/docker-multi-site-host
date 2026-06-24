# Containerized WordPress Server

Multi-site WordPress on one server: shared MariaDB, HAProxy with automatic SSL, Portainer, phpMyAdmin, and scheduled backups.

## Overview

Each folder under `SITES_DIR` has one `haproxy.cfg` (frontend + backend, split by `## backend ##`) and optionally `docker-compose.yml`.

**Docker networks:** `database` — production MariaDB, WordPress sites, backup-runner, phpMyAdmin. `database-monitor` — optional extra network on phpMyAdmin only; attach external DBs here for inspection (never add external MariaDB to `database`).

## First-time setup

1. Fill `.env`, then `docker compose up -d --build`
2. In `docker-compose.sites.yml`, uncomment the `_admintools` include block, then `docker compose up -d`
3. Edit `$SITES_DIR/_admintools/haproxy.cfg` (hostnames above `## backend ##`)
4. `docker compose restart haproxy`

## Add a WordPress site

1. `cp -r "$SITES_DIR/_template_wordpress" "$SITES_DIR/my-site"`
2. Edit `docker-compose.yml` — **rename the service key** from `example-com` to `my-site` (must match `container_name`; each site needs a unique service name)
3. Set `build.context` to the repo `wordpress/` directory (absolute path when `SITES_DIR` is outside the repo)
4. For PHP 7.4: uncomment `build.args.PHP_VERSION` and the alternate `image` line in `docker-compose.yml`
5. Edit `wp-config.php`, `haproxy.cfg`, `database.sql`
6. Add to `backup/sites.conf` and `docker-compose.sites.yml`
7. `docker compose up -d`
8. `docker compose restart haproxy`

## Add a static site

1. `cp -r "$SITES_DIR/_template_static" "$SITES_DIR/my-site"`
2. Edit `docker-compose.yml` — rename service key and `container_name` from `example-com` to `my-site`
3. Edit `haproxy.cfg` (domain) and replace files in `public/`
4. Add to `backup/sites.conf` (files only, no database) and `docker-compose.sites.yml`
5. `docker compose up -d`
6. `docker compose restart haproxy`

Example backup line: `my-site|-|public||0|7`

## haproxy.cfg format (per site)

```cfg
    acl host_example hdr(host) -i example.com
    use_backend example_backend if host_example

## backend ##

backend example_backend
    mode http
    server example-com example-com:80 init-addr none
```

## Common commands

```bash
docker compose up -d --build
docker compose restart haproxy
docker compose restart backup-runner
```
