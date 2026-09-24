#!/usr/bin/env python3
"""mail-router: lightweight SMTP submission + rule-based forwarder + admin panel.

Single process, SQLite storage, stdlib only. No mailbox: mail is forwarded or dropped.
Does not touch mail-smtp.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import smtplib
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import Message
from email.utils import getaddresses, parseaddr
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import urlopen

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

APP_NAME = "mail-router"
VERSION = "1.0.0"
LOOP_HEADER = "X-Mail-Router"
SRS_PREFIX = "SRS0"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


@dataclass
class Config:
    mail_hostname: str
    data_dir: Path
    smtp_bind: str
    smtp_port: int
    submission_port: int
    panel_bind: str
    panel_port: int
    admin_password: str
    tls_cert_file: str
    tls_key_file: str
    daily_message_quota: int
    daily_recipient_quota: int
    max_recipients_per_message: int
    max_message_size_bytes: int
    queue_max_attempts: int
    queue_retry_seconds: int
    queue_ttl_seconds: int
    unmatched_action: str
    max_hops: int
    srs_secret: str
    public_ip: str  # optional override; empty = auto-detect

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mail-router.db"

    @property
    def dkim_dir(self) -> Path:
        return self.data_dir / "dkim"

    @property
    def has_tls(self) -> bool:
        return bool(self.tls_cert_file and self.tls_key_file
                    and Path(self.tls_cert_file).is_file()
                    and Path(self.tls_key_file).is_file())


def build_config() -> Config:
    env_path = Path(os.environ.get("MAIL_ROUTER_ENV", "")).expanduser()
    if not env_path.is_file():
        here = Path(__file__).resolve().parent
        for candidate in (
            Path("/etc/mail-router/mail-router.env"),
            here / "mail-router.env",
            here / "mail-router.env.example",
        ):
            if candidate.is_file():
                env_path = candidate
                break
    file_kv = load_env_file(env_path) if env_path.is_file() else {}
    env = {**file_kv, **{k: v for k, v in os.environ.items() if k.startswith("MAIL_") or k in file_kv}}

    def g(key: str, default: str = "") -> str:
        return str(env.get(key, default)).strip()

    data_dir = Path(g("DATA_DIR", "/var/lib/mail-router")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    secret_file = data_dir / "srs.secret"
    if secret_file.is_file():
        srs_secret = secret_file.read_text(encoding="utf-8").strip()
    else:
        srs_secret = secrets.token_hex(32)
        secret_file.write_text(srs_secret + "\n", encoding="utf-8")
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass

    mail_hostname = g("MAIL_HOSTNAME", "localhost")
    tls_cert = g("TLS_CERT_FILE", "")
    tls_key = g("TLS_KEY_FILE", "")
    if not tls_cert or not tls_key:
        le_cert = Path(f"/etc/letsencrypt/live/{mail_hostname}/fullchain.pem")
        le_key = Path(f"/etc/letsencrypt/live/{mail_hostname}/privkey.pem")
        if le_cert.is_file() and le_key.is_file():
            tls_cert = str(le_cert)
            tls_key = str(le_key)

    return Config(
        mail_hostname=mail_hostname,
        data_dir=data_dir,
        smtp_bind=g("SMTP_BIND", "0.0.0.0"),
        smtp_port=int(g("SMTP_PORT", "25") or "25"),
        submission_port=int(g("SUBMISSION_PORT", "587") or "587"),
        panel_bind=g("PANEL_BIND", "127.0.0.1"),
        panel_port=int(g("PANEL_PORT", "8088") or "8088"),
        admin_password=g("ADMIN_PASSWORD", "change-me-now-please"),
        tls_cert_file=tls_cert,
        tls_key_file=tls_key,
        daily_message_quota=int(g("DAILY_MESSAGE_QUOTA", "100") or "100"),
        daily_recipient_quota=int(g("DAILY_RECIPIENT_QUOTA", "200") or "200"),
        max_recipients_per_message=int(g("MAX_RECIPIENTS_PER_MESSAGE", "10") or "10"),
        max_message_size_bytes=int(g("MAX_MESSAGE_SIZE_BYTES", "10485760") or "10485760"),
        queue_max_attempts=int(g("QUEUE_MAX_ATTEMPTS", "8") or "8"),
        queue_retry_seconds=int(g("QUEUE_RETRY_SECONDS", "120") or "120"),
        queue_ttl_seconds=int(g("QUEUE_TTL_SECONDS", "86400") or "86400"),
        unmatched_action=(g("UNMATCHED_ACTION", "discard") or "discard").lower(),
        max_hops=int(g("MAX_HOPS", "20") or "20"),
        srs_secret=srs_secret,
        public_ip=g("PUBLIC_IP", "") or g("SERVER_IP", ""),
    )


def _looks_ipv4(value: str) -> bool:
    parts = (value or "").strip().split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def detect_server_ipv4(hostname: str = "", override: str = "") -> str:
    """Best-effort public IPv4 for SPF examples (override → DNS → route → HTTP)."""
    override = (override or "").strip()
    if _looks_ipv4(override):
        return override

    host = (hostname or "").strip().rstrip(".")
    if host and host not in ("localhost", "127.0.0.1"):
        try:
            for info in socket.getaddrinfo(host, None, socket.AF_INET):
                ip = info[4][0]
                if _looks_ipv4(ip) and not ip.startswith("127."):
                    return ip
        except OSError:
            pass
        for cmd in (
            ["dig", "+short", "A", host],
            ["host", "-t", "A", host],
        ):
            try:
                out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5)
                text = out.decode("utf-8", errors="replace")
                for token in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
                    if _looks_ipv4(token) and not token.startswith("127."):
                        return token
            except Exception:
                continue

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2)
        sock.connect(("1.1.1.1", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if _looks_ipv4(ip) and not ip.startswith("127."):
            return ip
    except OSError:
        pass

    for url in (
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "http://checkip.amazonaws.com",
    ):
        try:
            with urlopen(url, timeout=3) as resp:  # noqa: S310 — admin-panel IP hint only
                ip = resp.read().decode("utf-8", errors="replace").strip()
                if _looks_ipv4(ip):
                    return ip
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------------------
# Logging / events
# ---------------------------------------------------------------------------

log = logging.getLogger(APP_NAME)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Password / token helpers
# ---------------------------------------------------------------------------

def hash_secret(secret: str, *, iterations: int = 120_000) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_secret(secret: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iters_s)
        salt = base64.b64decode(salt_b64.encode())
        expected = base64.b64decode(hash_b64.encode())
        dk = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def gen_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def normalize_email(addr: str) -> str:
    name, email = parseaddr(addr or "")
    email = (email or addr or "").strip().lower()
    return email


def domain_of(email: str) -> str:
    email = normalize_email(email)
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[1]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class DB:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self, cfg: Config) -> None:
        conn = self.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    password_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS senders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    token_hash TEXT NOT NULL,
                    token_hint TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    daily_msg_quota INTEGER,
                    daily_rcpt_quota INTEGER,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quota (
                    day TEXT NOT NULL,
                    sender_email TEXT NOT NULL COLLATE NOCASE,
                    messages INTEGER NOT NULL DEFAULT 0,
                    recipients INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, sender_email)
                );
                CREATE TABLE IF NOT EXISTS listen_addresses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 100,
                    match_mode TEXT NOT NULL DEFAULT 'all',
                    stop_on_match INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_conditions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
                    field TEXT NOT NULL,
                    op TEXT NOT NULL,
                    value TEXT NOT NULL,
                    case_sensitive INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS rule_destinations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
                    email TEXT NOT NULL COLLATE NOCASE
                );
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    next_attempt_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    envelope_from TEXT NOT NULL,
                    envelope_to TEXT NOT NULL,
                    raw_message BLOB NOT NULL,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS domains (
                    domain TEXT PRIMARY KEY COLLATE NOCASE,
                    dkim_selector TEXT NOT NULL DEFAULT 'mail',
                    private_key_pem TEXT NOT NULL,
                    dns_txt TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_queue_next ON queue(next_attempt_at);
                """
            )
            row = conn.execute("SELECT password_hash FROM admin WHERE id = 1").fetchone()
            if not row:
                if len(cfg.admin_password) < 12:
                    raise SystemExit(
                        "ADMIN_PASSWORD must be at least 12 characters on first boot"
                    )
                conn.execute(
                    "INSERT INTO admin (id, password_hash, updated_at) VALUES (1, ?, ?)",
                    (hash_secret(cfg.admin_password), utc_now_iso()),
                )
                self._event(conn, "boot", "admin account created from ADMIN_PASSWORD")
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('version', ?)",
                (VERSION,),
            )
            conn.commit()
        finally:
            conn.close()

    def _event(self, conn: sqlite3.Connection, kind: str, detail: str) -> None:
        conn.execute(
            "INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)",
            (utc_now_iso(), kind, detail[:2000]),
        )
        # Keep last 2000 events
        conn.execute(
            """
            DELETE FROM events WHERE id NOT IN (
                SELECT id FROM events ORDER BY id DESC LIMIT 2000
            )
            """
        )

    def event(self, kind: str, detail: str) -> None:
        conn = self.connect()
        try:
            self._event(conn, kind, detail)
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# SRS (Sender Rewriting Scheme) — keeps SPF valid when forwarding
# ---------------------------------------------------------------------------

def _b32_nopad(data: bytes) -> str:
    return base64.b32encode(data).decode("ascii").rstrip("=").lower()


def srs_encode(original_from: str, hostname: str, secret: str) -> str:
    original_from = normalize_email(original_from)
    if not original_from or "@" not in original_from:
        return f"noreply@{hostname}"
    local, domain = original_from.rsplit("@", 1)
    # timestamp: days since epoch mod 1024, base32-ish short
    day = int(time.time()) // 86400 % 1024
    tt = format(day, "x")
    payload = f"{tt}={domain}={local}"
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()[:4]
    hh = _b32_nopad(digest)[:8]
    # SRS local-part must stay within SMTP limits
    srs_local = f"{SRS_PREFIX}={hh}={tt}={domain}={local}"
    if len(srs_local) > 64:
        srs_local = srs_local[:64]
    return f"{srs_local}@{hostname}"


def srs_decode(srs_addr: str, secret: str) -> Optional[str]:
    srs_addr = normalize_email(srs_addr)
    if "@" not in srs_addr:
        return None
    local, _host = srs_addr.split("@", 1)
    parts = local.split("=")
    if len(parts) < 5 or parts[0].upper() != SRS_PREFIX:
        return None
    _pref, hh, tt, domain = parts[0], parts[1], parts[2], parts[3]
    orig_local = "=".join(parts[4:])
    payload = f"{tt}={domain}={orig_local}"
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()[:4]
    expect = _b32_nopad(digest)[:8]
    if not hmac.compare_digest(hh.lower(), expect.lower()):
        return None
    return f"{orig_local}@{domain}"


