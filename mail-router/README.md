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
| `TLS_CERT_FILE` / `TLS_KEY_FILE` | Optional STARTTLS for SMTP |

Panel: `http://127.0.0.1:8088` (SSH tunnel or reverse proxy).

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

- Panel defaults to localhost; expose only via SSH tunnel or authenticated reverse proxy + TLS.
- Sender tokens are stored as PBKDF2 hashes (shown once in the UI).
- Unknown recipients are rejected (not an open relay).
- Loop protection: `X-Mail-Router` header + hop limit.
- Daily quotas for submitters (env defaults; overridable later per design).
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
