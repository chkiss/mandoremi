"""Owner notifications — deliberately NOT user-facing email.

There is no transactional email provider. The only mail this app sends goes to
the owner's own inbox, authenticated against the owner's own mailbox
(Gmail submission on 587, which is reachable from this host). That keeps it
free and keeps deliverability a non-issue: we are not trying to convince a
stranger's spam filter, only to reach ourselves.

Mail to *users* is sent by the owner by hand, from their personal address.

Configure (all optional — unset means "log only, don't send"):
    SMTP_HOST      default smtp.gmail.com
    SMTP_PORT      default 587
    SMTP_USER      the owner's address, e.g. you@gmail.com
    SMTP_PASS      a Gmail App Password (requires 2FA on the account)
    OWNER_EMAIL    where to send; defaults to SMTP_USER
"""
import logging
import os
import smtplib
import threading
from email.message import EmailMessage

log = logging.getLogger("mandoremi.notify")

HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
PORT = int(os.environ.get("SMTP_PORT", "587"))
USER = os.environ.get("SMTP_USER", "")
PASS = os.environ.get("SMTP_PASS", "")
OWNER = os.environ.get("OWNER_EMAIL", "") or USER


def enabled():
    return bool(USER and PASS and OWNER)


def _send(subject, body):
    msg = EmailMessage()
    msg["From"] = USER
    msg["To"] = OWNER
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(HOST, PORT, timeout=20) as s:
            s.starttls()
            s.login(USER, PASS)
            s.send_message(msg)
        log.info("owner notification sent: %s", subject)
    except Exception as e:                              # noqa: BLE001
        # Never fail a user's request because our own notification broke; the
        # row is already in password_resets and tools/reset_requests.py lists
        # it regardless of whether this mail arrived.
        log.error("owner notification FAILED (%s): %s", subject, e)


def send_async(subject, body):
    if not enabled():
        log.info("notify disabled; would have sent: %s", subject)
        return
    threading.Thread(target=_send, args=(subject, body), daemon=True).start()


def reset_requested(email, matched):
    send_async(
        f"[Mandoremi] password reset requested: {email}",
        f"A password reset was requested for: {email}\n"
        f"Matches an existing account: {'YES' if matched else 'no'}\n\n"
        f"To action it, on the server:\n"
        f"  cd ~/hsk-lyrics\n"
        f"  ./.venv/bin/python tools/reset_requests.py --list\n"
        f"  ./.venv/bin/python tools/reset_requests.py --reset {email}\n\n"
        f"That prints a new password and the message to send. Send it ONLY to "
        f"the address registered on the account.\n")