# ---------------------------------------------------------------------------
# DKIM (openssl-backed, optional)
# ---------------------------------------------------------------------------

def openssl_available() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True,
            capture_output=True,
            timeout=5,
        )
        return True
    except Exception:
        return False


def generate_dkim_keypair(domain: str, selector: str = "mail") -> tuple[str, str]:
    """Return (private_pem, dns_txt_value)."""
    if not openssl_available():
        raise RuntimeError("openssl not found — install openssl to manage DKIM keys")
    priv = subprocess.check_output(
        ["openssl", "genrsa", "2048"],
        stderr=subprocess.DEVNULL,
    )
    pub = subprocess.check_output(
        ["openssl", "rsa", "-pubout"],
        input=priv,
        stderr=subprocess.DEVNULL,
    )
    # Extract base64 modulus-less SPKI for DKIM p=
    # Convert to DER public key then base64
    der = subprocess.check_output(
        ["openssl", "rsa", "-pubin", "-outform", "DER"],
        input=pub,
        stderr=subprocess.DEVNULL,
    )
    p_b64 = base64.b64encode(der).decode("ascii")
    # DKIM wants PKCS#1 RSAPublicKey OR SPKI; OpenDKIM often accepts SPKI in p=
    # Prefer PKCS#1 for wider compatibility:
    try:
        pkcs1 = subprocess.check_output(
            ["openssl", "rsa", "-pubin", "-RSAPublicKey_out", "-outform", "DER"],
            input=pub,
            stderr=subprocess.DEVNULL,
        )
        p_b64 = base64.b64encode(pkcs1).decode("ascii")
    except subprocess.CalledProcessError:
        pass
    dns = f"v=DKIM1; k=rsa; p={p_b64}"
    return priv.decode("ascii"), dns


def _dkim_canonicalize_header(name: str, value: str) -> str:
    # relaxed header canonicalization
    name = name.lower()
    value = re.sub(r"\r?\n[ \t]+", " ", value)
    value = re.sub(r"[ \t]+", " ", value).strip()
    return f"{name}:{value}\r\n"


def _dkim_body_hash(raw: bytes) -> str:
    # Split headers/body
    if b"\r\n\r\n" in raw:
        body = raw.split(b"\r\n\r\n", 1)[1]
    elif b"\n\n" in raw:
        body = raw.split(b"\n\n", 1)[1].replace(b"\n", b"\r\n")
    else:
        body = b""
    # relaxed body: ignore trailing empty lines, collapse WSP
    text = body.replace(b"\r\n", b"\n").decode("utf-8", errors="replace")
    lines = text.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    out_lines = []
    for line in lines:
        line = re.sub(r"[ \t]+", " ", line).rstrip(" \t")
        out_lines.append(line)
    canon = ("\r\n".join(out_lines) + ("\r\n" if out_lines else "")).encode("utf-8")
    if not out_lines:
        canon = b""
    return base64.b64encode(hashlib.sha256(canon).digest()).decode("ascii")


def dkim_sign_message(
    raw: bytes,
    domain: str,
    selector: str,
    private_pem: str,
    headers_to_sign: Optional[list[str]] = None,
) -> bytes:
    """Prepend DKIM-Signature using openssl rsautl/pkeyutl. Best-effort."""
    if not openssl_available():
        return raw
    headers_to_sign = headers_to_sign or [
        "from", "to", "cc", "subject", "date", "message-id", "mime-version",
        "content-type", "reply-to",
    ]
    # Parse header block
    if b"\r\n\r\n" in raw:
        header_blob, _body = raw.split(b"\r\n\r\n", 1)
        nl = b"\r\n"
    elif b"\n\n" in raw:
        header_blob, body = raw.split(b"\n\n", 1)
        raw = header_blob.replace(b"\n", b"\r\n") + b"\r\n\r\n" + body.replace(b"\n", b"\r\n")
        header_blob = raw.split(b"\r\n\r\n", 1)[0]
        nl = b"\r\n"
    else:
        return raw

    # Unfold and index headers
    header_text = header_blob.decode("utf-8", errors="replace")
    hdrs: list[tuple[str, str]] = []
    current: Optional[list[str]] = None
    for line in header_text.split("\n"):
        line = line.rstrip("\r")
        if not line:
            continue
        if line[0] in " \t" and current is not None:
            current[1] += " " + line.strip()
        elif ":" in line:
            name, val = line.split(":", 1)
            current = [name, val.strip()]
            hdrs.append((current[0], current[1]))  # type: ignore
        else:
            current = None

    bh = _dkim_body_hash(raw)
    present = {h[0].lower(): h for h in hdrs}
    signed_names = [h for h in headers_to_sign if h in present]
    if "from" not in signed_names and "from" in present:
        signed_names.insert(0, "from")

    dkim_fields = {
        "v": "1",
        "a": "rsa-sha256",
        "c": "relaxed/relaxed",
        "d": domain,
        "s": selector,
        "t": str(int(time.time())),
        "bh": bh,
        "h": ":".join(signed_names),
    }
    # Build signature input
    canon_headers = "".join(
        _dkim_canonicalize_header(present[n][0], present[n][1]) for n in signed_names
    )
    dkim_value_nosig = "; ".join(f"{k}={v}" for k, v in dkim_fields.items()) + "; b="
    canon_dkim = _dkim_canonicalize_header("dkim-signature", dkim_value_nosig)
    to_sign = (canon_headers + canon_dkim.rstrip("\r\n")).encode("utf-8")

    try:
        fd, key_name = tempfile.mkstemp(prefix="mail-router-dkim-", suffix=".pem")
        key_path = Path(key_name)
        try:
            os.write(fd, private_pem.encode("ascii"))
            os.close(fd)
            os.chmod(key_path, 0o600)
            sig = subprocess.check_output(
                ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
                input=to_sign,
                stderr=subprocess.DEVNULL,
            )
        finally:
            try:
                key_path.unlink(missing_ok=True)
            except Exception:
                pass
        b_val = base64.b64encode(sig).decode("ascii")
    except Exception as exc:
        log.warning("DKIM sign failed for %s: %s", domain, exc)
        return raw

    dkim_header = (
        "DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed; "
        f"d={domain}; s={selector}; t={dkim_fields['t']}; "
        f"bh={bh}; h={dkim_fields['h']}; b={b_val}\r\n"
    )
    return dkim_header.encode("ascii") + raw

# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------

OPS = {
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
    "regex",
    "is_empty",
    "not_empty",
}


def _emails_from_header(msg: Message, *header_names: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for name in header_names:
        for _, email in getaddresses(msg.get_all(name, []) or []):
            e = normalize_email(email)
            if e and e not in seen:
                seen.add(e)
                out.append(e)
    return out


def match_recipient_condition(
    op: str,
    expected: str,
    candidates: list[str],
    case_sensitive: bool,
) -> bool:
    """Match equals/contains against a list of recipient emails (header and/or envelope)."""
    expected_n = normalize_email(expected) if not case_sensitive else (expected or "").strip()
    cand = [normalize_email(c) if not case_sensitive else c.strip() for c in candidates if c]
    if op == "equals":
        return expected_n in cand
    if op == "not_equals":
        return expected_n not in cand
    if op == "contains":
        return any(expected_n in c for c in cand) if expected_n else False
    if op == "not_contains":
        return all(expected_n not in c for c in cand) if expected_n else True
    if op == "starts_with":
        return any(c.startswith(expected_n) for c in cand)
    if op == "ends_with":
        return any(c.endswith(expected_n) for c in cand)
    if op == "is_empty":
        return not cand
    if op == "not_empty":
        return bool(cand)
    # regex / fallback: join candidates
    return match_op(", ".join(cand), op, expected, case_sensitive)


def extract_field(msg: Message, raw: bytes, field: str) -> str:
    field_l = field.lower().strip()
    if field_l == "from":
        return normalize_email(msg.get("From", ""))
    if field_l == "to":
        return ", ".join(_emails_from_header(msg, "To"))
    if field_l == "cc":
        return ", ".join(_emails_from_header(msg, "Cc"))
    if field_l == "bcc":
        return ", ".join(_emails_from_header(msg, "Bcc"))
    if field_l == "subject":
        return msg.get("Subject", "") or ""
    if field_l == "reply-to":
        return normalize_email(msg.get("Reply-To", ""))
    if field_l == "body":
        return _extract_body_text(msg)
    if field_l.startswith("header:"):
        name = field[7:].strip()
        return msg.get(name, "") or ""
    if field_l == "envelope_to":
        return ""  # filled by caller
    return msg.get(field, "") or ""


def _extract_body_text(msg: Message) -> str:
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try:
                    parts.append(part.get_content())  # type: ignore[attr-defined]
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
            elif ctype == "text/html" and not parts:
                try:
                    html_body = part.get_content()  # type: ignore[attr-defined]
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    html_body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                parts.append(re.sub(r"<[^>]+>", " ", str(html_body)))
    else:
        try:
            parts.append(str(msg.get_content()))  # type: ignore[attr-defined]
        except Exception:
            payload = msg.get_payload(decode=True) or b""
            parts.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(parts)


def match_op(value: str, op: str, expected: str, case_sensitive: bool) -> bool:
    if not case_sensitive:
        value_cmp = value.lower()
        expected_cmp = expected.lower()
    else:
        value_cmp = value
        expected_cmp = expected

    if op == "equals":
        return value_cmp == expected_cmp
    if op == "not_equals":
        return value_cmp != expected_cmp
    if op == "contains":
        return expected_cmp in value_cmp
    if op == "not_contains":
        return expected_cmp not in value_cmp
    if op == "starts_with":
        return value_cmp.startswith(expected_cmp)
    if op == "ends_with":
        return value_cmp.endswith(expected_cmp)
    if op == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            return re.search(expected, value, flags) is not None
        except re.error:
            return False
    if op == "is_empty":
        return not value.strip()
    if op == "not_empty":
        return bool(value.strip())
    return False


def address_matches_pattern(rcpt: str, pattern: str) -> bool:
    rcpt = normalize_email(rcpt)
    pattern = pattern.strip().lower()
    if pattern == "*":
        return True
    if pattern.startswith("*@"):
        return rcpt.endswith("@" + pattern[2:]) or rcpt == pattern[2:]
    return rcpt == pattern


@dataclass
class MatchedRule:
    rule_id: int
    name: str
    destinations: list[str]
    stop_on_match: bool


class RuleEngine:
    def __init__(self, db: DB):
        self.db = db

    def is_listened(self, rcpt: str) -> bool:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT address FROM listen_addresses WHERE enabled = 1"
            ).fetchall()
            return any(address_matches_pattern(rcpt, r["address"]) for r in rows)
        finally:
            conn.close()

    def match(
        self,
        msg: Message,
        raw: bytes,
        envelope_from: str,
        envelope_to: list[str],
    ) -> list[MatchedRule]:
        conn = self.db.connect()
        try:
            rules = conn.execute(
                "SELECT * FROM rules WHERE enabled = 1 ORDER BY priority ASC, id ASC"
            ).fetchall()
            matched: list[MatchedRule] = []
            for rule in rules:
                conds = conn.execute(
                    "SELECT * FROM rule_conditions WHERE rule_id = ?",
                    (rule["id"],),
                ).fetchall()
                dests = [
                    normalize_email(r["email"])
                    for r in conn.execute(
                        "SELECT email FROM rule_destinations WHERE rule_id = ?",
                        (rule["id"],),
                    ).fetchall()
                ]
                dests = [d for d in dests if d]
                if not dests:
                    continue

                results = []
                for c in conds:
                    field = (c["field"] or "").lower().strip()
                    op = c["op"]
                    case = bool(c["case_sensitive"])
                    if field == "envelope_from":
                        val = normalize_email(envelope_from)
                        results.append(match_op(val, op, c["value"], case))
                    elif field == "envelope_to":
                        results.append(
                            match_recipient_condition(
                                op, c["value"], list(envelope_to), case
                            )
                        )
                    elif field in ("to", "cc", "bcc"):
                        # Header recipients + envelope RCPT (real mail often differs)
                        header_map = {
                            "to": ("To",),
                            "cc": ("Cc",),
                            "bcc": ("Bcc",),
                        }
                        candidates = _emails_from_header(msg, *header_map[field])
                        if field == "to":
                            for e in envelope_to:
                                en = normalize_email(e)
                                if en and en not in candidates:
                                    candidates.append(en)
                        results.append(
                            match_recipient_condition(op, c["value"], candidates, case)
                        )
                    else:
                        val = extract_field(msg, raw, c["field"])
                        results.append(match_op(val, op, c["value"], case))

                if not conds:
                    ok = True  # unconditional rule
                elif (rule["match_mode"] or "all").lower() == "any":
                    ok = any(results)
                else:
                    ok = all(results)

                if ok:
                    matched.append(
                        MatchedRule(
                            rule_id=rule["id"],
                            name=rule["name"],
                            destinations=dests,
                            stop_on_match=bool(rule["stop_on_match"]),
                        )
                    )
                    if rule["stop_on_match"]:
                        break
            return matched
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Quotas / senders
# ---------------------------------------------------------------------------

