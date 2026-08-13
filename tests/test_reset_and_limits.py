"""Manual password reset, change-password, captcha gating and the global cap."""
import pytest
from conftest import register

from app import captcha, main


def _no_captcha(monkeypatch):
    monkeypatch.setattr(captcha, "SITE_KEY", "")
    monkeypatch.setattr(captcha, "SECRET", "")


# ---------- reset requests ----------

def test_reset_request_does_not_leak_whether_an_account_exists(client, monkeypatch):
    _no_captcha(monkeypatch)
    register(client)
    client.post("/api/logout")

    a = client.post("/api/password-reset-request", json={"email": "alice@example.com"})
    main._buckets.clear()
    b = client.post("/api/password-reset-request", json={"email": "nobody@example.com"})
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json(), "response must not reveal account existence"


def test_reset_request_records_the_account_link(client, monkeypatch):
    from app import db

    _no_captcha(monkeypatch)
    register(client)
    client.post("/api/logout")
    client.post("/api/password-reset-request",
                json={"email": "alice@example.com", "note": "locked out"})
    with db.connect() as c:
        row = c.execute("SELECT * FROM password_resets").fetchone()
    assert row["user_id"] is not None and row["note"] == "locked out"
    assert row["handled"] is None


def test_reset_request_deduplicates_while_open(client, monkeypatch):
    from app import db

    _no_captcha(monkeypatch)
    register(client)
    client.post("/api/logout")
    for _ in range(3):
        main._buckets.clear()
        client.post("/api/password-reset-request", json={"email": "alice@example.com"})
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM password_resets").fetchone()["c"]
    assert n == 1, "repeat requests must not pile up in the owner's queue"


def test_reset_request_is_rate_limited(client, monkeypatch):
    _no_captcha(monkeypatch)
    codes = [client.post("/api/password-reset-request",
                         json={"email": f"x{i}@example.com"}).status_code
             for i in range(5)]
    assert 429 in codes


def test_reset_request_never_sends_to_a_form_supplied_address(client, monkeypatch):
    """The form takes no reply-to field at all — the reply address comes from
    the users table, so filling in someone else's email can't redirect it."""
    _no_captcha(monkeypatch)
    r = client.post("/api/password-reset-request",
                    json={"email": "victim@example.com",
                          "reply_to": "attacker@example.com"})
    assert r.status_code == 200
    from app import db
    with db.connect() as c:
        cols = {d[1] for d in c.execute("PRAGMA table_info(password_resets)")}
    assert "reply_to" not in cols


# ---------- captcha gating ----------

def test_register_requires_captcha_when_configured(client, monkeypatch):
    monkeypatch.setattr(captcha, "SITE_KEY", "site")
    monkeypatch.setattr(captcha, "SECRET", "secret")
    monkeypatch.setattr(captcha, "verify", lambda tok, ip=None, timeout=8: tok == "good")

    bad = client.post("/api/register", json={"email": "a@b.co", "password": "hunter2secret",
                                             "captcha": "bad"})
    assert bad.status_code == 400
    main._buckets.clear()
    good = client.post("/api/register", json={"email": "a@b.co", "password": "hunter2secret",
                                              "captcha": "good"})
    assert good.status_code == 200


def test_reset_request_requires_captcha_when_configured(client, monkeypatch):
    monkeypatch.setattr(captcha, "SITE_KEY", "site")
    monkeypatch.setattr(captcha, "SECRET", "secret")
    monkeypatch.setattr(captcha, "verify", lambda tok, ip=None, timeout=8: tok == "good")
    r = client.post("/api/password-reset-request",
                    json={"email": "a@b.co", "captcha": "nope"})
    assert r.status_code == 400


def test_captcha_disabled_verifies_nothing(monkeypatch):
    monkeypatch.setattr(captcha, "SITE_KEY", "")
    monkeypatch.setattr(captcha, "SECRET", "")
    assert captcha.enabled() is False
    assert captcha.verify("") is True


def test_captcha_fails_closed_on_network_error(monkeypatch):
    """Canonical behaviour: an unreachable siteverify is a failed challenge,
    so an outage can't be used as a bypass."""
    monkeypatch.setattr(captcha, "SITE_KEY", "s")
    monkeypatch.setattr(captcha, "SECRET", "x")

    def boom(*a, **k):
        raise OSError("cloudflare unreachable")

    monkeypatch.setattr(captcha.urllib.request, "urlopen", boom)
    assert captcha.verify("token") is False


# ---------- change password ----------

def test_change_password_flow(client, monkeypatch):
    _no_captcha(monkeypatch)
    register(client)
    bad = client.post("/api/change-password",
                      json={"current_password": "wrong", "new_password": "newpassword1"})
    assert bad.status_code == 401
    ok = client.post("/api/change-password",
                     json={"current_password": "hunter2secret", "new_password": "newpassword1"})
    assert ok.status_code == 200

    client.post("/api/logout")
    main._buckets.clear()
    assert client.post("/api/login", json={"email": "alice@example.com",
                                           "password": "hunter2secret"}).status_code == 401
    main._buckets.clear()
    assert client.post("/api/login", json={"email": "alice@example.com",
                                           "password": "newpassword1"}).status_code == 200


def test_change_password_rejects_short(client, monkeypatch):
    _no_captcha(monkeypatch)
    register(client)
    r = client.post("/api/change-password",
                    json={"current_password": "hunter2secret", "new_password": "short"})
    assert r.status_code == 400


# ---------- global autofetch cap ----------

def test_global_autofetch_cap_is_independent_of_ip(client, monkeypatch):
    """Per-IP limits don't bound what we do to third parties; this does."""
    main._global_buckets.clear()
    monkeypatch.setattr(main, "AUTOFETCH_GLOBAL_PER_MIN", 2)
    _no_captcha(monkeypatch)
    register(client)
    sid = client.post("/api/songs", json={"title": "曲", "artist": "人"}).json()["id"]

    from app import lyrics_fetch
    monkeypatch.setattr(lyrics_fetch, "resolve_text", lambda a, t: None)

    codes = []
    for i in range(4):
        main._buckets.clear()                      # defeat the per-IP limiter
        codes.append(client.post(f"/api/songs/{sid}/autofetch",
                                 headers={"X-Real-IP": f"10.0.0.{i}"}).status_code)
    assert codes[-1] == 429, "global cap must bite regardless of source IP"


def test_global_limit_window_resets(monkeypatch):
    main._global_buckets.clear()
    main.global_limit("k", 1)
    with pytest.raises(Exception):
        main.global_limit("k", 1)
    main._global_buckets["k"] = (99, 0)             # pretend the window is old
    main.global_limit("k", 1)                       # must not raise
