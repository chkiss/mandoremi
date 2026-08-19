"""Shared seed corpus of text-free song analyses.

The point of this table is to share the *analysis* of a song across users
without ever sharing (or storing) its lyrics. What goes in is exactly the
ghost payload produced by ``analyze.strip_text``: stats, the vocab bag, and
grammar/idiom counts. Lines and grammar example lines -- the parts from which
lyric text could be reconstructed -- are dropped before storage, and
``store()`` refuses anything that still carries them.

Keying is (artist, title), normalized, because that is all a user has when
they add a song: they don't have the lyrics, so a lyrics-hash key can't be
looked up until after a fetch, which is the very cost we're trying to avoid.
"""
import json
import re

from . import analyze, normalize

# Titles only. The ARTIST half of key() delegates to artists.alias_key -- this
# used to be a second copy of that regex, and the copies silently drifted: the
# full-width bang was added there for "hush！" and not here, so the corpus kept
# keying that artist separately from the 'hush' we had already resolved.
_PUNCT_RE = re.compile(r"[\s'’\-_.,!?()（）\[\]【】·・~～\"“”"
                       r"！？。：；　]+")


def key(artist, title):
    """Normalized (artist, title) lookup key.

    Simplifies Han (so 夜空中最亮的星 and 夜空中最亮的星 in traditional agree),
    casefolds latin, and drops spacing/punctuation. Uses the fetcher's own
    core_title so 'Song (Live Version)'-style suffixes key the same as 'Song'.
    """
    from . import artists as _artists
    from . import lyrics_fetch  # local import: lyrics_fetch imports normalize

    # alias_key() drops collaborators and normalizes, so "Jay Chou, Gary Yang"
    # keys the same as "Jay Chou" -- and, because it is the SAME function, the
    # corpus key can never disagree with artist_alias about what an artist is.
    a = _artists.alias_key(artist or "")
    t = _PUNCT_RE.sub("", normalize.to_simplified(
        lyrics_fetch.core_title(title or "")).lower())
    return a, t


def lookup(conn, artist, title, version):
    """Return (ghost_analysis, lyrics_hash) for a seeded song, or None.

    Tries the canonical artist id first, so a song seeded as 周杰伦 is found by
    a user who typed "Jay Chou", then falls back to the raw string key for rows
    seeded before that artist had an id. A string hit backfills the id, so the
    corpus converges on canonical keys as it is used.

    Version-gated: a stale analysis is ignored rather than served, since a
    ghost analysis has no text and therefore cannot be re-analyzed in place.
    """
    from . import artists

    a, t = key(artist, title)
    if not t:
        return None

    ident = artists.lookup(conn, artist)
    if ident:
        row = conn.execute(
            "SELECT analysis, lyrics_hash FROM seed_analysis "
            "WHERE artist_id = ? AND title_key = ? AND version = ?",
            (ident[0], t, version)).fetchone()
        if row:
            return json.loads(row["analysis"]), row["lyrics_hash"]

    row = conn.execute(
        "SELECT analysis, lyrics_hash, artist_id FROM seed_analysis "
        "WHERE artist_key = ? AND title_key = ? AND version = ?",
        (a, t, version)).fetchone()
    if not row:
        return None
    if ident and row["artist_id"] is None:
        conn.execute("UPDATE seed_analysis SET artist_id = ? "
                     "WHERE artist_key = ? AND title_key = ?", (ident[0], a, t))
    return json.loads(row["analysis"]), row["lyrics_hash"]


