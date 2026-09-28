# mail-router

Lightweight **SMTP submission + rule-based mail forwarder** with a small admin panel.

- Does **not** store mailboxes (forward or discard only; short retry queue).
- Does **not** modify or depend on `mail-smtp`.
- One Python process, SQLite on disk, stdlib only (`openssl` optional for DKIM).
- One script controls start / stop / install.

---

## What it does

| Feature | Purpose |
|---------|---------|
| **Senders** | SMTP AUTH accounts (email + token) for apps — same idea as `mail-smtp-account`, via UI |
| **Listen addresses** | Which inbound recipients this host accepts (`user@domain` or `*@domain`) |
| **Rules** | Match From / To / Cc / Subject / body / envelope / custom headers → forward to destinations |
| **Domains** | Optional DKIM keys + DNS TXT to paste |
| **Panel** | Configure only — **no compose / send UI** |

Inbound mail that matches listen addresses is evaluated against rules (priority order). Matched messages are forwarded with **SRS** envelope rewriting so SPF can still pass. Nothing is kept as inbox storage.

---

## Requirements

- Ubuntu (or similar) with Python 3.10+
- Root (or `cap_net_bind_service`) to bind port **25**
- Open **outbound 25** to the internet (delivery)
- DNS: `A` / `MX` for domains you receive on; PTR recommended for the sending IP
- Optional: `openssl`, `dig` or `host` (MX lookup / DKIM)

**Port conflict:** do not run `mail-router` and `mail-smtp` on the same host with the same SMTP ports.

---

## Install

```bash
cd /opt
# copy this folder to the server, then:
cd mail-router
# strip Windows CRLF if you copied from a Windows machine (required once):
sed -i 's/\r$//' mail-router.sh mail_router.py mail-router.env.example
cp mail-router.env.example mail-router.env
nano mail-router.env
sudo bash mail-router.sh install
```

Each `install` syncs `./mail-router.env` (or the example) → `/etc/mail-router/mail-router.env` and restarts the service. Use `--keep-env` to skip overwriting `/etc`.

Important env values:

| Key | Meaning |
|-----|---------|
| `MAIL_HOSTNAME` | Public hostname of this host |
| `ADMIN_PASSWORD` | First-boot panel password (min 12 chars); change after login |
| `PANEL_BIND` | Default `127.0.0.1` — keep it unless behind a trusted reverse proxy |
| `PANEL_LOGIN_FAIL_MAX` | Wrong passwords before lock (default `3`) |
| `PANEL_LOGIN_LOCK_MINUTES` | Lock duration after too many failures (default `10`) |
| `TLS_CERT_FILE` / `TLS_KEY_FILE` | Optional STARTTLS for SMTP |

Panel: `http://127.0.0.1:8088` on the server (not public). Open it from your PC with an SSH tunnel.

### Open the panel (SSH tunnel)

Panel listens only on the server’s localhost. From Windows / macOS / Linux, forward a **local** port to the server panel port (`PANEL_PORT`, default `8088`).

```bash
# Local port 8088 → server panel 8088 (use your SSH host/port)
ssh -L 8088:127.0.0.1:8088 -p SSH_PORT root@SERVER_IP
```

If local `8088` is already in use, pick another local port (e.g. `8089`) — the number **before** the first colon is yours; the one after `127.0.0.1:` is the panel on the server:

```bash
ssh -L 8089:127.0.0.1:8088 -p SSH_PORT root@SERVER_IP
```

Keep that SSH session open, then browse:

- same local port: [http://127.0.0.1:8088](http://127.0.0.1:8088)
- or with `8089`: [http://127.0.0.1:8089](http://127.0.0.1:8089)

`SSH_PORT` is whatever port your `sshd` uses (often `22`; `Connection refused` on 22 usually means a different port).

Without systemd (foreground/background via pid file):

```bash
sudo bash mail-router.sh start
sudo bash mail-router.sh status
sudo bash mail-router.sh stop
```

---

## Typical setup

1. Point **MX** for the domain at this server.
2. Open the panel → **Listen** → add e.g. `support@example.com` or `*@example.com`.
3. **Rules** → create forwards (conditions optional = always match).
4. **Senders** → create tokens for apps that need outbound SMTP (port **587**, AUTH, From = username).
5. **Domains** → generate DKIM and publish the TXT record; add SPF including this server IP.

### Rule fields / operators

**Fields:** `from`, `to`, `cc`, `subject`, `body`, `reply-to`, `envelope_from`, `envelope_to`, `header:Name`

**Operators:** `equals`, `not_equals`, `contains`, `not_contains`, `starts_with`, `ends_with`, `regex`, `is_empty`, `not_empty`

Match mode: **all** or **any** conditions. Lower **priority** number runs first. **Stop on match** ends rule evaluation.

---

## Security notes

- Panel defaults to **localhost only** (`PANEL_BIND=127.0.0.1`). Open with an SSH tunnel; do not bind `0.0.0.0`.
- Login uses a one-time server-side CSRF token + math captcha. After `PANEL_LOGIN_FAIL_MAX` wrong passwords (default **3**), all logins lock for `PANEL_LOGIN_LOCK_MINUTES` (default **10**).
- systemd runs as user `mail-router` with filesystem sandbox (`ProtectSystem`, `ProtectHome`, `ReadWritePaths` limited to data/config). Compromising the process does not grant access to other users’ home dirs or unrelated paths.
- Sender tokens are stored as PBKDF2 hashes (shown once in the UI).
- Unknown recipients are rejected (not an open relay).
- Loop protection: `X-Mail-Router` header + hop limit.
- Daily quotas for submitters (env defaults).
- Queue TTL drops undeliverable mail after retries (no silent infinite store).

---

## Uninstall

```bash
sudo bash mail-router.sh uninstall
sudo bash mail-router.sh uninstall --purge-data
```

---

## Layout

```text
mail-router/
  mail_router.py           # SMTP + queue + panel (single process)
  mail-router.sh           # start | stop | restart | status | install | uninstall
  mail-router.env.example
  README.md
```

Data (SQLite, SRS secret, logs, pid): `DATA_DIR` (default `/var/lib/mail-router`).
