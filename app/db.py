"""SQLite persistence. Lyrics are stored only on the owning user's song row
(user-uploaded, served back only to that user). The shared analysis cache
keyed by lyrics hash holds statistics only — never lyric text or line data."""
import contextlib
import json
import os
import sqlite3

DB_PATH = os.environ.get("HSKLYRICS_DB", os.path.join(os.path.dirname(__file__), "..", "hsklyrics.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  pwhash TEXT NOT NULL,
  created TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  expires TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlists (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  source_url TEXT,
  created TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS songs (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  playlist_id INTEGER REFERENCES playlists(id),
  artist TEXT DEFAULT '',
  title TEXT NOT NULL,
  lyrics TEXT,
  lyrics_hash TEXT,
  analysis TEXT,
  created TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS analysis_cache (
  lyrics_hash TEXT PRIMARY KEY,
  version INTEGER NOT NULL,
  stats TEXT NOT NULL,
  created TEXT DEFAULT (datetime('now'))
);
-- Seed corpus: text-free analyses keyed by (artist, title) so that ANY user
-- auto-analyzing a song we've already resolved gets it instantly, with no
-- refetch. Holds the same ghost payload as an auto-fetched song -- stats,
-- vocab bag and grammar counts, never lyric text or line data -- which is why
-- it is safe to share across accounts.
CREATE TABLE IF NOT EXISTS seed_analysis (
  artist_key TEXT NOT NULL,
  title_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  lyrics_hash TEXT NOT NULL,
  analysis TEXT NOT NULL,
  created TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (artist_key, title_key)
);
-- Password reset requests, handled by hand (no transactional email service).
-- A user submits the form; the owner resets the password with
-- tools/reset_password.py and replies from their own mailbox. The new password
-- always goes to the address REGISTERED ON THE ACCOUNT, never to anything the
-- form supplied, or the form would be an account-takeover tool.
CREATE TABLE IF NOT EXISTS password_resets (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL,            -- as typed; may not match any account
  user_id INTEGER,                -- set when it does match one
  note TEXT,                      -- optional message from the requester
  created TEXT DEFAULT (datetime('now')),
  handled TEXT                    -- datetime once actioned
);
CREATE INDEX IF NOT EXISTS idx_resets_open ON password_resets(handled, created);
-- Negative cache for the seed corpus. Resolving a song costs ~9s of HTTP to
-- third-party lyric services, and a song they simply don't carry costs that
-- every single time it is asked for. Recording the miss means a re-run, or a
-- user clicking Auto-analyze twice, doesn't pay it again. Retried after
-- SEED_MISS_RETRY_DAYS, since catalogues do gain songs.
CREATE TABLE IF NOT EXISTS seed_miss (
  artist_key TEXT NOT NULL,
  title_key TEXT NOT NULL,
  tries INTEGER NOT NULL DEFAULT 1,
  last_try TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (artist_key, title_key)
);
-- Canonical artist identity. The same artist arrives under many strings --
-- "Jay Chou", "周杰伦", "周杰倫" -- because playlist imports carry whatever the
-- source platform used. Mapping each string to one canonical id lets the seed
-- corpus above hit regardless of which name the user typed. Self-building:
-- every resolution any user triggers is cached here for everyone.
CREATE TABLE IF NOT EXISTS artist_alias (
  alias_key TEXT PRIMARY KEY,     -- normalized user-supplied string
  artist_id INTEGER NOT NULL,     -- canonical (NetEase) artist id
  display TEXT NOT NULL,          -- canonical name to show, e.g. 周杰伦
  confidence TEXT NOT NULL,       -- exact | latin | han-prefix | override
  created TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_alias_artist ON artist_alias(artist_id);
CREATE TABLE IF NOT EXISTS known_words (
  user_id INTEGER NOT NULL REFERENCES users(id),
  word TEXT NOT NULL,
  PRIMARY KEY (user_id, word)
);
CREATE TABLE IF NOT EXISTS lockouts (
  email TEXT PRIMARY KEY,
  fails INTEGER NOT NULL DEFAULT 0,
  lockout_count INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  permanent INTEGER NOT NULL DEFAULT 0
);
-- One user's private copy of lyrics resolved on their behalf. NOT the shared
-- corpus and not interchangeable with it: seed_analysis is text-free and read
-- by everybody, this is text and is read by nobody but its owner.
--
-- It exists so that re-analysing the corpus is a local CPU pass instead of
-- thousands of network fetches. Changing the segmenter's dictionary (adding
-- chengyu, say) invalidates every stored analysis, and without the text there
-- is nothing to recompute from -- the analysis is a one-way function.
--
-- Keyed the same way as seed_analysis so the two line up row for row.
CREATE TABLE IF NOT EXISTS lyrics_vault (
  user_id INTEGER NOT NULL REFERENCES users(id),
  artist_key TEXT NOT NULL,
  title_key TEXT NOT NULL,
  artist TEXT NOT NULL,           -- display forms, kept for QA and re-fetch
  title TEXT NOT NULL,
  lyrics TEXT NOT NULL,
  lyrics_hash TEXT NOT NULL,
  created TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, artist_key, title_key)
);
CREATE INDEX IF NOT EXISTS idx_songs_user ON songs(user_id);
CREATE INDEX IF NOT EXISTS idx_songs_playlist ON songs(playlist_id);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers proceed during writes; busy_timeout retries instead of
    # raising "database is locked" under concurrent threadpool requests.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextlib.contextmanager
def session():
    """Connection that commits on success, rolls back on error, and ALWAYS
    closes.

    `with connect() as conn` does not close — sqlite3's context manager only
    ends the transaction, leaving the connection (and its file descriptor)
    alive until the garbage collector happens to get to it. A request handler
    gets away with that. A bulk job does not: the corpus re-seed opened seven
    connections per song across eight threads and died at 6,824 songs with
    "unable to open database file", which is EMFILE wearing a disguise.

    Prefer this everywhere; keep connect() for callers that manage their own
    lifetime.
    """
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _migrate(conn):
    """Additive migrations for tables that already exist in the wild."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(seed_analysis)")}
    if "artist_id" not in cols:
        # Rows seeded before canonical ids existed keep artist_id NULL and are
        # still found by their string key; seed.lookup backfills on the way past.
        conn.execute("ALTER TABLE seed_analysis ADD COLUMN artist_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seed_artist "
                 "ON seed_analysis(artist_id, title_key)")
    # The English name as it should be shown ("Faye Wong"). alias_key holds the
    # normalized lookup form ('fayewong'), which is not presentable.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(artist_alias)")}
    if "english" not in cols:
        conn.execute("ALTER TABLE artist_alias ADD COLUMN english TEXT")
    # How well known the artist is, independent of how much of them we have
    # seeded -- seeding depth tracks lyric availability, not popularity.
    if "popularity" not in cols:
        conn.execute("ALTER TABLE artist_alias ADD COLUMN popularity INTEGER")


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    os.chmod(DB_PATH, 0o600)  # owner-only: box has other local users


def to_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
