"""Email+password accounts with scrypt hashing and DB-backed session cookies."""
import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from . import db

SESSION_DAYS = 90
COOKIE = "hsksession"


def hash_password(password):
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def verify_password(password, stored):
    try:
        salt_b64, h_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(h_b64)
        h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(h, expected)
    except Exception:
        return False


# Escalating account lockout: every 3rd consecutive failure locks the account
# for the next period. The ladder TOPS OUT at the longest period rather than
# becoming permanent — with no self-service password reset, a permanent lock is
# an unrecoverable state a user can walk into by mistake, and the only exit is
# the owner running SQL. A 7-day lock stops credential stuffing just as well.
# Admin unlock: DELETE FROM lockouts WHERE email='...';
LOCKOUT_PERIODS = [timedelta(hours=1), timedelta(hours=6), timedelta(days=1), timedelta(days=7)]
LOCKOUT_THRESHOLD = 3


def check_lockout(conn, email):
    """Raise 403 if this account is currently locked."""
    row = conn.execute("SELECT * FROM lockouts WHERE email = ?", (email,)).fetchone()
    if not row:
        return
    if row["permanent"]:
        raise HTTPException(403, "This account is permanently locked after repeated "
                                 "failed logins. Contact the site owner to unlock it.")
    if row["locked_until"]:
        until = datetime.fromisoformat(row["locked_until"])
        if until > datetime.now(timezone.utc):
            mins = int((until - datetime.now(timezone.utc)).total_seconds() // 60) + 1
            raise HTTPException(403, f"Too many failed logins — account locked for "
                                     f"another {mins} min.")


def record_login_failure(conn, email):
    """Count a failure; every LOCKOUT_THRESHOLD-th consecutive one escalates."""
    row = conn.execute("SELECT * FROM lockouts WHERE email = ?", (email,)).fetchone()
    fails = (row["fails"] if row else 0) + 1
    count = row["lockout_count"] if row else 0
    # commit before raising: the caller's `with conn` block rolls back on the
    # HTTPException it (or we) raise, which would silently drop the counters
    if fails < LOCKOUT_THRESHOLD:
        conn.execute("INSERT INTO lockouts(email, fails) VALUES (?,?) "
                     "ON CONFLICT(email) DO UPDATE SET fails = ?", (email, fails, fails))
        conn.commit()
        return
    count += 1
    # Cap at the longest period instead of escalating to permanent.
    period = LOCKOUT_PERIODS[min(count, len(LOCKOUT_PERIODS)) - 1]
    until = (datetime.now(timezone.utc) + period).isoformat()
    conn.execute("INSERT INTO lockouts(email, fails, lockout_count, locked_until) VALUES (?,0,?,?) "
                 "ON CONFLICT(email) DO UPDATE SET fails = 0, lockout_count = ?, locked_until = ?",
                 (email, count, until, count, until))
    conn.commit()
    raise HTTPException(403, f"Too many failed logins — account locked for "
                             f"{_period_label(period)}.")


def clear_login_failures(conn, email):
    conn.execute("DELETE FROM lockouts WHERE email = ?", (email,))


def _period_label(delta):
    hours = int(delta.total_seconds() // 3600)
    return f"{hours} h" if hours < 48 else f"{hours // 24} days"


def create_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    conn.execute("INSERT INTO sessions(token, user_id, expires) VALUES (?,?,?)",
                 (token, user_id, expires))
    return token


def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Not logged in")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT u.id, u.email, s.expires FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ?", (token,)).fetchone()
    if not row or datetime.fromisoformat(row["expires"]) < datetime.now(timezone.utc):
        raise HTTPException(401, "Session expired")
    return {"id": row["id"], "email": row["email"]}
