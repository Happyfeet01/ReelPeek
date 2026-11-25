"""Invited accounts are activated only after the mailbox owner chooses a password."""
import hashlib
import hmac
import os
import re
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import urlsplit


def normalize_email(value):
    address = value.strip().lower()
    if len(address) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", address):
        raise ValueError("Bitte eine gültige E-Mail-Adresse eingeben.")
    return address


def password_digest(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**15,
                          r=8, p=1, maxmem=64*1024*1024).hex()


def check_password(password, stored):
    salt, expected = (stored or ("00"*16 + ":" + "00"*64)).split(":")
    return len(password) <= 256 and hmac.compare_digest(password_digest(password, salt), expected)


def provision_owner(db, email):
    with db.connect() as conn:
        conn.execute("UPDATE accounts SET email=? WHERE id=1 AND email IS NULL", (normalize_email(email),))


def issue_link(db, email):
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = int(time.time())
    with db.connect() as conn:
        updated = conn.execute("""UPDATE accounts SET token_hash=?, token_until=?, last_mail=?
            WHERE email=? AND state='pending' AND last_mail<=?""",
            (digest, now + 86400, now, email, now - 60))
    if not updated.rowcount:
        return
    try:
        send_link(email, token)
    except Exception:
        with db.connect() as conn:
            conn.execute("UPDATE accounts SET token_hash=NULL, token_until=NULL, last_mail=0 WHERE token_hash=?", (digest,))
        raise


def send_link(email, token):
    origin = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    url = urlsplit(origin)
    if url.scheme != "https" or not url.hostname or url.path or url.query or url.fragment or url.username:
        raise ValueError("PUBLIC_BASE_URL muss eine HTTPS-Adresse ohne Pfad sein.")
    message = EmailMessage()
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = email
    message["Subject"] = "Deinen ReelPeek-Zugang bestätigen"
    message.set_content(f"Für {email} wurde ein Zugang zu ReelPeek eingeladen.\n\n"
                        f"Hier bestätigst du die Adresse und wählst dein Passwort:\n"
                        f"{origin}/aktivieren#{token}\n\nDer Link gilt 24 Stunden.")
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587")), timeout=15) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if os.getenv("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(message)


def activate(db, token, password):
    if not 12 <= len(password) <= 256:
        raise ValueError("Wähle ein Passwort mit 12 bis 256 Zeichen.")
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = int(time.time())
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM accounts WHERE token_hash=? AND token_until>? AND state='pending'",
                            (digest, now)).fetchone():
            raise ValueError("Der Link ist ungültig oder abgelaufen.")
        salt = secrets.token_hex(16)
        stored = salt + ":" + password_digest(password, salt)
        changed = conn.execute("""UPDATE accounts SET password_hash=?, state='active',
            token_hash=NULL, token_until=NULL WHERE token_hash=? AND token_until>? AND state='pending'""",
            (stored, digest, now))
        if not changed.rowcount:
            raise ValueError("Der Link wurde bereits verwendet.")