class SenderStore:
    def __init__(self, db: DB, cfg: Config):
        self.db = db
        self.cfg = cfg

    def authenticate(self, email: str, token: str) -> bool:
        email = normalize_email(email)
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT token_hash, enabled FROM senders WHERE email = ?",
                (email,),
            ).fetchone()
            if not row or not row["enabled"]:
                return False
            return verify_secret(token, row["token_hash"])
        finally:
            conn.close()

    def check_quota(self, email: str, add_rcpts: int) -> Optional[str]:
        email = normalize_email(email)
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT daily_msg_quota, daily_rcpt_quota FROM senders WHERE email = ?",
                (email,),
            ).fetchone()
            msg_q = (row["daily_msg_quota"] if row and row["daily_msg_quota"] is not None
                     else self.cfg.daily_message_quota)
            rcpt_q = (row["daily_rcpt_quota"] if row and row["daily_rcpt_quota"] is not None
                      else self.cfg.daily_recipient_quota)
            day = date.today().isoformat()
            cur = conn.execute(
                "SELECT messages, recipients FROM quota WHERE day = ? AND sender_email = ?",
                (day, email),
            ).fetchone()
            msgs = int(cur["messages"]) if cur else 0
            rcpts = int(cur["recipients"]) if cur else 0
            if msgs + 1 > msg_q:
                return f"Daily message quota exceeded ({msg_q}/day)"
            if rcpts + add_rcpts > rcpt_q:
                return f"Daily recipient quota exceeded ({rcpt_q}/day)"
            return None
        finally:
            conn.close()

    def bump_quota(self, email: str, recipients: int) -> None:
        email = normalize_email(email)
        day = date.today().isoformat()
        conn = self.db.connect()
        try:
            conn.execute(
                """
                INSERT INTO quota (day, sender_email, messages, recipients)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(day, sender_email) DO UPDATE SET
                    messages = messages + 1,
                    recipients = recipients + excluded.recipients
                """,
                (day, email, recipients),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Outbound delivery + queue
# ---------------------------------------------------------------------------

class Mailer:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db

    def _dkim_for_domain(self, domain: str) -> Optional[tuple[str, str]]:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT dkim_selector, private_key_pem FROM domains WHERE domain = ?",
                (domain.lower(),),
            ).fetchone()
            if not row:
                return None
            return row["dkim_selector"], row["private_key_pem"]
        finally:
            conn.close()

    def prepare_raw(self, raw: bytes, signing_domain: Optional[str] = None) -> bytes:
        if not signing_domain:
            return raw
        pair = self._dkim_for_domain(signing_domain)
        if not pair:
            return raw
        selector, pem = pair
        return dkim_sign_message(raw, signing_domain, selector, pem)

    def deliver(self, envelope_from: str, envelope_to: list[str], raw: bytes) -> None:
        # Sign with hostname domain if we have a key
        host_domain = domain_of(f"x@{self.cfg.mail_hostname}") or self.cfg.mail_hostname
        # Prefer signing as the domain in envelope_from if local SRS / our domain
        sign_domain = None
        ef_dom = domain_of(envelope_from)
        if ef_dom and self._dkim_for_domain(ef_dom):
            sign_domain = ef_dom
        elif self._dkim_for_domain(host_domain):
            sign_domain = host_domain
        raw = self.prepare_raw(raw, sign_domain)

        # Direct MX delivery via smtplib (resolves per recipient domain)
        by_domain: dict[str, list[str]] = {}
        for rcpt in envelope_to:
            d = domain_of(rcpt)
            by_domain.setdefault(d, []).append(rcpt)

        last_err: Optional[Exception] = None
        for d, rcpts in by_domain.items():
            try:
                self._deliver_domain(envelope_from, rcpts, raw, d)
            except Exception as exc:
                last_err = exc
                log.warning("deliver to %s failed: %s", d, exc)
        if last_err and len(by_domain) == 1:
            raise last_err
        if last_err:
            raise last_err

    def _deliver_domain(
        self, envelope_from: str, rcpts: list[str], raw: bytes, domain: str
    ) -> None:
        # Use smtplib with MX lookup via DNS — stdlib has no MX helper; use getaddrinfo fallback
        # Prefer `dig`/`host` if available, else A record of domain
        mx_hosts = self._mx_hosts(domain)
        errors: list[str] = []
        for host in mx_hosts:
            try:
                with smtplib.SMTP(host, 25, timeout=60) as smtp:
                    smtp.ehlo(self.cfg.mail_hostname)
                    if smtp.has_extn("starttls"):
                        ctx = ssl.create_default_context()
                        try:
                            smtp.starttls(context=ctx)
                            smtp.ehlo(self.cfg.mail_hostname)
                        except Exception:
                            pass
                    smtp.sendmail(envelope_from, rcpts, raw)
                return
            except Exception as exc:
                errors.append(f"{host}: {exc}")
        raise RuntimeError("; ".join(errors) or f"no MX for {domain}")

    def _mx_hosts(self, domain: str) -> list[str]:
        hosts: list[str] = []
        # Try `dig` then `host`
        for cmd in (
            ["dig", "+short", "MX", domain],
            ["host", "-t", "MX", domain],
        ):
            try:
                out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=10)
                text = out.decode("utf-8", errors="replace")
                if cmd[0] == "dig":
                    rows = []
                    for line in text.splitlines():
                        parts = line.split()
                        if len(parts) >= 2 and parts[0].isdigit():
                            rows.append((int(parts[0]), parts[1].rstrip(".")))
                    rows.sort()
                    hosts = [h for _, h in rows]
                else:
                    # "domain mail is handled by 10 mx.example.com."
                    for line in text.splitlines():
                        m = re.search(r"handled by\s+(\d+)\s+(\S+)\.?", line, re.I)
                        if m:
                            hosts.append(m.group(2).rstrip("."))
                if hosts:
                    break
            except Exception:
                continue
        if not hosts:
            hosts = [domain]
        return hosts

    def enqueue(
        self,
        envelope_from: str,
        envelope_to: list[str],
        raw: bytes,
    ) -> int:
        conn = self.db.connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO queue (created_at, next_attempt_at, attempts,
                                   envelope_from, envelope_to, raw_message)
                VALUES (?, ?, 0, ?, ?, ?)
                """,
                (
                    utc_now_iso(),
                    utc_now_iso(),
                    envelope_from,
                    json.dumps(envelope_to),
                    raw,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def inject_headers(raw: bytes, headers: dict[str, str]) -> bytes:
    extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    if b"\r\n\r\n" in raw:
        h, b = raw.split(b"\r\n\r\n", 1)
        return h + b"\r\n" + extra.encode("utf-8") + b"\r\n" + b
    if b"\n\n" in raw:
        h, b = raw.split(b"\n\n", 1)
        return h.replace(b"\n", b"\r\n") + b"\r\n" + extra.encode("utf-8") + b"\r\n\r\n" + b.replace(b"\n", b"\r\n")
    return extra.encode("utf-8") + b"\r\n" + raw


def count_received_hops(raw: bytes) -> int:
    try:
        text = raw.split(b"\r\n\r\n", 1)[0].decode("utf-8", errors="replace")
    except Exception:
        return 0
    return sum(1 for line in text.splitlines() if line.lower().startswith("received:"))


def has_loop_marker(raw: bytes, hostname: str) -> bool:
    marker = f"{LOOP_HEADER}: {hostname}".lower().encode()
    head = raw[:8192].lower()
    return marker in head


# ---------------------------------------------------------------------------
# SMTP server (minimal asyncio)
# ---------------------------------------------------------------------------

class SMTPSession:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        app: "App",
        submission: bool,
    ):
        self.reader = reader
        self.writer = writer
        self.app = app
        self.submission = submission
        self.peer = writer.get_extra_info("peername")
        self.hello = ""
        self.mail_from = ""
        self.rcpt_to: list[str] = []
        self.authed_user: Optional[str] = None
        self.tls = False
        self._reset_tx()

    def _reset_tx(self) -> None:
        self.mail_from = ""
        self.rcpt_to = []

    async def send(self, code: int, msg: str) -> None:
        data = f"{code} {msg}\r\n".encode("ascii", errors="replace")
        self.writer.write(data)
        await self.writer.drain()

    async def send_multi(self, code: int, lines: list[str]) -> None:
        for i, line in enumerate(lines):
            sep = "-" if i < len(lines) - 1 else " "
            self.writer.write(f"{code}{sep}{line}\r\n".encode("ascii", errors="replace"))
        await self.writer.drain()

    async def readline(self) -> Optional[str]:
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout=300)
        except (asyncio.TimeoutError, ConnectionError):
            return None
        if not line:
            return None
        return line.decode("utf-8", errors="replace").rstrip("\r\n")

    async def run(self) -> None:
        cfg = self.app.cfg
        await self.send(220, f"{cfg.mail_hostname} {APP_NAME} ESMTP")
        try:
            while True:
                line = await self.readline()
                if line is None:
                    break
                if not line:
                    continue
                cmd, _, arg = line.partition(" ")
                cmd_u = cmd.upper()
                arg = arg.strip()
                if cmd_u == "HELO":
                    self.hello = arg or "unknown"
                    await self.send(250, cfg.mail_hostname)
                elif cmd_u == "EHLO":
                    self.hello = arg or "unknown"
                    caps = [
                        cfg.mail_hostname,
                        "PIPELINING",
                        "8BITMIME",
                        f"SIZE {cfg.max_message_size_bytes}",
                    ]
                    if cfg.has_tls and not self.tls:
                        caps.append("STARTTLS")
                    if self.submission:
                        caps.append("AUTH PLAIN LOGIN")
                    await self.send_multi(250, caps)
                elif cmd_u == "STARTTLS":
                    if self.tls:
                        await self.send(503, "TLS already active")
                    elif not cfg.has_tls:
                        await self.send(
                            454,
                            "TLS not available — set TLS_CERT_FILE/TLS_KEY_FILE "
                            "(or place Let's Encrypt certs for MAIL_HOSTNAME)",
                        )
                    else:
                        try:
                            await self.send(220, "Ready to start TLS")
                            await self._wrap_tls()
                            self._reset_tx()
                            self.authed_user = None
                        except Exception:
                            log.exception("STARTTLS failed")
                            await self.send(454, "TLS not available right now")
                            break
                elif cmd_u == "AUTH" and self.submission:
                    await self._auth(arg)
                elif cmd_u == "MAIL":
                    await self._mail(arg)
                elif cmd_u == "RCPT":
                    await self._rcpt(arg)
                elif cmd_u == "DATA":
                    await self._data()
                elif cmd_u == "RSET":
                    self._reset_tx()
                    await self.send(250, "OK")
                elif cmd_u == "NOOP":
                    await self.send(250, "OK")
                elif cmd_u == "QUIT":
                    await self.send(221, "Bye")
                    break
                else:
                    await self.send(502, "Command not implemented")
        finally:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

    async def _wrap_tls(self) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.app.cfg.tls_cert_file, self.app.cfg.tls_key_file)
        try:
            transport = self.writer.transport
            protocol = transport.get_protocol()
            new_transport = await asyncio.get_running_loop().start_tls(
                transport, protocol, ctx, server_side=True
            )
            self.writer._transport = new_transport  # noqa: SLF001
            self.tls = True
        except Exception as exc:
            log.warning("STARTTLS failed: %s", exc)
            raise

    async def _auth(self, arg: str) -> None:
        mech, _, rest = arg.partition(" ")
        mech = mech.upper()
        if mech == "PLAIN":
            if not rest:
                await self.send(334, "")
                rest = await self.readline() or ""
            try:
                decoded = base64.b64decode(rest.encode("ascii")).decode("utf-8")
                parts = decoded.split("\x00")
                if len(parts) == 3:
                    _, user, password = parts
                elif len(parts) == 2:
                    user, password = parts
                else:
                    raise ValueError("bad PLAIN")
            except Exception:
                await self.send(535, "Authentication failed")
                return
            if self.app.senders.authenticate(user, password):
                self.authed_user = normalize_email(user)
                await self.send(235, "Authentication successful")
            else:
                await self.send(535, "Authentication failed")
                self.app.db.event("auth_fail", f"peer={self.peer} user={user}")
        elif mech == "LOGIN":
            await self.send(334, base64.b64encode(b"Username:").decode())
            u_line = await self.readline()
            await self.send(334, base64.b64encode(b"Password:").decode())
            p_line = await self.readline()
            try:
                user = base64.b64decode((u_line or "").encode()).decode("utf-8")
                password = base64.b64decode((p_line or "").encode()).decode("utf-8")
            except Exception:
                await self.send(535, "Authentication failed")
                return
            if self.app.senders.authenticate(user, password):
                self.authed_user = normalize_email(user)
                await self.send(235, "Authentication successful")
            else:
                await self.send(535, "Authentication failed")
                self.app.db.event("auth_fail", f"peer={self.peer} user={user}")
        else:
            await self.send(504, "AUTH mechanism unavailable")

    async def _mail(self, arg: str) -> None:
        if self.submission and not self.authed_user:
            await self.send(530, "Authentication required")
            return
        m = re.match(r"FROM:\s*<?([^>]*)>?", arg, re.I)
        if not m:
            await self.send(501, "Syntax: MAIL FROM:<address>")
            return
        addr = normalize_email(m.group(1))
        if self.submission:
            if addr != self.authed_user:
                await self.send(553, "From must match authenticated sender")
                return
        self.mail_from = addr
        self.rcpt_to = []
        await self.send(250, "OK")

    async def _rcpt(self, arg: str) -> None:
        if not self.mail_from:
            await self.send(503, "Send MAIL first")
            return
        m = re.match(r"TO:\s*<?([^>]*)>?", arg, re.I)
        if not m:
            await self.send(501, "Syntax: RCPT TO:<address>")
            return
        addr = normalize_email(m.group(1))
        if not addr or "@" not in addr:
            await self.send(501, "Bad address")
            return
        if len(self.rcpt_to) >= self.app.cfg.max_recipients_per_message:
            await self.send(452, "Too many recipients")
            return

        if self.submission:
            err = self.app.senders.check_quota(self.authed_user or "", len(self.rcpt_to) + 1)
            if err:
                await self.send(550, err)
                return
            self.rcpt_to.append(addr)
            await self.send(250, "OK")
            return

        # Inbound: only listened addresses
        if not self.app.rules.is_listened(addr):
            await self.send(550, "Relay not permitted / unknown recipient")
            return
        self.rcpt_to.append(addr)
        await self.send(250, "OK")

    async def _data(self) -> None:
        if not self.mail_from or not self.rcpt_to:
            await self.send(503, "Send MAIL/RCPT first")
            return
        await self.send(354, "End data with <CR><LF>.<CR><LF>")
        chunks: list[bytes] = []
        size = 0
        while True:
            line = await self.reader.readline()
            if not line:
                await self.send(451, "Connection lost")
                return
            if line == b".\r\n" or line == b".\n":
                break
            if line.startswith(b"."):
                line = line[1:]
            size += len(line)
            if size > self.app.cfg.max_message_size_bytes:
                # Drain until dot
                while True:
                    line = await self.reader.readline()
                    if not line or line in (b".\r\n", b".\n"):
                        break
                await self.send(552, "Message too large")
                self._reset_tx()
                return
            chunks.append(line)
        raw = b"".join(chunks)

        try:
            if self.submission:
                await self._handle_submission(raw)
            else:
                await self._handle_inbound(raw)
        except Exception as exc:
            log.exception("DATA handler error")
            self.app.db.event("error", str(exc))
            await self.send(451, "Temporary failure")
            self._reset_tx()

    async def _deliver_or_enqueue(
        self, envelope_from: str, envelope_to: list[str], raw: bytes, kind: str
    ) -> str:
        try:
            await asyncio.to_thread(
                self.app.mailer.deliver, envelope_from, envelope_to, raw
            )
            return "delivered"
        except Exception as exc:
            qid = await asyncio.to_thread(
                self.app.mailer.enqueue, envelope_from, envelope_to, raw
            )
            self.app.db.event(f"{kind}_queued", f"id={qid} err={exc}")
            return "queued"

    async def _handle_submission(self, raw: bytes) -> None:
        user = self.authed_user or ""
        err = self.app.senders.check_quota(user, len(self.rcpt_to))
        if err:
            await self.send(550, err)
            self._reset_tx()
            return
        raw = inject_headers(
            raw,
            {
                LOOP_HEADER: self.app.cfg.mail_hostname,
                "X-Mail-Router-Sender": user,
            },
        )
        result = await self._deliver_or_enqueue(
            self.mail_from, list(self.rcpt_to), raw, "submit"
        )
        self.app.senders.bump_quota(user, len(self.rcpt_to))
        self.app.db.event(
            "submit",
            f"{user} -> {','.join(self.rcpt_to)} bytes={len(raw)} result={result}",
        )
        if result == "delivered":
            await self.send(250, "Queued for delivery")
        else:
            await self.send(250, "Accepted (queued for retry)")
        self._reset_tx()

    async def _handle_inbound(self, raw: bytes) -> None:
        cfg = self.app.cfg
        if has_loop_marker(raw, cfg.mail_hostname):
            await self.send(550, "Mail loop detected")
            self.app.db.event("loop", f"from={self.mail_from}")
            self._reset_tx()
            return
        if count_received_hops(raw) > cfg.max_hops:
            await self.send(550, "Too many hops")
            self._reset_tx()
            return

        msg = message_from_bytes(raw, policy=policy.default)
        matches = self.app.rules.match(msg, raw, self.mail_from, list(self.rcpt_to))
        if not matches:
            detail = f"from={self.mail_from} to={','.join(self.rcpt_to)}"
            log.info("inbound unmatched %s", detail)
            self.app.db.event("unmatched", detail)
            if cfg.unmatched_action == "bounce":
                await self.send(550, "No matching forward rule")
            else:
                await self.send(250, "OK (discarded: no matching rule)")
            self._reset_tx()
            return

        # Collect destinations; loop header prevents cycles if a dest is also listened.
        dests: list[str] = []
        seen: set[str] = set()
        for m in matches:
            for d in m.destinations:
                dn = normalize_email(d)
                if dn and dn not in seen:
                    seen.add(dn)
                    dests.append(dn)

        if not dests:
            log.info("inbound no destinations from=%s to=%s", self.mail_from, self.rcpt_to)
            await self.send(250, "OK (no destinations)")
            self._reset_tx()
            return

        srs_from = srs_encode(self.mail_from, cfg.mail_hostname, cfg.srs_secret)
        fwd_raw = inject_headers(
            raw,
            {
                LOOP_HEADER: cfg.mail_hostname,
                "X-Mail-Router-Original-To": ", ".join(self.rcpt_to),
                "X-Mail-Router-Rule": ", ".join(f"{m.rule_id}:{m.name}" for m in matches),
            },
        )
        result = await self._deliver_or_enqueue(srs_from, dests, fwd_raw, "forward")
        detail = (
            f"{self.mail_from} => {','.join(dests)} "
            f"rules={[m.rule_id for m in matches]} result={result}"
        )
        log.info("inbound forward %s", detail)
        self.app.db.event("forward", detail)
        if result == "delivered":
            await self.send(250, "OK (forwarded)")
        else:
            await self.send(250, "OK (queued for retry)")
        self._reset_tx()


async def smtp_client_cb(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    app: "App",
    submission: bool,
) -> None:
    session = SMTPSession(reader, writer, app, submission=submission)
    await session.run()


# ---------------------------------------------------------------------------
# Queue worker
# ---------------------------------------------------------------------------

async def queue_worker(app: "App") -> None:
    while True:
        try:
            await asyncio.to_thread(process_queue_once, app)
        except Exception:
            log.exception("queue worker error")
        await asyncio.sleep(5)


def process_queue_once(app: "App") -> None:
    cfg = app.cfg
    conn = app.db.connect()
    try:
        now = utc_now()
        now_s = utc_now_iso()
        rows = conn.execute(
            """
            SELECT * FROM queue
            WHERE next_attempt_at <= ?
            ORDER BY id ASC
            LIMIT 20
            """,
            (now_s,),
        ).fetchall()
        for row in rows:
            created = datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            if (now - created).total_seconds() > cfg.queue_ttl_seconds:
                conn.execute("DELETE FROM queue WHERE id = ?", (row["id"],))
                app.db._event(conn, "queue_expired", f"id={row['id']}")
                continue
            if row["attempts"] >= cfg.queue_max_attempts:
                conn.execute("DELETE FROM queue WHERE id = ?", (row["id"],))
                app.db._event(conn, "queue_dropped", f"id={row['id']} err={row['last_error']}")
                continue
            dests = json.loads(row["envelope_to"])
            try:
                app.mailer.deliver(row["envelope_from"], dests, row["raw_message"])
                conn.execute("DELETE FROM queue WHERE id = ?", (row["id"],))
                app.db._event(conn, "queue_sent", f"id={row['id']}")
            except Exception as exc:
                next_at = now + timedelta(seconds=cfg.queue_retry_seconds * (row["attempts"] + 1))
                conn.execute(
                    """
                    UPDATE queue SET attempts = attempts + 1,
                        next_attempt_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (next_at.strftime("%Y-%m-%dT%H:%M:%SZ"), str(exc)[:500], row["id"]),
                )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Admin HTTP panel
