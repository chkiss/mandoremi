"""Shared seed corpus: the mechanism that lets one resolved song serve every
user without sharing lyrics."""
import json

import pytest
from conftest import SONG, register

from app import analyze, artists, seed


def _ghost():
    return analyze.strip_text(analyze.analyze(SONG))


def test_a_new_user_sees_the_corpus_immediately(client):
    """The 0/100 bug: a second account importing songs the corpus already knows
    saw none of them analyzed, because only autofetch ever read the corpus."""
    from app import db, seed

    register(client, email="first@example.com")
    sid = client.post("/api/songs",
                      json={"artist": "周杰伦", "title": "安静"}).json()["id"]
    client.put(f"/api/songs/{sid}/lyrics", json={"text": SONG})
    # publish it to the shared corpus the way a seeding run would
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (sid,)).fetchone()
        seed.store(conn, row["artist"], row["title"],
                   analyze.strip_text(json.loads(row["analysis"])),
                   row["lyrics_hash"])
    client.post("/api/logout")

    register(client, email="second@example.com")
    sid2 = client.post("/api/songs",
                       json={"artist": "周杰伦", "title": "安静"}).json()["id"]
    got = client.get(f"/api/songs/{sid2}").json()
    assert got["analysis"] is not None, "new user should inherit the corpus"
    assert got["analysis"]["stats"]["chinese_tokens"] > 0
    # ...and it must stay text-free: the corpus never carries anyone's lyrics
    assert "lines" not in got["analysis"]
    assert client.get("/api/songs").json()[0]["analyzed"] is True


def test_a_new_user_inherits_across_name_spellings(client):
    """Same song, typed the way Spotify writes it. Needs the alias table --
    without a resolved id the corpus falls back to the raw string key, and
    'jaychou' and '周杰伦' are different keys."""
    from app import db, seed

    register(client, email="a@example.com")
    sid = client.post("/api/songs",
                      json={"artist": "周杰伦", "title": "安静"}).json()["id"]
    client.put(f"/api/songs/{sid}/lyrics", json={"text": SONG})
    with db.connect() as conn:
        # Aliases first, so the corpus row is written WITH its canonical id --
        # which is the state resolve_artists.py leaves production in.
        for k in ("周杰伦", "jaychou"):
            conn.execute("INSERT OR REPLACE INTO artist_alias "
                         "(alias_key, artist_id, display, confidence) "
                         "VALUES (?,?,?,?)", (k, 6452, "周杰伦", "exact"))
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (sid,)).fetchone()
        seed.store(conn, row["artist"], row["title"],
                   analyze.strip_text(json.loads(row["analysis"])),
                   row["lyrics_hash"])
    client.post("/api/logout")

    register(client, email="b@example.com")
    sid2 = client.post("/api/songs",
                       json={"artist": "Jay Chou", "title": "安静"}).json()["id"]
    assert client.get(f"/api/songs/{sid2}").json()["analysis"] is not None


def test_adopting_the_corpus_never_overwrites_your_own_lyrics(client):
    from app import db, seed

    register(client)
    sid = client.post("/api/songs",
                      json={"artist": "周杰伦", "title": "安静"}).json()["id"]
    client.put(f"/api/songs/{sid}/lyrics", json={"text": SONG})
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (sid,)).fetchone()
        assert row["lyrics"]
        before = row["analysis"]
        # a corpus entry for the same song must not displace their own text
        seed.store(conn, row["artist"], row["title"],
                   analyze.strip_text(json.loads(row["analysis"])),
                   "differenthash")
    client.get(f"/api/songs/{sid}")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (sid,)).fetchone()
    assert row["lyrics"], "user's own lyrics were dropped"
    assert row["analysis"] == before


def test_corpus_artist_key_is_exactly_artists_alias_key():
    """These were two copies of one regex and they drifted: the full-width bang
    was added to artists.py for "hush！" but not to seed.py, so the corpus kept
    that artist separate from the 'hush' the alias table had already resolved.
    seed.key() must delegate, not reimplement."""
    for name in ("hush！", "Hush", "周杰伦", "Jay Chou, Gary Yang",
                 "李荣浩，陈坤", "G.E.M. 邓紫棋", "Ronghao Li 陈坤", "周杰倫"):
        assert seed.key(name, "x")[0] == artists.alias_key(name), name


