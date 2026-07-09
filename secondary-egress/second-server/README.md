# Second server (inbound mode)

Scripts and config for the **second server** when it must initiate SSH to the Docker host. See `../README.md` for the full inbound/outbound overview.

Copy this folder anywhere on the second server (no fixed install path). `install.sh` copies files to system paths; `systemd` runs `/usr/local/bin/reverse-tunnel.sh`, not your copy folder. **Re-run `install.sh` after every update** to the files you copied.

## What install.sh installs

| Source (this folder) | Installed path |
|----------------------|----------------|
| `reverse-tunnel.sh` | `/usr/local/bin/reverse-tunnel.sh` |
| `reverse-tunnel.env` or `reverse-tunnel.example.env` | `/etc/reverse-tunnel.env` |
| `reverse-tunnel.service.example` | `/etc/systemd/system/reverse-tunnel.service` |
| `privoxy.example.config` (`--privoxy`) | `/etc/privoxy/config` |

Use `--update-env` to replace an existing `/etc/reverse-tunnel.env`.

## SSH ports

| Connection | Config file | Variable |
|------------|-------------|----------|
| Docker host → second server (outbound) | Docker host `.env` | `SECOND_IP_SSH_PORT` |
| Second server → Docker host (inbound tunnel) | `/etc/reverse-tunnel.env` | `TUNNEL_SSH_PORT` |

Use each machine's real `sshd` port — example files use `22` as a placeholder.

## Setup

### 1. Copy files to the second server

```bash
scp -P 22 -r secondary-egress/second-server root@SECOND_SERVER:/opt/second-server
```

Optional: keep a local `reverse-tunnel.env` in that folder with your values (gitignored in the repo).

### 2. SSH key to the Docker host

On the second server:

```bash
ssh-keygen -t ed25519 -f /root/.ssh/docker-host-tunnel -N ""
cat /root/.ssh/docker-host-tunnel.pub
```

Add the public key to `authorized_keys` on the Docker host. On the Docker host, set `GatewayPorts clientspecified` in `sshd_config` and restart `ssh`.

Test (use your Docker host address and port):

```bash
ssh -i /root/.ssh/docker-host-tunnel -p 22 root@203.0.113.10 true && echo SSH_OK
```

### 3. Install and configure

On the second server:

```bash
sudo apt install -y autossh
sudo bash /opt/second-server/install.sh --privoxy --tunnel-only --update-env
sudo nano /etc/reverse-tunnel.env
sudo systemctl enable --now reverse-tunnel
```

If Privoxy is already configured, omit `--privoxy`.

`install.sh` prints the installed script line count. After updates, run install again before restarting the service.

Example `/etc/reverse-tunnel.env`:

```env
TUNNEL_SSH_HOST=203.0.113.10
TUNNEL_SSH_PORT=22
TUNNEL_SSH_USER=root
TUNNEL_SSH_KEY=/root/.ssh/docker-host-tunnel
FTP_REMOTE_PORT=8021
FTP_LOCAL_PORT=21
FTP_PASV_PORT_START=40000
FTP_PASV_PORT_END=40000
PROXY_REMOTE_PORT=18118
PROXY_LOCAL_PORT=8118
TUNNEL_REVERSE_PORTS=8082:127.0.0.1:80
```

`TUNNEL_REVERSE_PORTS` uses the same syntax as `SECOND_IP_REVERSE_PORTS` in the Docker host `.env`. Targets must be reachable from the Docker host (`127.0.0.1`, not Docker service names).

### 4. Docker host

Set `SECOND_IP_EGRESS_MODE=inbound` in `.env`, then:

```bash
docker compose up -d --build secondary-egress
```

## Published ports

| On second server | On Docker host | Used by |
|------------------|----------------|---------|
| FTP `127.0.0.1:21` | `:8021` | `backup-runner` (`BACKUP_FTP_SERVER=host.docker.internal:8021`) |
| FTP passive `127.0.0.1:40000` | `:40000` | FTP PASV (match `pasv_min/max_port` in vsftpd) |
| Privoxy `127.0.0.1:8118` | `:18118` | `secondary-egress` inbound proxy |
| `:8082` (example) | forwarded via tunnel | `TUNNEL_REVERSE_PORTS=8082:127.0.0.1:80` |

## Verify

On the Docker host:

```bash
nc -zv 127.0.0.1 18118
nc -zv 127.0.0.1 8021
nc -zv 127.0.0.1 40000
curl -x http://127.0.0.1:18118 -I https://example.com
```

On the second server:

```bash
sudo systemctl status privoxy reverse-tunnel
sudo journalctl -u reverse-tunnel -n 30 --no-pager
```

Expected log line after restart:

```
reverse-tunnel: publish FTP passive :40000-40000 on Docker host
```

## Troubleshooting

### Scripts fail with `pipefail: invalid option` or `status=203/EXEC`

Line endings are CRLF (common when copying from Windows). On the second server:

```bash
sed -i 's/\r$//' /opt/second-server/*.sh
sudo bash /opt/second-server/install.sh --tunnel-only
sudo systemctl daemon-reload
sudo systemctl restart reverse-tunnel
```

### `status=127` on `reverse-tunnel`

Install `autossh`, re-run `install.sh`, and restart the service:

```bash
sudo apt install -y autossh
sudo bash /opt/second-server/install.sh --tunnel-only
sudo systemctl restart reverse-tunnel
```

### Tunnel starts but passive FTP or proxy ports missing on Docker host

Confirm `/usr/local/bin/reverse-tunnel.sh` matches your copy folder, then re-run install:

```bash
wc -l /opt/second-server/reverse-tunnel.sh /usr/local/bin/reverse-tunnel.sh
sudo bash /opt/second-server/install.sh --tunnel-only --update-env
sudo systemctl restart reverse-tunnel
```

### `8082: Address already in use` when testing manually

Stop the service before a manual run — only one process can bind the forwarded ports:

```bash
sudo systemctl stop reverse-tunnel
sudo /bin/bash /usr/local/bin/reverse-tunnel.sh
```