# ---------------------------------------------------------------------------

CSS = """
:root { --bg:#0f1419; --card:#1a2332; --text:#e7ecf3; --muted:#9aa7b8; --acc:#3d8bfd; --danger:#e35d6a; --ok:#3ecf8e; --line:#2a3548; }
* { box-sizing: border-box; }
body { margin:0; font:14px/1.45 system-ui,Segoe UI,sans-serif; background:var(--bg); color:var(--text); }
a { color:var(--acc); text-decoration:none; }
header { display:flex; gap:1rem; align-items:center; padding:0.85rem 1.25rem; border-bottom:1px solid var(--line); background:#121a24; position:sticky; top:0; }
header .brand { font-weight:700; letter-spacing:0.02em; }
header nav { display:flex; gap:0.85rem; flex-wrap:wrap; }
main { max-width:980px; margin:1.25rem auto; padding:0 1rem 3rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:1rem 1.1rem; margin-bottom:1rem; }
h1,h2 { margin:0 0 0.75rem; font-size:1.15rem; }
.muted { color:var(--muted); }
.row { display:flex; gap:0.75rem; flex-wrap:wrap; align-items:end; }
label { display:flex; flex-direction:column; gap:0.25rem; font-size:12px; color:var(--muted); min-width:140px; flex:1; }
input, select, textarea { background:#0e1520; color:var(--text); border:1px solid var(--line); border-radius:6px; padding:0.45rem 0.55rem; font:inherit; }
textarea { min-height:70px; width:100%; }
button, .btn { background:var(--acc); color:#fff; border:0; border-radius:6px; padding:0.45rem 0.8rem; cursor:pointer; font:inherit; display:inline-block; }
button.secondary, .btn.secondary { background:#2a3548; }
button.danger { background:var(--danger); }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; padding:0.45rem 0.35rem; border-bottom:1px solid var(--line); vertical-align:top; }
.flash { padding:0.6rem 0.8rem; border-radius:6px; margin-bottom:1rem; }
.flash.ok { background:rgba(62,207,142,0.15); color:var(--ok); }
.flash.err { background:rgba(227,93,106,0.15); color:var(--danger); }
.token { font-family:ui-monospace,monospace; background:#0e1520; padding:0.6rem 0.75rem; border-radius:6px; word-break:break-all; }
.cond { display:grid; grid-template-columns: 1.2fr 1fr 2fr auto; gap:0.4rem; margin-bottom:0.4rem; }
@media (max-width:700px){ .cond{ grid-template-columns:1fr; } }
"""


