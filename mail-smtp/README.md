# mail-smtp

Simple email **sending** service for an Ubuntu server.  
It does not receive mail and has no webmail. Sending only.

---

## What you need

- An Ubuntu server with a fixed IP
- Root access
- Your domain in Cloudflare
- A real email address (for the security certificate)

---

## Step 1 — Point mail to this server

In Cloudflare:

1. Add an **A** record:
   - Name: `mail`
   - Value: your server IP
   - Turn the orange cloud **OFF** (DNS only / grey)

2. Ask your hosting provider to set **PTR**:
   - Server IP → `mail.example.com`

Replace `example.com` with your real domain everywhere below.

---

## Step 2 — Install once

Copy this folder to the server (for example `/opt/mail-smtp`), then:

```bash
cd /opt/mail-smtp
sed -i 's/\r$//' install.sh uninstall.sh bin/*
cp mail-smtp.env.example mail-smtp.env
nano mail-smtp.env
```

Fill in only these values:

| Setting | What to put |
|---------|-------------|
| `MAIL_HOSTNAME` | `mail.example.com` |
| `ACME_EMAIL` | your real email |
| `CF_API_TOKEN` | Cloudflare token with DNS Edit permission |

Create the token here:  
https://dash.cloudflare.com/profile/api-tokens  
Permission needed: **Zone → DNS → Edit**

Then run:

```bash
bash install.sh
```

The security certificate renews automatically after this.

---

## Step 3 — Create a sending account

```bash
mail-smtp-account add info@example.com
```

Use a password with at least 12 characters.  
The command prints a **DKIM** text — save it for the next step.

Useful commands:

```bash
mail-smtp-account list
mail-smtp-account passwd info@example.com
mail-smtp-account remove info@example.com
mail-smtp-account dkim example.com
mail-smtp-account dkim-remove example.com
```

---

## Step 4 — DNS for each sending domain

Do this for every domain you send from (the part after `@`).

### SPF (TXT on `example.com`)

If you have no SPF yet:

```text
v=spf1 ip4:YOUR_SERVER_IP -all
```

If SPF already exists, edit that same record and add `ip4:YOUR_SERVER_IP`.  
Do **not** create two SPF records.

Example:

```text
v=spf1 include:_spf.mx.cloudflare.net ip4:YOUR_SERVER_IP ~all
```

### DKIM (TXT)

- Name: `mail._domainkey`
- Value: the text printed by `mail-smtp-account add`

### DMARC (TXT)

- Name: `_dmarc`
- Value:

```text
v=DMARC1; p=none;
```

Wait a few minutes (sometimes longer), then send a test.

---

## Step 5 — App / website settings

| Setting | Value |
|---------|--------|
| SMTP host | `mail.example.com` |
| Port | `587` (recommended) or `465` |
| Encryption | Port 587: STARTTLS — Port 465: SSL/TLS |
| Username | full address (`info@example.com`) |
| Password | the account password |
| From | must be **exactly** the same as the username |

---

## Checklist

- [ ] `mail` A record points to this server (orange cloud off)
- [ ] PTR is set
- [ ] `install.sh` finished
- [ ] Sending account created
- [ ] SPF includes this server IP
- [ ] DKIM added in DNS
- [ ] DMARC added in DNS
- [ ] Test send works (including to Gmail)

---

## Common problems

**Gmail says SPF or DKIM failed**  
Check Step 4 again.

**App sends OK, but the message never arrives**  
Ask hosting to open **outbound port 25**. Without it, this server cannot deliver to Gmail/Yahoo.

**Wrong domain added by mistake**

```bash
mail-smtp-account dkim-remove example.com
```

**DKIM key error in logs**

```bash
mail-smtp-apply
systemctl restart opendkim
```

---

## Uninstall

```bash
sudo bash uninstall.sh
sudo bash uninstall.sh --purge-packages
```

---

## Important

Use this for normal site emails (orders, password reset, contact forms).  
Not for bulk or marketing mail. Daily limits apply.
