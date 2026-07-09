# Backup runner

Scheduled backups run inside the `backup-runner` container (`backup.sh` every 10 minutes). Archives are stored locally under `BACKUP_DIR` and can be uploaded to a remote FTP server.

## Local layout

```
BACKUP_DIR/
  my-site/
    db_backup_YYYYMMDD_HHMM.sql.zip
    files_backup_YYYYMMDD_HHMM.zip
```

When FTP is enabled, the same per-site tree is mirrored on the remote server.

## Docker host configuration

Set these in the root `.env`. Examples depend on egress mode — see `secondary-egress/README.md`.

**Outbound** (FTP via SOCKS on the second server):

```env
BACKUP_FTP_SERVER=127.0.0.1:21
BACKUP_FTP_USER=backupftp
BACKUP_FTP_PASS=...
BACKUP_FTP_PROXY=socks5://secondary-egress:1080
```

**Inbound** (FTP via reverse tunnel to the second server):

```env
BACKUP_FTP_SERVER=host.docker.internal:8021
BACKUP_FTP_USER=backupftp
BACKUP_FTP_PASS=...
BACKUP_FTP_PROXY=
```

| Variable | Role |
|----------|------|
| `BACKUP_FTP_SERVER` | FTP host, port, and optional base path after `:21/` (e.g. `127.0.0.1:21` or `127.0.0.1:21/data`) |
| `BACKUP_FTP_USER` / `BACKUP_FTP_PASS` | FTP credentials |
| `BACKUP_FTP_PROXY` | Optional `curl --proxy` URL. See `secondary-egress/README.md` for outbound vs inbound tunnel modes |

Leave `BACKUP_FTP_SERVER` empty to keep backups local only. For proxy/tunnel setup see `secondary-egress/README.md`.

After changing `.env`:

```bash
docker compose up -d --build backup-runner
docker compose restart backup-runner
```

Successful upload log line:

```
UPLOAD my-site/db_backup_20260709_0410.sql.zip 226
```

`226` means the transfer completed.

For FTP over SSH (outbound or inbound tunnel), see `secondary-egress/README.md`.

---

## FTP server setup (Ubuntu + vsftpd)

Run on the **backup server** (often the same machine as `SECOND_IP_SSH_HOST`).

### 1. Install vsftpd

```bash
sudo apt update
sudo apt install -y vsftpd
```

### 2. Create the FTP user

Pick a home directory for chroot (example: `/backup/remote`):

```bash
sudo mkdir -p /backup/remote
sudo useradd -m -d /backup/remote -s /usr/sbin/nologin backupftp
sudo passwd backupftp
```

To change an existing user's home later:

```bash
sudo usermod -d /backup/remote backupftp
```

Confirm:

```bash
grep backupftp /etc/passwd
```

### 3. Allow `nologin` shell

vsftpd only accepts shells listed in `/etc/shells`:

```bash
grep pam_shells /etc/pam.d/vsftpd
echo "/usr/sbin/nologin" | sudo tee -a /etc/shells
```

### 4. User allowlist

```bash
echo "backupftp" | sudo tee /etc/vsftpd.userlist
```

### 5. vsftpd configuration

```bash
sudo cp /etc/vsftpd.conf /etc/vsftpd.conf.bak
sudo nano /etc/vsftpd.conf
```

Recommended settings (localhost only):

```ini
listen=YES
listen_ipv6=NO
listen_address=127.0.0.1
anonymous_enable=NO
local_enable=YES
write_enable=YES
chroot_local_user=YES
allow_writeable_chroot=YES
local_umask=022
pasv_enable=YES
pasv_address=127.0.0.1
# One passive port; must match FTP_PASV_PORT_* in /etc/reverse-tunnel.env when FTP is tunneled
pasv_min_port=40000
pasv_max_port=40000
userlist_enable=YES
userlist_file=/etc/vsftpd.userlist
userlist_deny=NO
```

Do **not** open port 21 on the public firewall — access is via the SSH tunnel only.

When using inbound egress, set the same passive port range in `/etc/reverse-tunnel.env` on the second server (`FTP_PASV_PORT_START` / `FTP_PASV_PORT_END`).

```bash
sudo systemctl restart vsftpd
sudo systemctl enable vsftpd
```

### 6. Directory permissions

With `allow_writeable_chroot=YES`, the chroot root must be writable by `backupftp`:

```bash
sudo chown backupftp:backupftp /backup/remote
sudo chmod 755 /backup/remote
```

**Stricter alternative** (drop `allow_writeable_chroot=YES`, keep chroot root owned by root):

```bash
sudo chown root:root /backup/remote
sudo chmod 755 /backup/remote
sudo mkdir -p /backup/remote/data
sudo chown backupftp:backupftp /backup/remote/data
```

Then set `BACKUP_FTP_SERVER=127.0.0.1:21/data` in `.env`.

### 7. Map `.env` to the remote path

| FTP user home (`grep backupftp /etc/passwd`) | `BACKUP_FTP_SERVER` | File on disk |
|----------------------------------------------|---------------------|--------------|
| `/backup/remote` | `127.0.0.1:21` | `/backup/remote/my-site/db_backup_....zip` |
| `/backup/remote` | `127.0.0.1:21/data` | `/backup/remote/data/my-site/db_backup_....zip` |

The path after `:21/` is relative to the FTP user's chroot root.

---

## Testing

### On the backup server (local FTP)

```bash
sudo apt install -y ftp
ftp -p 127.0.0.1
# login as backupftp, then:
put /etc/hostname test.txt
```

### From the Docker host

```bash
docker exec backup-runner /bin/bash /usr/local/bin/backup.sh
docker logs backup-runner --tail 30
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `UPLOAD ... 530` | Login rejected | Check user/password, `/etc/vsftpd.userlist`, `/etc/shells` |
| `UPLOAD ... 227` | PASV data channel failed | Match `pasv_min/max_port` with `FTP_PASV_PORT_*`; restart `reverse-tunnel`; on Docker host confirm port `40000` is listening |
| `UPLOAD ... 000` | No FTP response | vsftpd not running, wrong proxy, or tunnel down |
| `access denied` at login | User not in userlist or `nologin` not in `/etc/shells` | Steps 3–4 above |
| `Could not create file` | Target directory missing or not writable | Step 6; create base path and fix ownership |
| HTTP proxy used for FTP | Privoxy cannot proxy FTP | Use `socks5://secondary-egress:1080` |

Server logs:

```bash
sudo tail -n 30 /var/log/vsftpd.log
sudo journalctl -u vsftpd -n 30 --no-pager
```
