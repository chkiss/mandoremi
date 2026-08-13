"""The private lyrics vault, and the boundary between it and the shared corpus.

The tests that matter here are the negative ones: text must reach the vault and
must NOT reach seed_analysis, and one user's vault must not be readable as
another's. Those are the two invariants the feature is built around, and both
fail silently if broken.
"""
import json

import pytest

from app import analyze, db, seed, vault

LYRICS = "我爱你\n不知不觉就天亮了\n我们一起走"


@pytest.fixture
def conn(client):
    """client's fixture points db.DB_PATH at a temp file and runs init()."""
    with db.connect() as c:
        c.execute("INSERT INTO users (id, email, pwhash) VALUES (1,'a@b.c','x')")
        c.execute("INSERT INTO users (id, email, pwhash) VALUES (2,'d@e.f','x')")
        c.commit()
        yield c


def test_lyrics_round_trip(conn):
    vault.put(conn, 1, "周杰伦", "七里香", LYRICS, "h1")
    assert vault.get(conn, 1, "周杰伦", "七里香") == LYRICS


def test_one_users_lyrics_are_not_another_users(conn):
    vault.put(conn, 1, "周杰伦", "七里香", LYRICS, "h1")
    assert vault.get(conn, 2, "周杰伦", "七里香") is None
    assert vault.count(conn, 1) == 1
    assert vault.count(conn, 2) == 0


def test_lookup_normalizes_the_way_the_corpus_does(conn):
    # Same keying as seed_analysis, or the two tables cannot be joined and
    # `missing()` silently reports every row as absent.
    vault.put(conn, 1, "Jay Chou", "Qi Li Xiang (Live Version)", LYRICS, "h1")
    assert vault.get(conn, 1, "jay chou", "Qi Li Xiang") == LYRICS


def test_acquire_puts_text_in_the_sink_and_never_in_the_corpus(conn, monkeypatch):
    monkeypatch.setattr(seed.lyrics_fetch if hasattr(seed, "lyrics_fetch")
                        else __import__("app.lyrics_fetch", fromlist=["x"]),
                        "resolve_text", lambda a, t: LYRICS)
    seen = {}

    def sink(artist, title, text, h):
        seen["text"] = text
        with db.connect() as c:
            vault.put(c, 1, artist, title, text, h)
            c.commit()

    reason, ghost, _h = seed.acquire("周杰伦", "七里香", text_sink=sink)
    assert reason == "fetched"
    assert seen["text"] == LYRICS

    # The corpus row must carry no route back to the text.
    with db.connect() as c:
        row = c.execute("SELECT analysis FROM seed_analysis").fetchone()
        stored = json.loads(row["analysis"])
    assert "lines" not in stored and "text" not in stored
    assert "不知不觉就天亮了" not in row["analysis"]
    assert vault.get(c, 1, "周杰伦", "七里香") == LYRICS


def test_text_source_avoids_the_network_entirely(conn, monkeypatch):
    import app.lyrics_fetch as lf

    def explode(a, t):
        raise AssertionError("fetched a song we already hold the lyrics for")

    monkeypatch.setattr(lf, "resolve_text", explode)
    reason, ghost, _h = seed.acquire(
        "周杰伦", "七里香", text_source=lambda a, t: LYRICS)
    assert reason == "local"
    assert ghost["stats"]["chinese_tokens"] > 0


def test_require_text_refetches_a_song_whose_analysis_is_current(conn, monkeypatch):
    """A corpus row at the current version is not enough for a vault backfill.

    This is the case that made the first re-seed useless: 1,857 songs were
    already analysed, so acquire() short-circuited on "hit" and their lyrics
    were never kept.
    """
    import app.lyrics_fetch as lf
    monkeypatch.setattr(lf, "resolve_text", lambda a, t: LYRICS)

    seed.acquire("周杰伦", "七里香")           # analysed, no text kept
    assert seed.acquire("周杰伦", "七里香")[0] == "hit"

    calls = []
    reason, _g, _h = seed.acquire(
        "周杰伦", "七里香", require_text=True,
        text_source=lambda a, t: None,
        text_sink=lambda a, t, text, h: calls.append(text))
    assert reason == "fetched"
    assert calls == [LYRICS]


def test_missing_lists_corpus_rows_with_no_lyrics(conn, monkeypatch):
    import app.lyrics_fetch as lf
    monkeypatch.setattr(lf, "resolve_text", lambda a, t: LYRICS)
    seed.acquire("周杰伦", "七里香")
    conn.commit()
    assert vault.missing(conn, 1) != []
    vault.put(conn, 1, "周杰伦", "七里香", LYRICS, "h1")
    conn.commit()
    assert vault.missing(conn, 1) == []


def test_a_failing_sink_does_not_leave_an_analysis_without_its_lyrics(conn, monkeypatch):
    import app.lyrics_fetch as lf
    monkeypatch.setattr(lf, "resolve_text", lambda a, t: LYRICS)

    def bad_sink(*a):
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        seed.acquire("周杰伦", "七里香", text_sink=bad_sink)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM seed_analysis").fetchone()[0] == 0


def test_session_closes_its_connection(conn):
    """The bug that killed the first vault re-seed at 6,824 songs.

    `with db.connect()` ends the transaction but leaves the file descriptor
    open. Seven of those per song across eight threads exhausted the 1024-fd
    limit and every write started failing with "unable to open database file".
    """
    import sqlite3
    with db.session() as c:
        c.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")


def test_bulk_acquire_does_not_leak_descriptors(conn, monkeypatch):
    import app.lyrics_fetch as lf
    monkeypatch.setattr(lf, "resolve_text", lambda a, t: LYRICS)
    import os
    fds = lambda: len(os.listdir("/proc/self/fd"))
    for i in range(5):                      # warm up any lazily-opened files
        seed.acquire(f"artist{i}", f"song{i}")
    before = fds()
    for i in range(5, 40):
        seed.acquire(f"artist{i}", f"song{i}")
    # A per-song leak would show up as ~35 extra descriptors, not a handful.
    assert fds() - before < 10, f"descriptors grew {before} -> {fds()}"
