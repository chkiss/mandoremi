"""Negative cache: don't re-pay a ~9s lookup we already know fails."""
from conftest import SONG, register

from app import seed


def test_record_and_expiry(conn):
    assert seed.missed(conn, "邓紫棋", "光年之外") is False
    seed.record_miss(conn, "邓紫棋", "光年之外")
    assert seed.missed(conn, "邓紫棋", "光年之外") is True
    # keyed like the corpus: script and spacing don't create a second entry
    assert seed.missed(conn, "鄧紫棋", " 光年之外 ") is True
    # expires, because catalogues gain songs
    assert seed.missed(conn, "邓紫棋", "光年之外", retry_days=0) is False


def test_repeated_misses_increment_tries(conn):
    for _ in range(3):
        seed.record_miss(conn, "a", "曲子")
    row = conn.execute("SELECT tries FROM seed_miss").fetchone()
    assert row["tries"] == 3


def test_success_clears_the_miss(conn):
    from app import analyze

    seed.record_miss(conn, "邓紫棋", "光年之外")
    ghost = analyze.strip_text(analyze.analyze(SONG))
    seed.store(conn, "邓紫棋", "光年之外", ghost, "h")
    seed.clear_miss(conn, "邓紫棋", "光年之外")
    assert seed.missed(conn, "邓紫棋", "光年之外") is False


def test_autofetch_records_and_then_skips_the_fetch(client, monkeypatch):
    """Second click must 404 without calling out again."""
    from app import lyrics_fetch, main

    calls = []

    def fake_resolve(artist, title):
        calls.append((artist, title))
        return None                       # nothing found upstream

    monkeypatch.setattr(lyrics_fetch, "resolve_text", fake_resolve)
    register(client)
    sid = client.post("/api/songs", json={"title": "无名曲", "artist": "某人"}).json()["id"]

    assert client.post(f"/api/songs/{sid}/autofetch").status_code == 404
    assert len(calls) == 1

    main._buckets.clear()
    assert client.post(f"/api/songs/{sid}/autofetch").status_code == 404
    assert len(calls) == 1, "second attempt must be served from the negative cache"


def test_autofetch_miss_does_not_block_a_later_success(client, monkeypatch):
    """A miss must not poison the song forever once the corpus learns it."""
    from app import analyze, db, lyrics_fetch, main, seed as seedmod

    monkeypatch.setattr(lyrics_fetch, "resolve_text", lambda a, t: None)
    register(client)
    sid = client.post("/api/songs", json={"title": "无名曲", "artist": "某人"}).json()["id"]
    assert client.post(f"/api/songs/{sid}/autofetch").status_code == 404

    ghost = analyze.strip_text(analyze.analyze(SONG))
    with db.connect() as c:
        seedmod.store(c, "某人", "无名曲", ghost, "h")

    main._buckets.clear()
    r = client.post(f"/api/songs/{sid}/autofetch")
    assert r.status_code == 200, "a seeded analysis must win over a stale miss"
