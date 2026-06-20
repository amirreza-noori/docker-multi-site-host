# File transfer via jump host

When the source and destination servers **cannot** see each other, route `scp` through a middle host with `ProxyCommand`.

Pattern for each archive:

1. On **source** — `tar` + `gzip` the folder.
2. `scp` to **destination** via the jump host (`-o ProxyCommand=...`).
3. On **destination** — `tar -xzf` to unpack.

## Hosts (replace with yours)

| Role | Symbolic name | SSH port | Notes |
|------|---------------|----------|-------|
| Jump host | `jump-host` | `9011` | Used inside `ProxyCommand` |
| Destination | `new-server` | `6579` | `-P` on `scp`; unpack commands run here |

Add both to `~/.ssh/config` or `/etc/hosts`, or substitute your real hostnames/IPs when running the commands.

Paths below use `my-site` as the site folder name — change to match your layout.

## Theme (`my-theme`)

```bash
cd /php-sites/my-site/root/wp-content/themes/ && \
tar -c my-theme | gzip -9 > my-theme.tar.gz

scp -r -o ProxyCommand='ssh -W %h:%p -p 9011 root@jump-host' \
    -P 6579 \
    /php-sites/my-site/root/wp-content/themes/my-theme.tar.gz \
    root@new-server:/sites/my-site/themes/

cd /sites/my-site/themes/ && \
tar -xzf my-theme.tar.gz
```

## Plugins

```bash
cd /php-sites/my-site/root/wp-content/ && \
tar -c plugins | gzip -9 > plugins.tar.gz

scp -r -o ProxyCommand='ssh -W %h:%p -p 9011 root@jump-host' \
    -P 6579 \
    /php-sites/my-site/root/wp-content/plugins.tar.gz \
    root@new-server:/sites/my-site/

cd /sites/my-site/ && \
tar -xzf plugins.tar.gz
```

## Uploads

```bash
cd /php-sites/my-site/root/wp-content/ && \
tar -c uploads | gzip -9 > /php-sites/my-site/uploads.tar.gz

scp -r -o ProxyCommand='ssh -W %h:%p -p 9011 root@jump-host' \
    -P 6579 \
    /php-sites/my-site/uploads.tar.gz \
    root@new-server:/sites/my-site/

cd /sites/my-site/ && \
tar -xzf uploads.tar.gz
```

## Notes

- **Jump host:** `root@jump-host` on port `9011` (`ProxyCommand`).
- **Destination:** `root@new-server` on port `6579` (`-P` on `scp`).
- Unpack commands run on the **destination**; pack commands run on the **source**.
- Remove `.tar.gz` archives from both servers after verifying the transfer.

[← Back to index](README.md)
