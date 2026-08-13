"""One user's private lyrics store.

WHY THIS IS SEPARATE FROM THE CORPUS
------------------------------------
``seed_analysis`` is text-free and shared by every user. That is the whole
licensing argument for sharing it: what's shared is data *about* a song, never
the song. This module is the opposite in both respects — it holds the text, and
it is scoped to a single ``user_id`` that must never widen.

Keeping them apart is what lets both be true at once. Do not "simplify" this by
putting lyrics on the corpus row.

WHAT IT BUYS
------------
Analysis is a one-way function: strip_text() discards the lines, so a stored
analysis cannot be recomputed. Any change to the segmenter's dictionary — a new
chengyu list, a normalization rule, an HSK list update — invalidates every
analysis in the corpus, and without the text the only way to rebuild is to
re-download thousands of songs at ~8.7 seconds each. With the text on hand the
same rebuild is a local CPU pass.

It is also the surface for quality checks: a suspect analysis can be re-read
against the lyrics it came from, which is impossible for a corpus row alone.
"""
from . import seed


def get(conn, user_id, artist, title):
    """The stored lyrics for one song, or None."""
    a, t = seed.key(artist, title)
    if not t:
        return None
    row = conn.execute(
        "SELECT lyrics FROM lyrics_vault "
        "WHERE user_id = ? AND artist_key = ? AND title_key = ?",
        (user_id, a, t)).fetchone()
    return row["lyrics"] if row else None


def put(conn, user_id, artist, title, lyrics, lyrics_hash):
    """Store lyrics for one song under one user.

    user_id is required and has no default on purpose: there is no such thing
    as an unowned row here, and a caller that has not decided whose it is has
    not yet earned the right to write it.
    """
    a, t = seed.key(artist, title)
    if not t or not lyrics:
        return False
    conn.execute(
        "INSERT OR REPLACE INTO lyrics_vault"
        "(user_id, artist_key, title_key, artist, title, lyrics, lyrics_hash)"
        " VALUES (?,?,?,?,?,?,?)",
        (user_id, a, t, artist or "", title, lyrics, lyrics_hash))
    return True


def count(conn, user_id):
    return conn.execute("SELECT COUNT(*) FROM lyrics_vault WHERE user_id = ?",
                        (user_id,)).fetchone()[0]


def missing(conn, user_id):
    """(artist_key, title_key) in the shared corpus with no lyrics stored.

    The work list for a backfill: these are the songs whose analysis exists but
    cannot be recomputed without going back to the network.
    """
    return [(r["artist_key"], r["title_key"]) for r in conn.execute(
        "SELECT s.artist_key, s.title_key FROM seed_analysis s "
        "LEFT JOIN lyrics_vault v ON v.artist_key = s.artist_key "
        " AND v.title_key = s.title_key AND v.user_id = ? "
        "WHERE v.artist_key IS NULL", (user_id,))]
