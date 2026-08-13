"""Conformance with the canonical Turnstile integration.

Guards the details that are easy to break silently and hard to notice: the
siteverify request shape, strict success checking, the analytics action tag,
and the rule that the secret never appears in source.
"""
import io
import json
import pathlib

from app import captcha

ROOT = pathlib.Path(__file__).resolve().parent.parent


class _Resp(io.BytesIO):
    status = 200

    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch, payload, status=200):
    """Run verify() against a canned siteverify response, returning the
    request that was actually sent."""
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["body"] = req.data.decode()
        sent["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _Resp(payload, status)

    monkeypatch.setattr(captcha, "SITE_KEY", "site")
    monkeypatch.setattr(captcha, "SECRET", "shhh")
    monkeypatch.setattr(captcha.urllib.request, "urlopen", fake_urlopen)
    return sent


def test_siteverify_request_is_canonical(monkeypatch):
    sent = _capture(monkeypatch, {"success": True})
    assert captcha.verify("tok", "203.0.113.9") is True
    assert sent["url"] == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    assert sent["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert "secret=shhh" in sent["body"]
    assert "response=tok" in sent["body"]
    assert "remoteip=203.0.113.9" in sent["body"]


def test_only_success_true_passes(monkeypatch):
    _capture(monkeypatch, {"success": False, "error-codes": ["invalid-input-response"]})
    assert captcha.verify("tok") is False

    _capture(monkeypatch, {"success": "true"})      # string, not boolean
    assert captcha.verify("tok") is False, "must check success === true, not truthiness"

    _capture(monkeypatch, {})                        # no success field
    assert captcha.verify("tok") is False


def test_non_2xx_fails_closed(monkeypatch):
    _capture(monkeypatch, {"success": True}, status=500)
    assert captcha.verify("tok") is False


def test_empty_token_never_reaches_the_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call siteverify without a token")

    monkeypatch.setattr(captcha, "SITE_KEY", "site")
    monkeypatch.setattr(captcha, "SECRET", "shhh")
    monkeypatch.setattr(captcha.urllib.request, "urlopen", boom)
    assert captcha.verify("") is False


def test_action_tag_is_stamped_on_every_widget():
    """Cloudflare attributes the integration by data-action."""
    assert captcha.ACTION == "turnstile-spin-v2"
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'div.className = "cf-turnstile"' in js
    assert 'data-action' in js and "turnstile-spin-v2" in js


def test_widget_only_renders_when_verification_is_possible(monkeypatch):
    monkeypatch.setattr(captcha, "SITE_KEY", "site")
    monkeypatch.setattr(captcha, "SECRET", "")
    assert captcha.enabled() is False, "no secret means no widget, not an unchecked one"


def test_secret_is_never_hardcoded():
    """The sitekey is public and may have a default; the secret may not.

    Matches an actual literal assignment — naming the env var in a docstring
    or a systemd snippet is documentation, not a leak.
    """
    import re

    src = (ROOT / "app" / "captcha.py").read_text(encoding="utf-8")
    assert 'os.environ.get("TURNSTILE_SECRET", "")' in src, \
        "the secret must come from the environment with no default"

    literal = re.compile(r"""TURNSTILE_SECRET\s*[:=]\s*["'][^"']{6,}["']""")
    for path in list(ROOT.glob("app/*.py")) + list(ROOT.glob("static/*.js")) + \
            list(ROOT.glob("static/*.html")) + list(ROOT.glob("tools/*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not literal.search(text), f"{path} appears to inline the secret"


def test_sitekey_is_wired_into_the_served_page(client, monkeypatch):
    """With the secret set, the page must ship the sitekey and the CF script."""
    from app import main

    monkeypatch.setattr(captcha, "SECRET", "shhh")
    html = main._page_html("index.html")
    assert captcha.SITE_KEY in html
    assert "challenges.cloudflare.com/turnstile/v0/api.js" in html
    assert captcha.ACTION in html