def acquire(artist, title, version=None, text_source=None, text_sink=None,
            require_text=False):
    """THE way to obtain an analysis for a song we have no lyrics for.

    Returns ``(reason, ghost, lyrics_hash)`` where reason is one of
    ``hit`` (served from the corpus), ``local`` (re-analysed from text the
    caller supplied, no network), ``fetched`` (resolved and now seeded),
    ``miss-cached`` (known-bad, skipped without a fetch), ``nomatch``,
    ``nochinese``. ghost/hash are None unless reason is hit/local/fetched.

    ``text_source(artist, title)`` is consulted before the network and lets a
    caller re-analyse from lyrics it already holds — the difference between a
    ~9s fetch and a ~0.02s local pass. ``text_sink(artist, title, text, hash)``
    is called after a successful fetch, and is the ONLY route by which lyric
    text leaves this function.

    That sink is deliberately a caller-supplied callback rather than a return
    value: text must not be something a caller receives by accident, and the
    one legitimate destination (app.vault) is scoped to a single user. Never
    point it at anything shared — seed_analysis in particular, whose whole
    contract is that it holds no text.

    ``require_text=True`` says the caller is here for the lyrics, not only the
    analysis, so an up-to-date corpus row is not on its own a reason to skip
    the song. Bulk backfills want this; the request path does not, and paying
    ~9s to re-fetch text a user's page will never read would be a bug.

    Every caller -- the API endpoint and every bulk seeding tool -- must go
    through here rather than calling lyrics_fetch directly, because this is
    where the corpus read, the negative cache, the write-back and the
    text-free guarantee are applied together. Skipping it is how a re-run ends
    up paying ~9s each for thousands of already-known failures, and how lyric
    text ends up somewhere it must not be. See docs/SEEDING.md.

    Deliberately holds no DB connection across the network call.
    """
    from . import analyze, db, lyrics_fetch

    version = version or analyze.hskdata.config()["analysis_version"]
    # Held lyrics beat both the negative cache and the network: a song we
    # already have the text for can always be re-analysed, even if upstream
    # has since stopped serving it.
    text = text_source(artist, title) if text_source else None
    local = bool(text)
    with db.session() as conn:
        hit = lookup(conn, artist, title, version)
    # A current corpus row means there is nothing to compute -- but under
    # require_text it does NOT mean there is nothing to do, because the point
    # of the pass is the text, and a song analysed on an earlier run has none
    # stored. Returning "hit" there would quietly leave a permanent hole.
    if hit and (local or not require_text):
        return "hit", hit[0], hit[1]
    if not text:
        with db.session() as conn:
            if missed(conn, artist, title):
                return "miss-cached", None, None
        text = lyrics_fetch.resolve_text(artist or "", title)
    if not text:
        with db.session() as conn:
            record_miss(conn, artist, title)
        return "nomatch", None, None

    result = analyze.analyze(text)
    if result["stats"]["chinese_tokens"] == 0:
        with db.session() as conn:
            record_miss(conn, artist, title)
        return "nochinese", None, None

    ghost = analyze.strip_text(result)
    h = analyze.lyrics_hash(text)
    # Hand the text off BEFORE the corpus write. If the sink raises, nothing is
    # stored: a corpus row whose lyrics were meant to be kept and silently were
    # not is exactly the state this feature exists to prevent, and it is
    # invisible afterwards.
    if text_sink and not local:
        text_sink(artist, title, text, h)
    with db.session() as conn:
        store(conn, artist, title, ghost, h)
        clear_miss(conn, artist, title)
    return ("local" if local else "fetched"), ghost, h


SEED_MISS_RETRY_DAYS = 30


def missed(conn, artist, title, retry_days=SEED_MISS_RETRY_DAYS):
    """True if this song was recently looked for and not found.

    Callers should skip the ~9s fetch when this is true. Misses expire so that
    songs added to upstream catalogues are eventually picked up.
    """
    a, t = key(artist, title)
    if not t:
        return False
    row = conn.execute(
        "SELECT 1 FROM seed_miss WHERE artist_key = ? AND title_key = ? "
        "AND last_try > datetime('now', ?)",
        (a, t, f"-{int(retry_days)} days")).fetchone()
    return row is not None


def record_miss(conn, artist, title):
    """Remember that this song could not be resolved."""
    a, t = key(artist, title)
    if not t:
        return
    conn.execute(
        "INSERT INTO seed_miss(artist_key, title_key) VALUES (?,?) "
        "ON CONFLICT(artist_key, title_key) DO UPDATE SET "
        "tries = tries + 1, last_try = datetime('now')", (a, t))


def clear_miss(conn, artist, title):
    """Drop the negative cache entry — call after a successful resolve."""
    a, t = key(artist, title)
    if t:
        conn.execute("DELETE FROM seed_miss WHERE artist_key = ? AND title_key = ?", (a, t))


def store(conn, artist, title, ghost, lyrics_hash):
    """Add a text-free analysis to the shared corpus.

    Raises if handed anything that could reconstruct lyrics -- this table is
    readable by every user, so the invariant is enforced here rather than
    trusted from the caller.
    """
    leaks = [f for f in ("lines", "text", "raw") if f in ghost]
    if leaks:
        raise ValueError(f"refusing to seed analysis carrying {leaks}")
    if any("examples" in g or "lines" in g for g in ghost.get("grammar", [])):
        raise ValueError("refusing to seed grammar with example lines")
    from . import artists

    a, t = key(artist, title)
    if not t:
        return
    ident = artists.lookup(conn, artist)
    conn.execute(
        "INSERT OR REPLACE INTO seed_analysis"
        "(artist_key, title_key, artist_id, version, lyrics_hash, analysis) "
        "VALUES (?,?,?,?,?,?)",
        (a, t, ident[0] if ident else None,
         ghost.get("version") or analyze.hskdata.config()["analysis_version"],
         lyrics_hash, json.dumps(ghost, ensure_ascii=False, separators=(",", ":"))))
