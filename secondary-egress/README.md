# Secondary egress

Outbound HTTP(S) via the second server's IP. Containers on `www` always use the same proxy URL:

```
http://secondary-egress:8118
```

## Modes

| Mode | Who initiates SSH | `secondary-egress:8118` |
|------|-------------------|---------------------------|
| **outbound** (`SECOND_IP_EGRESS_MODE=outbound`) | Docker host → second server | Privoxy + SOCKS inside the container |
| **inbound** (`SECOND_IP_EGRESS_MODE=inbound`) | Second server → Docker host | HAProxy forwards to Privoxy on the second server via reverse tunnel |

---

## Outbound mode

The `secondary-egress` container SSHs to `SECOND_IP_SSH_HOST`, opens SOCKS tunnels, and runs Privoxy.

### Docker host `.env`

```env
SECOND_IP_EGRESS_MODE=outbound
SECOND_IP_SSH_HOST=
SECOND_IP_SSH_PORT=22
SECOND_IP_SSH_USER=
SECOND_IP_SSH_KEY_PATH=
SECOND_IP_REVERSE_PORTS=
```

### Additional proxies

| URL | Protocol | Use |
|-----|----------|-----|
| `socks5://secondary-egress:1080` | SOCKS5 | Non-HTTP tools (e.g. FTP via `curl --proxy`) |

`8118` is Privoxy (HTTP only). Do not use it for FTP.

### Reverse forwards (`SECOND_IP_REVERSE_PORTS`)

Publish ports on the second server back to services on the Docker host. Requires the outbound SSH tunnel and `GatewayPorts clientspecified` on the second server's `sshd`.

Example: `8082:haproxy:80` → `SECOND_IP_SSH_HOST:8082` reaches HAProxy on the Docker host.

---

## Inbound mode

Use when the Docker host **cannot** TCP-connect to the second server, but the second server **can** SSH to the Docker host.

Sites still use `http://secondary-egress:8118`. The container waits for the reverse tunnel, then forwards port `8118` to Privoxy on the second server (published on the Docker host at port `18118`).

### 1. Docker host `.env`

```env
SECOND_IP_EGRESS_MODE=inbound
SECOND_IP_INBOUND_PROXY_PORT=18118
```

`SECOND_IP_SSH_*` and `SECOND_IP_SSH_KEY_PATH` are still required by compose but unused in inbound mode.

`SECOND_IP_REVERSE_PORTS` in `.env` is **not used in inbound mode**. Set the equivalent list as `TUNNEL_REVERSE_PORTS` in `/etc/reverse-tunnel.env` on the second server (see `second-server/README.md`). Use `127.0.0.1` or a host IP for targets — Docker container names are not resolved by the Docker host `sshd`.

### 2. Docker host `sshd`

```ini
GatewayPorts clientspecified
```

```bash
sudo systemctl restart ssh
```

Add the second server's SSH public key to `authorized_keys` on the Docker host.

### 3. Second server

Copy `second-server/` anywhere on the second server, then run `install.sh` — see `second-server/README.md`.

### 4. Start on Docker host

```bash
docker compose up -d --build secondary-egress
```

Flow:

```
site container → secondary-egress:8118 (HAProxy)
              → host.docker.internal:18118 (reverse tunnel)
              → Privoxy on second server :8118
              → internet (second server IP)
```

### Backup FTP (inbound)

```env
BACKUP_FTP_SERVER=host.docker.internal:8021
BACKUP_FTP_PROXY=
```

### Reverse port forwards (inbound)

Publish a port on the second server that reaches a service on the Docker host. Add to `/etc/reverse-tunnel.env`:

```env
TUNNEL_REVERSE_PORTS=8082:127.0.0.1:80
```

This example exposes `:8082` on the second server and forwards to HAProxy on the Docker host. Restart after changes:

```bash
sudo systemctl restart reverse-tunnel
```

---

## Troubleshooting

### Cannot SSH from Docker host to second server

Use inbound mode (`SECOND_IP_EGRESS_MODE=inbound`) and set up `second-server/` on the second machine.

### Inbound: `secondary-egress` waits for upstream

Ensure `reverse-tunnel` is running on the second server and Privoxy is listening:

```bash
nc -zv 127.0.0.1 18118   # on Docker host
curl -x http://127.0.0.1:18118 -I https://example.com
```

### Outbound: `secondary-egress` restart loop

Failed SOCKS setup restarts the container and may trigger fail2ban. Whitelist the Docker host IP or use inbound mode.

### FTP upload fails with HTTP proxy

Use `socks5://secondary-egress:1080` (outbound) or inbound FTP via `host.docker.internal:8021` with `BACKUP_FTP_PROXY` empty.