def h(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)


class Panel:
    def __init__(self, app: "App"):
        self.app = app

    def layout(self, title: str, body: str, user_ok: bool = True, flash: str = "", flash_err: str = "") -> bytes:
        nav = ""
        if user_ok:
            nav = """
            <a href="/">Dashboard</a>
            <a href="/senders">Senders</a>
            <a href="/listen">Listen</a>
            <a href="/rules">Rules</a>
            <a href="/domains">Domains</a>
            <a href="/events">Events</a>
            <a href="/settings">Settings</a>
            <a href="/logout">Logout</a>
            """
        flash_html = ""
        if flash:
            flash_html += f'<div class="flash ok">{h(flash)}</div>'
        if flash_err:
            flash_html += f'<div class="flash err">{h(flash_err)}</div>'
        page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)} · {APP_NAME}</title><style>{CSS}</style></head>
<body>
<header><div class="brand">{APP_NAME}</div><nav>{nav}</nav></header>
<main>{flash_html}{body}</main>
</body></html>"""
        return page.encode("utf-8")

    def session_token(self, headers: dict[str, str]) -> Optional[str]:
        cookie = SimpleCookie()
        if "cookie" in headers:
            cookie.load(headers["cookie"])
        morsel = cookie.get("mr_session")
        return morsel.value if morsel else None

    def valid_session(self, token: Optional[str]) -> bool:
        if not token:
            return False
        conn = self.app.db.connect()
        try:
            row = conn.execute(
                "SELECT expires_at FROM sessions WHERE token = ?", (token,)
            ).fetchone()
            if not row:
                return False
            exp = datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            if exp < utc_now():
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                conn.commit()
                return False
            return True
        finally:
            conn.close()

    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        exp = utc_now() + timedelta(hours=12)
        conn = self.app.db.connect()
        try:
            conn.execute(
                "INSERT INTO sessions (token, created_at, expires_at) VALUES (?, ?, ?)",
                (token, utc_now_iso(), exp.strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
            # prune
            conn.execute(
                "DELETE FROM sessions WHERE expires_at < ?", (utc_now_iso(),)
            )
            conn.commit()
        finally:
            conn.close()
        return token

    def destroy_session(self, token: Optional[str]) -> None:
        if not token:
            return
        conn = self.app.db.connect()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await self._handle(reader, writer)
        except Exception:
            log.exception("panel error")
            try:
                writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        req_line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not req_line:
            return
        parts = req_line.decode("utf-8", errors="replace").strip().split()
        if len(parts) < 2:
            return
        method, target = parts[0].upper(), parts[1]
        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=30)
            if line in (b"\r\n", b"\n", b""):
                break
            if b":" in line:
                k, v = line.decode("utf-8", errors="replace").split(":", 1)
                headers[k.strip().lower()] = v.strip()

        length = int(headers.get("content-length", "0") or "0")
        body = b""
        if length > 0:
            body = await reader.readexactly(min(length, 1_000_000))

        parsed = urlparse(target)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        form = parse_qs(body.decode("utf-8", errors="replace")) if body else {}

        def form_get(name: str, default: str = "") -> str:
            vals = form.get(name) or query.get(name) or []
            return vals[0] if vals else default

        token = self.session_token(headers)
        authed = self.valid_session(token)

        # Routes
        if path == "/login":
            if method == "POST":
                password = form_get("password")
                conn = self.app.db.connect()
                try:
                    row = conn.execute("SELECT password_hash FROM admin WHERE id=1").fetchone()
                finally:
                    conn.close()
                # crude rate limit via events
                if row and verify_secret(password, row["password_hash"]):
                    new_tok = self.create_session()
                    self.app.db.event("login", "ok")
                    resp_body = b""
                    headers_out = (
                        "HTTP/1.1 303 See Other\r\n"
                        "Location: /\r\n"
                        f"Set-Cookie: mr_session={new_tok}; HttpOnly; SameSite=Strict; Path=/\r\n"
                        "Content-Length: 0\r\n\r\n"
                    )
                    writer.write(headers_out.encode())
                    await writer.drain()
                    return
                self.app.db.event("login_fail", f"peer={writer.get_extra_info('peername')}")
                page = self.layout(
                    "Login",
                    self._login_form(),
                    user_ok=False,
                    flash_err="Invalid password",
                )
                await self._respond(writer, page)
                return
            page = self.layout("Login", self._login_form(), user_ok=False)
            await self._respond(writer, page)
            return

        if path == "/logout":
            self.destroy_session(token)
            headers_out = (
                "HTTP/1.1 303 See Other\r\nLocation: /login\r\n"
                "Set-Cookie: mr_session=; Max-Age=0; Path=/\r\nContent-Length: 0\r\n\r\n"
            )
            writer.write(headers_out.encode())
            await writer.drain()
            return

        if not authed:
            writer.write(b"HTTP/1.1 303 See Other\r\nLocation: /login\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return

        flash = form_get("_flash")
        flash_err = form_get("_flash_err")

        if path == "/" and method == "GET":
            await self._respond(writer, self.layout("Dashboard", self._dashboard(), flash=flash, flash_err=flash_err))
            return
        if path == "/senders":
            await self._senders(writer, method, form)
            return
        if path == "/listen":
            await self._listen(writer, method, form)
            return
        if path == "/rules":
            await self._rules(writer, method, form, query)
            return
        if path == "/domains":
            await self._domains(writer, method, form)
            return
        if path == "/events":
            await self._respond(writer, self.layout("Events", self._events()))
            return
        if path == "/settings":
            await self._settings(writer, method, form)
            return

        await self._respond(writer, self.layout("Not found", "<div class='card'>Not found</div>"), status=404)

    async def _respond(
        self, writer: asyncio.StreamWriter, body: bytes, status: int = 200, extra_headers: str = ""
    ) -> None:
        reason = HTTPStatus(status).phrase
        hdr = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"X-Frame-Options: DENY\r\n"
            f"X-Content-Type-Options: nosniff\r\n"
            f"Referrer-Policy: no-referrer\r\n"
            f"Cache-Control: no-store\r\n"
            f"{extra_headers}"
            f"\r\n"
        )
        writer.write(hdr.encode() + body)
        await writer.drain()

    def _login_form(self) -> str:
        return """
        <div class="card" style="max-width:360px;margin:3rem auto;">
          <h1>Admin login</h1>
          <p class="muted">No outbound compose — configure senders and forward rules only.</p>
          <form method="post" action="/login">
            <label>Password <input type="password" name="password" required autofocus></label>
            <p style="margin-top:0.8rem"><button type="submit">Sign in</button></p>
          </form>
        </div>"""

    def _dashboard(self) -> str:
        conn = self.app.db.connect()
        try:
            senders = conn.execute("SELECT COUNT(*) c FROM senders").fetchone()["c"]
            listen = conn.execute("SELECT COUNT(*) c FROM listen_addresses WHERE enabled=1").fetchone()["c"]
            rules = conn.execute("SELECT COUNT(*) c FROM rules WHERE enabled=1").fetchone()["c"]
            q = conn.execute("SELECT COUNT(*) c FROM queue").fetchone()["c"]
            recent = conn.execute(
                "SELECT ts, kind, detail FROM events ORDER BY id DESC LIMIT 15"
            ).fetchall()
        finally:
            conn.close()
        rows = "".join(
            f"<tr><td>{h(r['ts'])}</td><td>{h(r['kind'])}</td><td>{h(r['detail'])}</td></tr>"
            for r in recent
        )
        return f"""
        <div class="card">
          <h1>Dashboard</h1>
          <p class="muted">{h(self.app.cfg.mail_hostname)} · v{VERSION}</p>
          <p>Senders: <b>{senders}</b> · Listen addresses: <b>{listen}</b> · Rules: <b>{rules}</b> · Queue: <b>{q}</b></p>
          <p class="muted">Inbound SMTP :{self.app.cfg.smtp_port} · Submission :{self.app.cfg.submission_port} · Panel {self.app.cfg.panel_bind}:{self.app.cfg.panel_port}</p>
        </div>
        <div class="card">
          <h2>Recent events</h2>
          <table><thead><tr><th>Time</th><th>Kind</th><th>Detail</th></tr></thead><tbody>{rows or '<tr><td colspan=3 class=muted>None yet</td></tr>'}</tbody></table>
        </div>"""

    async def _senders(self, writer: asyncio.StreamWriter, method: str, form: dict) -> None:
        flash = flash_err = ""
        new_token_show = ""
        if method == "POST":
            action = (form.get("action") or [""])[0]
            conn = self.app.db.connect()
            try:
                if action == "add":
                    email = normalize_email((form.get("email") or [""])[0])
                    note = (form.get("note") or [""])[0][:200]
                    if "@" not in email:
                        flash_err = "Invalid email"
                    else:
                        token = gen_token()
                        conn.execute(
                            """
                            INSERT INTO senders (email, token_hash, token_hint, enabled, note, created_at)
                            VALUES (?, ?, ?, 1, ?, ?)
                            """,
                            (email, hash_secret(token), token[-4:], note, utc_now_iso()),
                        )
                        conn.commit()
                        flash = f"Sender {email} created"
                        new_token_show = token
                        self.app.db.event("sender_add", email)
                elif action == "rotate":
                    sid = int((form.get("id") or ["0"])[0])
                    token = gen_token()
                    conn.execute(
                        "UPDATE senders SET token_hash=?, token_hint=? WHERE id=?",
                        (hash_secret(token), token[-4:], sid),
                    )
                    conn.commit()
                    flash = "Token rotated — copy it now; it will not be shown again"
                    new_token_show = token
                elif action == "toggle":
                    sid = int((form.get("id") or ["0"])[0])
                    conn.execute(
                        "UPDATE senders SET enabled = 1 - enabled WHERE id=?", (sid,)
                    )
                    conn.commit()
                    flash = "Updated"
                elif action == "delete":
                    sid = int((form.get("id") or ["0"])[0])
                    conn.execute("DELETE FROM senders WHERE id=?", (sid,))
                    conn.commit()
                    flash = "Deleted"
            except sqlite3.IntegrityError:
                flash_err = "Sender already exists"
            finally:
                conn.close()

        conn = self.app.db.connect()
        try:
            rows = conn.execute(
                "SELECT id, email, token_hint, enabled, note, created_at FROM senders ORDER BY email"
            ).fetchall()
        finally:
            conn.close()
        table = "".join(
            f"""<tr>
            <td>{h(r['email'])}<div class="muted">…{h(r['token_hint'])}</div></td>
            <td>{'on' if r['enabled'] else 'off'}</td>
            <td>{h(r['note'])}</td>
            <td>
              <form method="post" style="display:inline"><input type="hidden" name="action" value="rotate"><input type="hidden" name="id" value="{r['id']}"><button class="secondary" type="submit">Rotate token</button></form>
              <form method="post" style="display:inline"><input type="hidden" name="action" value="toggle"><input type="hidden" name="id" value="{r['id']}"><button class="secondary" type="submit">Toggle</button></form>
              <form method="post" style="display:inline" onsubmit="return confirm('Delete sender?')"><input type="hidden" name="action" value="delete"><input type="hidden" name="id" value="{r['id']}"><button class="danger" type="submit">Delete</button></form>
            </td></tr>"""
            for r in rows
        )
        token_box = (
            f'<div class="card"><h2>New token (copy now)</h2><div class="token">{h(new_token_show)}</div>'
            f'<p class="muted">SMTP host: {h(self.app.cfg.mail_hostname)} · port {self.app.cfg.submission_port} · STARTTLS · username = sender email · password = token · From must match username</p></div>'
            if new_token_show else ""
        )
        body = f"""
        {token_box}
        <div class="card">
          <h1>Senders</h1>
          <p class="muted">SMTP AUTH accounts for apps. Tokens are stored hashed; plaintext is shown only once.</p>
          <form method="post" class="row">
            <input type="hidden" name="action" value="add">
            <label>Email <input name="email" type="email" required placeholder="noreply@example.com"></label>
            <label>Note <input name="note" placeholder="optional"></label>
            <button type="submit">Add sender</button>
          </form>
        </div>
        <div class="card">
          <table><thead><tr><th>Email / hint</th><th>Enabled</th><th>Note</th><th></th></tr></thead>
          <tbody>{table or '<tr><td colspan=4 class=muted>No senders</td></tr>'}</tbody></table>
        </div>"""
        await self._respond(writer, self.layout("Senders", body, flash=flash, flash_err=flash_err))

    async def _listen(self, writer: asyncio.StreamWriter, method: str, form: dict) -> None:
        flash = flash_err = ""
        if method == "POST":
            action = (form.get("action") or [""])[0]
            conn = self.app.db.connect()
            try:
                if action == "add":
                    address = (form.get("address") or [""])[0].strip().lower()
                    note = (form.get("note") or [""])[0][:200]
                    if not address or ("@" not in address and address != "*"):
                        flash_err = "Use user@domain or *@domain"
                    else:
                        conn.execute(
                            "INSERT INTO listen_addresses (address, enabled, note, created_at) VALUES (?,1,?,?)",
                            (address, note, utc_now_iso()),
                        )
                        conn.commit()
                        flash = "Listen address added"
                elif action == "toggle":
                    conn.execute(
                        "UPDATE listen_addresses SET enabled=1-enabled WHERE id=?",
                        (int((form.get("id") or ["0"])[0]),),
                    )
                    conn.commit()
                elif action == "delete":
                    conn.execute(
                        "DELETE FROM listen_addresses WHERE id=?",
                        (int((form.get("id") or ["0"])[0]),),
                    )
                    conn.commit()
                    flash = "Deleted"
            except sqlite3.IntegrityError:
                flash_err = "Address already exists"
            finally:
                conn.close()

        conn = self.app.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM listen_addresses ORDER BY address"
            ).fetchall()
        finally:
            conn.close()
        table = "".join(
            f"""<tr><td>{h(r['address'])}</td><td>{'on' if r['enabled'] else 'off'}</td><td>{h(r['note'])}</td>
            <td>
            <form method="post" style="display:inline"><input type="hidden" name="action" value="toggle"><input type="hidden" name="id" value="{r['id']}"><button class="secondary">Toggle</button></form>
            <form method="post" style="display:inline" onsubmit="return confirm('Delete?')"><input type="hidden" name="action" value="delete"><input type="hidden" name="id" value="{r['id']}"><button class="danger">Delete</button></form>
            </td></tr>"""
            for r in rows
        )
        body = f"""
        <div class="card">
          <h1>Listen addresses</h1>
          <p class="muted">Inbound RCPT addresses this host accepts for forwarding. Point MX here. Patterns: <code>user@domain</code> or <code>*@domain</code>.
          Tip: for each domain you forward, add <code>*@that-domain</code>. Rules that match on <code>to</code> also check the SMTP envelope recipient.</p>
          <form method="post" class="row">
            <input type="hidden" name="action" value="add">
            <label>Address <input name="address" required placeholder="support@example.com"></label>
            <label>Note <input name="note"></label>
            <button type="submit">Add</button>
          </form>
        </div>
        <div class="card"><table><thead><tr><th>Address</th><th>Enabled</th><th>Note</th><th></th></tr></thead>
        <tbody>{table or '<tr><td colspan=4 class=muted>None</td></tr>'}</tbody></table></div>"""
        await self._respond(writer, self.layout("Listen", body, flash=flash, flash_err=flash_err))

    async def _rules(
        self, writer: asyncio.StreamWriter, method: str, form: dict, query: dict
    ) -> None:
        flash = flash_err = ""
        edit_id = int((query.get("edit") or ["0"])[0] or 0)

        if method == "POST":
            action = (form.get("action") or [""])[0]
            conn = self.app.db.connect()
            try:
                if action in ("add", "update"):
                    name = (form.get("name") or [""])[0].strip() or "Rule"
                    priority = int((form.get("priority") or ["100"])[0] or 100)
                    match_mode = (form.get("match_mode") or ["all"])[0]
                    stop_on_match = 1 if (form.get("stop_on_match") or [""])[0] == "1" else 0
                    dests_raw = (form.get("destinations") or [""])[0]
                    dests = [
                        normalize_email(x)
                        for x in re.split(r"[\s,;]+", dests_raw)
                        if normalize_email(x)
                    ]
                    fields = form.get("cond_field") or []
                    ops = form.get("cond_op") or []
                    vals = form.get("cond_value") or []
                    cases = form.get("cond_case") or []
                    if action == "add":
                        cur = conn.execute(
                            """
                            INSERT INTO rules (name, enabled, priority, match_mode, stop_on_match, created_at)
                            VALUES (?,1,?,?,?,?)
                            """,
                            (name, priority, match_mode, stop_on_match, utc_now_iso()),
                        )
                        rid = cur.lastrowid
                    else:
                        rid = int((form.get("id") or ["0"])[0])
                        conn.execute(
                            """
                            UPDATE rules SET name=?, priority=?, match_mode=?, stop_on_match=?
                            WHERE id=?
                            """,
                            (name, priority, match_mode, stop_on_match, rid),
                        )
                        conn.execute("DELETE FROM rule_conditions WHERE rule_id=?", (rid,))
                        conn.execute("DELETE FROM rule_destinations WHERE rule_id=?", (rid,))
                    for i, field in enumerate(fields):
                        field = (field or "").strip()
                        if not field:
                            continue
                        op = ops[i] if i < len(ops) else "contains"
                        val = vals[i] if i < len(vals) else ""
                        # Parallel select fields keep indices aligned (unlike checkboxes)
                        case = 1 if (i < len(cases) and cases[i] == "1") else 0
                        if op not in OPS:
                            op = "contains"
                        conn.execute(
                            """
                            INSERT INTO rule_conditions (rule_id, field, op, value, case_sensitive)
                            VALUES (?,?,?,?,?)
                            """,
                            (rid, field, op, val, case),
                        )
                    for d in dests:
                        conn.execute(
                            "INSERT INTO rule_destinations (rule_id, email) VALUES (?,?)",
                            (rid, d),
                        )
                    conn.commit()
                    flash = "Rule saved"
                    edit_id = 0
                elif action == "toggle":
                    conn.execute(
                        "UPDATE rules SET enabled=1-enabled WHERE id=?",
                        (int((form.get("id") or ["0"])[0]),),
                    )
                    conn.commit()
                elif action == "delete":
                    conn.execute(
                        "DELETE FROM rules WHERE id=?",
                        (int((form.get("id") or ["0"])[0]),),
                    )
                    conn.commit()
                    flash = "Rule deleted"
            finally:
                conn.close()

        conn = self.app.db.connect()
        try:
            rules = conn.execute(
                "SELECT * FROM rules ORDER BY priority ASC, id ASC"
            ).fetchall()
            edit_rule = None
            edit_conds: list = []
            edit_dests = ""
            if edit_id:
                edit_rule = conn.execute(
                    "SELECT * FROM rules WHERE id=?", (edit_id,)
                ).fetchone()
                edit_conds = conn.execute(
                    "SELECT * FROM rule_conditions WHERE rule_id=?", (edit_id,)
                ).fetchall()
                edit_dests = ", ".join(
                    r["email"]
                    for r in conn.execute(
                        "SELECT email FROM rule_destinations WHERE rule_id=?", (edit_id,)
                    ).fetchall()
                )
        finally:
            conn.close()

        field_opts = [
            "from", "to", "cc", "subject", "body", "reply-to",
            "envelope_from", "envelope_to",
            "header:X-Priority", "header:List-Id",
        ]
        op_opts = sorted(OPS)

        def cond_row(field="", op="contains", value="", case=False) -> str:
            fo = "".join(
                f'<option value="{h(f)}"{" selected" if f==field else ""}>{h(f)}</option>'
                for f in field_opts
            )
            oo = "".join(
                f'<option value="{h(o)}"{" selected" if o==op else ""}>{h(o)}</option>'
                for o in op_opts
            )
            return f"""<div class="cond">
              <select name="cond_field"><option value="">—</option>{fo}</select>
              <select name="cond_op">{oo}</select>
              <input name="cond_value" value="{h(value)}" placeholder="value">
              <select name="cond_case" title="Case sensitive">
                <option value="0" {"selected" if not case else ""}>ignore case</option>
                <option value="1" {"selected" if case else ""}>match case</option>
              </select>
            </div>"""

        if edit_conds:
            conds_html = "".join(
                cond_row(c["field"], c["op"], c["value"], bool(c["case_sensitive"]))
                for c in edit_conds
            )
        else:
            conds_html = cond_row() + cond_row()

        # empty rows for new conditions
        conds_html += cond_row() + cond_row()

        form_title = "Edit rule" if edit_rule else "New rule"
        rid_field = (
            f'<input type="hidden" name="id" value="{edit_rule["id"]}">'
            if edit_rule else ""
        )
        action_val = "update" if edit_rule else "add"
        body_form = f"""
        <div class="card">
          <h1>{h(form_title)}</h1>
          <p class="muted">Empty condition list = always match. Match mode all/any. Lower priority number runs first.</p>
          <form method="post">
            <input type="hidden" name="action" value="{action_val}">
            {rid_field}
            <div class="row">
              <label>Name <input name="name" required value="{h(edit_rule['name'] if edit_rule else '')}"></label>
              <label>Priority <input name="priority" type="number" value="{h(edit_rule['priority'] if edit_rule else 100)}"></label>
              <label>Match mode
                <select name="match_mode">
                  <option value="all" {"selected" if not edit_rule or edit_rule['match_mode']=='all' else ""}>all conditions</option>
                  <option value="any" {"selected" if edit_rule and edit_rule['match_mode']=='any' else ""}>any condition</option>
                </select>
              </label>
              <label style="flex-direction:row;align-items:center">
                <input type="checkbox" name="stop_on_match" value="1" {"checked" if not edit_rule or edit_rule['stop_on_match'] else ""}>
                Stop on match
              </label>
            </div>
            <h2 style="margin-top:1rem">Conditions</h2>
            {conds_html}
            <h2 style="margin-top:1rem">Forward to</h2>
            <label>Destinations (comma-separated)
              <textarea name="destinations" required placeholder="you@example.com, other@example.com">{h(edit_dests)}</textarea>
            </label>
            <p style="margin-top:0.8rem"><button type="submit">Save rule</button>
            {"<a class='btn secondary' href='/rules'>Cancel</a>" if edit_rule else ""}</p>
          </form>
        </div>"""

        # list
        conn = self.app.db.connect()
        try:
            list_html_parts = []
            for r in rules:
                conds = conn.execute(
                    "SELECT field, op, value FROM rule_conditions WHERE rule_id=?",
                    (r["id"],),
                ).fetchall()
                dests = conn.execute(
                    "SELECT email FROM rule_destinations WHERE rule_id=?",
                    (r["id"],),
                ).fetchall()
                ctxt = "; ".join(f"{c['field']} {c['op']} {c['value']}" for c in conds) or "(always)"
                dtxt = ", ".join(d["email"] for d in dests)
                list_html_parts.append(
                    f"""<tr>
                    <td>{r['priority']}</td>
                    <td>{h(r['name'])}<div class="muted">{h(ctxt)}</div></td>
                    <td>{h(dtxt)}</td>
                    <td>{'on' if r['enabled'] else 'off'}</td>
                    <td>
                      <a class="btn secondary" href="/rules?edit={r['id']}">Edit</a>
                      <form method="post" style="display:inline"><input type="hidden" name="action" value="toggle"><input type="hidden" name="id" value="{r['id']}"><button class="secondary">Toggle</button></form>
                      <form method="post" style="display:inline" onsubmit="return confirm('Delete rule?')"><input type="hidden" name="action" value="delete"><input type="hidden" name="id" value="{r['id']}"><button class="danger">Delete</button></form>
                    </td></tr>"""
                )
        finally:
            conn.close()

        body = body_form + f"""
        <div class="card">
          <h2>Rules</h2>
          <table><thead><tr><th>Pri</th><th>Name / conditions</th><th>Destinations</th><th>Enabled</th><th></th></tr></thead>
          <tbody>{''.join(list_html_parts) or '<tr><td colspan=5 class=muted>No rules</td></tr>'}</tbody></table>
        </div>"""
        await self._respond(writer, self.layout("Rules", body, flash=flash, flash_err=flash_err))

    async def _domains(self, writer: asyncio.StreamWriter, method: str, form: dict) -> None:
        flash = flash_err = ""
        if method == "POST":
            action = (form.get("action") or [""])[0]
            domain = (form.get("domain") or [""])[0].strip().lower()
            conn = self.app.db.connect()
            try:
                if action == "add":
                    if not domain or "." not in domain:
                        flash_err = "Invalid domain"
                    else:
                        try:
                            priv, dns = generate_dkim_keypair(domain)
                            conn.execute(
                                """
                                INSERT INTO domains (domain, dkim_selector, private_key_pem, dns_txt, created_at)
                                VALUES (?, 'mail', ?, ?, ?)
                                """,
                                (domain, priv, dns, utc_now_iso()),
                            )
                            conn.commit()
                            flash = f"DKIM key created for {domain}"
                        except Exception as exc:
                            flash_err = str(exc)
                elif action == "delete":
                    conn.execute("DELETE FROM domains WHERE domain=?", (domain,))
                    conn.commit()
                    flash = "Domain removed"
            except sqlite3.IntegrityError:
                flash_err = "Domain already exists"
            finally:
                conn.close()

        conn = self.app.db.connect()
        try:
            rows = conn.execute(
                "SELECT domain, dkim_selector, dns_txt, created_at FROM domains ORDER BY domain"
            ).fetchall()
        finally:
            conn.close()

        host = self.app.cfg.mail_hostname
        server_ip = self.app.server_ip or detect_server_ipv4(host, self.app.cfg.public_ip)
        ip_label = server_ip or "YOUR_SERVER_IP"
        ip_note = (
            f"Detected server IP: <code>{h(server_ip)}</code>."
            if server_ip
            else "Could not auto-detect IP — set <code>PUBLIC_IP</code> in env or replace "
            "<code>YOUR_SERVER_IP</code> manually."
        )
        spf_new = f"v=spf1 a:{host} ip4:{ip_label} -all"
        spf_merge = f"v=spf1 include:_spf.mx.cloudflare.net a:{host} ip4:{ip_label} ~all"
        dmarc_val = "v=DMARC1; p=none;"
        dmarc_rua = "v=DMARC1; p=none; rua=mailto:dmarc@YOUR_DOMAIN;"

        guide = f"""
        <div class="card">
          <h1>DNS guide (SPF / DKIM / DMARC)</h1>
          <p class="muted">
            Mail hostname: <code>{h(host)}</code>.
            {ip_note}
            Do <b>not</b> create two SPF TXT records on one name — merge into a single SPF.
          </p>

          <h2>1) SPF — TXT on the sending domain apex (e.g. <code>example.com</code>)</h2>
          <p class="muted">If the domain has <b>no</b> SPF yet:</p>
          <div class="token">{h(spf_new)}</div>
          <p class="muted" style="margin-top:0.75rem">If SPF already exists (e.g. Cloudflare email), edit that same record and add
          <code>a:{h(host)}</code> and <code>ip4:{h(ip_label)}</code>. Example merge:</p>
          <div class="token">{h(spf_merge)}</div>

          <h2 style="margin-top:1rem">2) DMARC — TXT name <code>_dmarc</code> on the domain</h2>
          <p class="muted">Start monitoring-only (recommended):</p>
          <div class="token">{h(dmarc_val)}</div>
          <p class="muted" style="margin-top:0.75rem">Optional: receive aggregate reports:</p>
          <div class="token">{h(dmarc_rua)}</div>
          <p class="muted">Later you can tighten to <code>p=quarantine</code> or <code>p=reject</code> once mail looks clean.</p>

          <h2 style="margin-top:1rem">3) DKIM</h2>
          <p class="muted">Generate a key below; publish the TXT value at
          <code>mail._domainkey.YOUR_DOMAIN</code>. Wait for DNS propagation, then send a test.</p>

          <h2 style="margin-top:1rem">Also check</h2>
          <ul class="muted">
            <li><code>A</code> for <code>{h(host)}</code> → <code>{h(ip_label)}</code> (DNS only / grey cloud if Cloudflare)</li>
            <li>PTR (reverse DNS): <code>{h(ip_label)}</code> → <code>{h(host)}</code></li>
            <li>For receiving / forwarders: <code>MX</code> for the domain → <code>{h(host)}</code></li>
          </ul>
        </div>
        """

        blocks = []
        for r in rows:
            dom = r["domain"]
            sel = r["dkim_selector"]
            blocks.append(
                f"""<div class="card">
                <h2>{h(dom)}</h2>
                <p><b>DKIM</b> — TXT name: <code>{h(sel)}._domainkey.{h(dom)}</code></p>
                <div class="token">{h(r['dns_txt'])}</div>
                <p style="margin-top:0.75rem"><b>SPF</b> — TXT on <code>{h(dom)}</code> (or merge into existing SPF):</p>
                <div class="token">{h(f'v=spf1 a:{host} ip4:{ip_label} -all')}</div>
                <p style="margin-top:0.75rem"><b>DMARC</b> — TXT name: <code>_dmarc.{h(dom)}</code></p>
                <div class="token">{h(f'v=DMARC1; p=none;')}</div>
                <p class="muted" style="margin-top:0.5rem">Optional reports:
                <code>{h(f'v=DMARC1; p=none; rua=mailto:dmarc@{dom};')}</code></p>
                <form method="post" onsubmit="return confirm('Delete DKIM for domain?')" style="margin-top:0.8rem">
                  <input type="hidden" name="action" value="delete">
                  <input type="hidden" name="domain" value="{h(dom)}">
                  <button class="danger" type="submit">Remove</button>
                </form>
                </div>"""
            )

        body = f"""
        {guide}
        <div class="card">
          <h1>Generate DKIM</h1>
          <p class="muted">Optional signing keys for outbound submission and forwards
          (SRS envelope uses <code>{h(host)}</code>).</p>
          <form method="post" class="row">
            <input type="hidden" name="action" value="add">
            <label>Domain <input name="domain" required placeholder="example.com"></label>
            <button type="submit">Generate DKIM</button>
          </form>
        </div>
        {''.join(blocks) or '<div class="card muted">No DKIM domains yet — SPF/DMARC above still apply to every sending domain.</div>'}
        """
        await self._respond(writer, self.layout("Domains", body, flash=flash, flash_err=flash_err))

    def _events(self) -> str:
        conn = self.app.db.connect()
        try:
            rows = conn.execute(
                "SELECT ts, kind, detail FROM events ORDER BY id DESC LIMIT 200"
            ).fetchall()
            qrows = conn.execute(
                "SELECT id, created_at, attempts, envelope_from, envelope_to, last_error FROM queue ORDER BY id DESC LIMIT 50"
            ).fetchall()
        finally:
            conn.close()
        er = "".join(
            f"<tr><td>{h(r['ts'])}</td><td>{h(r['kind'])}</td><td>{h(r['detail'])}</td></tr>"
            for r in rows
        )
        qr = "".join(
            f"<tr><td>{r['id']}</td><td>{h(r['created_at'])}</td><td>{r['attempts']}</td>"
            f"<td>{h(r['envelope_from'])} → {h(r['envelope_to'])}</td><td>{h(r['last_error'])}</td></tr>"
            for r in qrows
        )
        return f"""
        <div class="card"><h1>Events</h1>
        <table><thead><tr><th>Time</th><th>Kind</th><th>Detail</th></tr></thead>
        <tbody>{er or '<tr><td colspan=3 class=muted>Empty</td></tr>'}</tbody></table></div>
        <div class="card"><h2>Delivery queue</h2>
        <table><thead><tr><th>ID</th><th>Created</th><th>Tries</th><th>Envelope</th><th>Last error</th></tr></thead>
        <tbody>{qr or '<tr><td colspan=5 class=muted>Empty</td></tr>'}</tbody></table></div>
        """

    async def _settings(self, writer: asyncio.StreamWriter, method: str, form: dict) -> None:
        flash = flash_err = ""
        if method == "POST":
            p1 = (form.get("password") or [""])[0]
            p2 = (form.get("password2") or [""])[0]
            if len(p1) < 12:
                flash_err = "Password must be at least 12 characters"
            elif p1 != p2:
                flash_err = "Passwords do not match"
            else:
                conn = self.app.db.connect()
                try:
                    conn.execute(
                        "UPDATE admin SET password_hash=?, updated_at=? WHERE id=1",
                        (hash_secret(p1), utc_now_iso()),
                    )
                    conn.execute("DELETE FROM sessions")
                    conn.commit()
                    flash = "Password updated — please log in again"
                finally:
                    conn.close()
                # force re-login
                page = self.layout(
                    "Settings",
                    '<div class="card"><p>Password updated. <a href="/login">Log in</a></p></div>',
                    user_ok=False,
                    flash=flash,
                )
                extra = "Set-Cookie: mr_session=; Max-Age=0; Path=/\r\n"
                await self._respond(writer, page, extra_headers=extra)
                return

        body = f"""
        <div class="card">
          <h1>Settings</h1>
          <p class="muted">Hostname: {h(self.app.cfg.mail_hostname)} · data: {h(self.app.cfg.data_dir)}</p>
          <form method="post" class="row">
            <label>New admin password <input type="password" name="password" required minlength="12"></label>
            <label>Confirm <input type="password" name="password2" required minlength="12"></label>
            <button type="submit">Update password</button>
          </form>
        </div>"""
        await self._respond(writer, self.layout("Settings", body, flash=flash, flash_err=flash_err))


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.senders = SenderStore(self.db, cfg)
        self.rules = RuleEngine(self.db)
        self.mailer = Mailer(cfg, self.db)
        self.panel = Panel(self)
        self.server_ip = detect_server_ipv4(cfg.mail_hostname, cfg.public_ip)
        if self.server_ip:
            log.info("detected server IPv4 for SPF hints: %s", self.server_ip)
        else:
            log.warning("could not detect server IPv4 — set PUBLIC_IP in env for SPF hints")
        if cfg.has_tls:
            log.info("TLS enabled for STARTTLS: cert=%s", cfg.tls_cert_file)
        else:
            log.warning(
                "TLS not configured — SMTP clients on port %s will fail STARTTLS. "
                "Set TLS_CERT_FILE/TLS_KEY_FILE or install certs at "
                "/etc/letsencrypt/live/%s/",
                cfg.submission_port,
                cfg.mail_hostname,
            )

    async def _try_listen(
        self,
        name: str,
        client_connected_cb,
        host: str,
        port: int,
        *,
        required: bool,
    ):
        try:
            srv = await asyncio.start_server(client_connected_cb, host=host, port=port)
            log.info("%s listening on %s:%s", name, host, port)
            return srv
        except OSError as exc:
            log.error("%s failed to bind %s:%s — %s", name, host, port, exc)
            self.db.event("bind_fail", f"{name} {host}:{port} {exc}")
            if required:
                raise
            return None

    async def run(self) -> None:
        self.db.init(self.cfg)
        self.cfg.dkim_dir.mkdir(parents=True, exist_ok=True)
        self.db.event("start", f"{APP_NAME} {VERSION} host={self.cfg.mail_hostname}")
        log.info(
            "starting %s %s hostname=%s data=%s",
            APP_NAME,
            VERSION,
            self.cfg.mail_hostname,
            self.cfg.data_dir,
        )

        servers = []

        # Panel first so admin UI stays up even if SMTP ports are taken
        srv_panel = await self._try_listen(
            "Panel",
            self.panel.handle,
            self.cfg.panel_bind,
            self.cfg.panel_port,
            required=True,
        )
        servers.append(srv_panel)
        log.info(
            "Panel URL: http://%s:%s (SSH tunnel if bind is 127.0.0.1)",
            self.cfg.panel_bind,
            self.cfg.panel_port,
        )

        srv_in = await self._try_listen(
            "SMTP inbound",
            lambda r, w: smtp_client_cb(r, w, self, False),
            self.cfg.smtp_bind,
            self.cfg.smtp_port,
            required=False,
        )
        if srv_in:
            servers.append(srv_in)

        srv_sub = await self._try_listen(
            "SMTP submission",
            lambda r, w: smtp_client_cb(r, w, self, True),
            self.cfg.smtp_bind,
            self.cfg.submission_port,
            required=False,
        )
        if srv_sub:
            servers.append(srv_sub)

        if len(servers) == 1 and not srv_in and not srv_sub:
            log.warning(
                "SMTP ports unavailable — panel is up; fix port conflict then restart"
            )

        worker = asyncio.create_task(queue_worker(self))
        try:
            await asyncio.gather(*(s.serve_forever() for s in servers))
        finally:
            worker.cancel()
            for s in servers:
                s.close()
                await s.wait_closed()


def reset_admin_password(cfg: Config, new_password: str) -> None:
    if len(new_password) < 12:
        raise SystemExit("password must be at least 12 characters")
    db = DB(cfg.db_path)
    db.init(cfg)
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE admin SET password_hash=?, updated_at=? WHERE id=1",
            (hash_secret(new_password), utc_now_iso()),
        )
        if conn.total_changes == 0:
            conn.execute(
                "INSERT INTO admin (id, password_hash, updated_at) VALUES (1, ?, ?)",
                (hash_secret(new_password), utc_now_iso()),
            )
        conn.execute("DELETE FROM sessions")
        db._event(conn, "admin_reset", "password reset via CLI")
        conn.commit()
    finally:
        conn.close()
    print("mail-router: admin password updated; existing sessions cleared")


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_logging()
    try:
        cfg = build_config()
    except Exception:
        log.exception("config failed")
        return 1

    if argv and argv[0] in ("reset-admin", "passwd-admin"):
        password = argv[1] if len(argv) > 1 else cfg.admin_password
        if len(argv) <= 1:
            print(
                "mail-router: using ADMIN_PASSWORD from env "
                f"({cfg.mail_hostname} / {cfg.db_path})"
            )
        try:
            reset_admin_password(cfg, password)
        except SystemExit as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0

    # Privilege note: port 25 usually needs root or cap_net_bind_service
    app = App(cfg)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        log.info("shutting down")
        return 0
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1
    except Exception:
        log.exception("fatal error")
        return 1
    log.error("event loop ended unexpectedly")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