def test_key_normalizes_script_case_and_punctuation():
    # traditional vs simplified, spacing, case and bracketed suffixes must all
    # collapse to the same key or the corpus silently misses.
    assert seed.key("鄧紫棋", "光年之外") == seed.key("邓紫棋", "光年之外")
    assert seed.key("Mayday", "Song Name") == seed.key("mayday", "  song-name ")
    assert seed.key("五月天", "倔強 (Live Version)") == seed.key("五月天", "倔强")


def test_roundtrip_and_version_gate(conn):
    g = _ghost()
    seed.store(conn, "邓紫棋", "光年之外", g, "hash123")
    hit = seed.lookup(conn, "鄧紫棋", "光年之外 ", g["version"])
    assert hit is not None
    got, h = hit
    assert h == "hash123"
    assert got["stats"]["chinese_tokens"] == g["stats"]["chinese_tokens"]
    # a stale analysis must not be served: ghosts have no text to re-analyze
    assert seed.lookup(conn, "邓紫棋", "光年之外", g["version"] + 1) is None
    assert seed.lookup(conn, "邓紫棋", "never seeded", g["version"]) is None


def test_stored_payload_carries_no_lyric_text(conn):
    """The whole licensing argument rests on this: what's shared is data ABOUT
    the song, never the song."""
    g = _ghost()
    seed.store(conn, "邓紫棋", "光年之外", g, "h")
    raw = conn.execute("SELECT analysis FROM seed_analysis").fetchone()["analysis"]
    stored = json.loads(raw)
    assert "lines" not in stored
    for gram in stored.get("grammar", []):
        assert "examples" not in gram and "lines" not in gram
    # no stored line reproduces a lyric line
    for line in [l for l in SONG.splitlines() if l.strip()]:
        assert line not in raw


def test_store_refuses_payload_with_text(conn):
    full = analyze.analyze(SONG)          # un-stripped: still has lines
    with pytest.raises(ValueError):
        seed.store(conn, "a", "b", full, "h")


def test_autofetch_uses_seed_without_fetching(client, monkeypatch):
    """A seeded song must resolve with no network call at all."""
    from app import db, lyrics_fetch, main

    register(client)
    sid = client.post("/api/songs", json={"title": "光年之外", "artist": "邓紫棋"}).json()["id"]

    g = _ghost()
    with db.connect() as c:
        seed.store(c, "邓紫棋", "光年之外", g, "seededhash")

    def boom(*a, **k):
        raise AssertionError("resolve_text must not be called on a seed hit")

    monkeypatch.setattr(lyrics_fetch, "resolve_text", boom)
    main._buckets.clear()
    r = client.post(f"/api/songs/{sid}/autofetch")
    assert r.status_code == 200, r.text
    assert r.json()["analysis"]["stats"]["chinese_tokens"] == g["stats"]["chinese_tokens"]

    # served from the corpus, and still no lyrics stored on the song row
    row = client.get(f"/api/songs/{sid}").json()
    assert row["has_lyrics"] is False


def test_autofetch_warms_the_corpus_for_other_users(client, monkeypatch):
    """User A's fetch must leave a corpus entry that user B reuses."""
    from app import db, lyrics_fetch, main

    calls = []

    def fake_resolve(artist, title):
        calls.append((artist, title))
        return SONG

    monkeypatch.setattr(lyrics_fetch, "resolve_text", fake_resolve)

    register(client)
    sid = client.post("/api/songs", json={"title": "月亮代表我的心", "artist": "邓丽君"}).json()["id"]
    assert client.post(f"/api/songs/{sid}/autofetch").status_code == 200
    assert len(calls) == 1

    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) c FROM seed_analysis").fetchone()["c"] == 1

    # second user, same song: no further fetch
    client.post("/api/logout")
    register(client, email="bob@example.com")
    main._buckets.clear()
    sid2 = client.post("/api/songs", json={"title": "月亮代表我的心", "artist": "邓丽君"}).json()["id"]
    assert client.post(f"/api/songs/{sid2}/autofetch").status_code == 200
    assert len(calls) == 1, "second user should have been served from the corpus"
