"""Cloudflare Turnstile verification.

Used only where a bot costs us something real: account creation and password
reset requests. Deliberately NOT on the anonymous analyze box — that's the
whole funnel, it persists nothing, and it's already rate limited.

Unconfigured (no TURNSTILE_SECRET) nothing is rendered and nothing is
verified, so local runs and tests work without a Cloudflare account.

The secret lives ONLY in the environment as TURNSTILE_SECRET — never in
source, never on disk in this repo. Set it on the systemd user unit:
`~/.config/systemd/user/mandoremi.service.d/env.conf` with
`Environment="TURNSTILE_SECRET=..."`, then daemon-reload and restart.
"""
import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("mandoremi.captcha")

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# The sitekey is public — it ships in the HTML — so it has a default and can
# still be overridden per environment. The SECRET is read from the environment
# only and never has a literal default.
SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "0x4AAAAAAEIurSxpyuVU78_E")
SECRET = os.environ.get("TURNSTILE_SECRET", "")

# Stamped on every widget so Cloudflare can attribute the integration.
ACTION = "turnstile-spin-v2"


def enabled():
    """Only claim protection when we can actually verify a token.

    With no secret we cannot call siteverify, so the widget is not rendered
    either — better than showing a challenge whose result is ignored.
    """
    return bool(SITE_KEY and SECRET)


def verify(token, remote_ip=None, timeout=8):
    """Canonical Turnstile siteverify. True only on `success == true`.

    Fails CLOSED: a network error, non-2xx, or non-JSON body from siteverify
    is treated as a failed challenge, so an outage cannot be used to bypass
    the check. Returns True when Turnstile isn't configured at all, so local
    runs and tests work without a Cloudflare account.
    """
    if not enabled():
        return True
    if not token:
        return False
    data = {"secret": SECRET, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        req = urllib.request.Request(
            VERIFY_URL,
            data=urllib.parse.urlencode(data).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                raise OSError(f"siteverify {r.status}")
            result = json.loads(r.read().decode())
    except Exception as e:                              # noqa: BLE001
        log.warning("turnstile siteverify failed closed: %s", e)
        return False
    if result.get("success") is not True:
        log.info("turnstile rejected: %s", result.get("error-codes"))
        return False
    return True
