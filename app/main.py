"""Mandoremi — Chinese lyrics HSK analyzer, FastAPI app."""
import hashlib
import json
import os
import re
import time
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analyze, auth, captcha, db, dictionary, notify, playlist_import, public

app = FastAPI(title="Mandoremi")
app.add_middleware(GZipMiddleware, minimum_size=1024)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
SECURE_COOKIES = os.environ.get("HSKLYRICS_SECURE_COOKIES", "1") == "1"


@app.on_event("startup")
def _startup():
    db.init()
    dictionary.load()
    analyze.analyze("预热")  # warm up segmenter + lexicons at boot


# ---------- rate limiting (in-memory, per IP; nginx passes X-Real-IP) ----------

_buckets = {}


def rate_limit(request: Request, key: str, limit: int, per: int = 60):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    now = time.time()
    count, start = _buckets.get((key, ip), (0, now))
    if now - start > per:
        count, start = 0, now
    count += 1
    _buckets[(key, ip)] = (count, start)
    if len(_buckets) > 10000:
        # prune expired windows first; only a genuine flood forces a full reset
        for k in [k for k, (_, s) in _buckets.items() if now - s > 600]:
            del _buckets[k]
        if len(_buckets) > 10000:
            _buckets.clear()
    if count > limit:
        raise HTTPException(429, "Too many requests — slow down a little")


_global_buckets = {}


def global_limit(key: str, limit: int, per: int = 60, message=None):
    """Ceiling across ALL callers, not per IP.

    Per-IP limits don't bound what we do to *third parties*: distributed
    traffic can still push a lot of outbound lyric lookups from our single
    server address, and the whole corpus pipeline depends on that address not
    being blocked upstream. This caps the total.
    """
    now = time.time()
    count, start = _global_buckets.get(key, (0, now))
    if now - start > per:
        count, start = 0, now
    count += 1
    _global_buckets[key] = (count, start)
    if count > limit:
        raise HTTPException(
            429, message or "The lookup service is busy right now — try again shortly")


# ---------- anonymous analysis (free tier, nothing persisted) ----------

class AnalyzeIn(BaseModel):
    text: str


@app.post("/api/analyze")
def analyze_anonymous(body: AnalyzeIn, request: Request):
    rate_limit(request, "analyze", 20)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Lyrics are empty")
    if len(text) > 50000:
        raise HTTPException(413, "Lyrics too long")
    result = analyze.analyze(text)
    if result["stats"]["chinese_tokens"] == 0:
        raise HTTPException(422, "No Chinese text found in those lyrics")
    return {"analysis": dictionary.annotate(result)}


# ---------- auth ----------

# A seeded corpus hit costs nothing upstream, so this only needs to bound real
# outbound lookups. ~90/min is far above organic use and far below anything
# that would look like scraping from our address.
AUTOFETCH_GLOBAL_PER_MIN = int(os.environ.get("AUTOFETCH_GLOBAL_PER_MIN", "90"))


class Credentials(BaseModel):
    email: str
    password: str
    captcha: str = ""


def _client_ip(request: Request):
    return request.headers.get("x-real-ip") or (
        request.client.host if request.client else None)


def _require_captcha(token, request: Request):
    if not captcha.verify(token, _client_ip(request)):
        raise HTTPException(400, "Captcha failed — please try again")


def _set_session(response: Response, token: str):
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=SECURE_COOKIES)


@app.post("/api/register")
def register(creds: Credentials, request: Request, response: Response):
    rate_limit(request, "register", 5, per=300)
    # Bot signups would burn the owner's own mailbox once reset requests exist.
    _require_captcha(creds.captcha, request)
    email = creds.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Invalid email address")
    if len(creds.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    with db.connect() as conn:
        try:
            cur = conn.execute("INSERT INTO users(email, pwhash) VALUES (?,?)",
                               (email, auth.hash_password(creds.password)))
        except Exception:
            raise HTTPException(409, "An account with that email already exists")
        token = auth.create_session(conn, cur.lastrowid)
    _set_session(response, token)
    return {"email": email}


@app.post("/api/login")
def login(creds: Credentials, request: Request, response: Response):
    rate_limit(request, "login", 10, per=300)
    email = creds.email.strip().lower()
    with db.connect() as conn:
        auth.check_lockout(conn, email)
        row = conn.execute("SELECT id, pwhash FROM users WHERE email = ?", (email,)).fetchone()
        if not row or not auth.verify_password(creds.password, row["pwhash"]):
            auth.record_login_failure(conn, email)
            raise HTTPException(401, "Wrong email or password")
        auth.clear_login_failures(conn, email)
        token = auth.create_session(conn, row["id"])
    _set_session(response, token)
    return {"email": email}


class ResetRequestIn(BaseModel):
    email: str
    note: str = ""
    captcha: str = ""


@app.post("/api/password-reset-request")
def password_reset_request(body: ResetRequestIn, request: Request):
    """Queue a manual password reset.

    There is no transactional email service, so this records the request and
    notifies the owner, who resets the password with tools/reset_password.py
    and replies from their own mailbox — always to the address registered on
    the account, never to anything supplied here.

    Answers identically whether or not the account exists: this endpoint must
    not become a way to test which emails are registered.
    """
    rate_limit(request, "reset", 3, per=900)
    _require_captcha(body.captcha, request)
    email = body.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Invalid email address")
    ok = {"ok": True, "message": "If that address has an account, the owner will "
                                 "email you a new password shortly."}
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        # An unmatched address is still recorded: it's usually a typo by a real
        # user, and seeing it is how the owner can help them.
        open_already = conn.execute(
            "SELECT id FROM password_resets WHERE email = ? AND handled IS NULL",
            (email,)).fetchone()
        if open_already:
            return ok
        conn.execute("INSERT INTO password_resets(email, user_id, note) VALUES (?,?,?)",
                     (email, row["id"] if row else None, body.note.strip()[:500]))
    notify.reset_requested(email, matched=bool(row))
    return ok


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/change-password")
def change_password(body: ChangePasswordIn, request: Request,
                    user=Depends(auth.current_user)):
    """Needed for the manual reset loop to terminate: the owner mails a
    temporary password, and the user replaces it here."""
    rate_limit(request, "changepw", 10, per=300)
    if len(body.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    with db.connect() as conn:
        row = conn.execute("SELECT pwhash FROM users WHERE id = ?", (user["id"],)).fetchone()
        if not auth.verify_password(body.current_password, row["pwhash"]):
            raise HTTPException(401, "Current password is wrong")
        conn.execute("UPDATE users SET pwhash = ? WHERE id = ?",
                     (auth.hash_password(body.new_password), user["id"]))
        # Log out other sessions: a temporary password may have been emailed.
        token = request.cookies.get(auth.COOKIE)
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?",
                     (user["id"], token or ""))
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.COOKIE)
    if token:
        with db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(auth.current_user)):
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM known_words WHERE user_id = ?",
                         (user["id"],)).fetchone()[0]
    return user | {"known_count": n}


# ---------- known words ----------

class KnownIn(BaseModel):
    text: str


HAN_WORD = re.compile(r"[一-鿿㐀-䶿]+")


def _known_set(conn, user_id):
    return {r["word"] for r in conn.execute(
        "SELECT word FROM known_words WHERE user_id = ?", (user_id,))}


@app.get("/api/known")
def get_known(user=Depends(auth.current_user)):
    with db.connect() as conn:
        words = sorted(_known_set(conn, user["id"]))
    return {"count": len(words), "words": words}


@app.put("/api/known")
def set_known(body: KnownIn, user=Depends(auth.current_user)):
    """Replace the personal known-word list. Any separator works; traditional
    characters are converted. An empty list clears it."""
    from . import hskdata, normalize
    raw = set(HAN_WORD.findall(normalize.to_simplified(body.text)))
    # store dictionary forms too so e.g. 朋友们 in the list matches vocab entry 朋友
    words = sorted(raw | {hskdata.normalize_token(w) for w in raw})
    if len(words) > 50000:
        raise HTTPException(413, "That's more than 50,000 words — trim the list")
    with db.connect() as conn:
        conn.execute("DELETE FROM known_words WHERE user_id = ?", (user["id"],))
        conn.executemany("INSERT INTO known_words(user_id, word) VALUES (?,?)",
                         [(user["id"], w) for w in words])
    return {"count": len(words)}


class KnownAddIn(BaseModel):
    words: list[str]


@app.post("/api/known/add")
def add_known(body: KnownAddIn, user=Depends(auth.current_user)):
    """Append words to the personal known-word list (used by the per-word
    "+" button; PUT /api/known replaces the whole list)."""
    from . import hskdata, normalize
    raw = set()
    for w in body.words[:100]:
        raw |= set(HAN_WORD.findall(normalize.to_simplified(w)))
    words = raw | {hskdata.normalize_token(w) for w in raw}
    if not words:
        raise HTTPException(400, "No Chinese words given")
    with db.connect() as conn:
        conn.executemany("INSERT OR IGNORE INTO known_words(user_id, word) VALUES (?,?)",
                         [(user["id"], w) for w in sorted(words)])
        n = conn.execute("SELECT COUNT(*) FROM known_words WHERE user_id = ?",
                         (user["id"],)).fetchone()[0]
    return {"count": n}


# ---------- playlists ----------

class PlaylistIn(BaseModel):
    name: str | None = None
    spotify_url: str | None = None


# Personalized per-song stats are recomputed from the full stored analysis
# (JSON parse + token re-merge) — too heavy to repeat for every song on every
# playlist view. Keyed by (lyrics_hash, known-set key); song edits change the
# hash and known-list updates change the key, so entries self-invalidate.
_stats_cache = {}


def _known_key(known):
    return hash(frozenset(known))


def _song_stats(conn, row, known, known_key):
    """Personalized stats for one song row, memoized."""
    ck = (row["lyrics_hash"], known_key)
    if row["lyrics_hash"] and ck in _stats_cache:
        return _stats_cache[ck]
    a = _fresh_analysis(conn, row)
    if a is None:
        return None
    stats = analyze.personalize(a, known)["stats"]
    if row["lyrics_hash"]:
        if len(_stats_cache) > 10000:
            _stats_cache.clear()
        _stats_cache[ck] = stats
    return stats


def _plevel(stats, level):
    """per_level keys are ints fresh from _stats but strings after a JSON round
    trip — accept either."""
    return stats["per_level"].get(level) or stats["per_level"][str(level)]


@app.get("/api/playlists")
def list_playlists(user=Depends(auth.current_user)):
    with db.connect() as conn:
        pls = conn.execute("SELECT id, name, source_url, created FROM playlists "
                           "WHERE user_id = ? ORDER BY created DESC", (user["id"],)).fetchall()
        known = _known_set(conn, user["id"])
        kk = _known_key(known)
        out = []
        for pl in pls:
            rows = conn.execute("SELECT * FROM songs WHERE playlist_id = ? AND user_id = ?",
                                (pl["id"], user["id"])).fetchall()
            stats_list = []
            for r in rows:
                s = _song_stats(conn, r, known, kk)
                if s:
                    stats_list.append(s)
            avg = None
            if stats_list:
                n = len(stats_list)
                per = {}
                for lvl in analyze.LEARNER_LEVELS:
                    per[lvl] = {
                        "coverage": round(sum(_plevel(s, lvl)["coverage"] for s in stats_list) / n, 4),
                        "learning_value": round(sum(_plevel(s, lvl)["learning_value"] for s in stats_list) / n, 1),
                    }
                avg = {"richness": round(sum(s["richness"] for s in stats_list) / n, 4),
                       "per_level": per}
            out.append(dict(pl) | {"songs": len(rows), "analyzed": len(stats_list), "avg": avg})
    return out


@app.post("/api/playlists")
def create_playlist(body: PlaylistIn, user=Depends(auth.current_user)):
    tracks = []
    name = (body.name or "").strip()
    source_url = None
    capped = False
    if body.spotify_url:
        try:
            info = playlist_import.fetch(body.spotify_url)
        except playlist_import.PlaylistImportError as e:
            raise HTTPException(422, str(e))
        name = name or info["name"]
        source_url = body.spotify_url
        tracks = info["tracks"]
        capped = info["capped"]
    if not name:
        raise HTTPException(400, "Playlist needs a name or a Spotify URL")
    with db.connect() as conn:
        cur = conn.execute("INSERT INTO playlists(user_id, name, source_url) VALUES (?,?,?)",
                           (user["id"], name, source_url))
        pid = cur.lastrowid
        for t in tracks:
            artist = t["artist"].replace("\xa0", " ").strip()
            conn.execute("INSERT INTO songs(user_id, playlist_id, artist, title) VALUES (?,?,?,?)",
                         (user["id"], pid, artist, t["title"].strip()))
    return {"id": pid, "name": name, "imported": len(tracks), "capped": capped}


@app.get("/api/playlists/{pid}")
def get_playlist(pid: int, user=Depends(auth.current_user)):
    with db.connect() as conn:
        pl = conn.execute("SELECT id, name, source_url, created FROM playlists "
                          "WHERE id = ? AND user_id = ?", (pid, user["id"])).fetchone()
        if not pl:
            raise HTTPException(404, "Playlist not found")
        rows = conn.execute(
            "SELECT * FROM songs WHERE playlist_id = ? AND user_id = ? ORDER BY id",
            (pid, user["id"])).fetchall()
        known = _known_set(conn, user["id"])
        kk = _known_key(known)
        songs = []
        for r in rows:
            s = _song_stats(conn, r, known, kk)
            entry = {"id": r["id"], "artist": r["artist"], "title": r["title"],
                     "artist_slug": public.slug_for(r["artist"]),
                     "song_path": public.song_path_for(r["artist"], r["title"]),
                     "analyzed": s is not None}
            if s:
                # Stats only. The table renders from `stats`; opening a song
                # calls /api/songs/{id}, which returns the full analysis for
                # that one song. Attaching every song's full analysis here cost
                # 3.1 MB on a 177-song playlist (14x) that nothing ever read.
                entry["stats"] = s
            songs.append(entry)
        user_analyzed = conn.execute(
            "SELECT COUNT(*) FROM songs WHERE user_id = ? AND analysis IS NOT NULL",
            (user["id"],)).fetchone()[0]
    return {"playlist": dict(pl), "songs": songs, "user_analyzed_count": user_analyzed}


@app.delete("/api/playlists/{pid}")
def delete_playlist(pid: int, user=Depends(auth.current_user)):
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM playlists WHERE id = ? AND user_id = ?",
                           (pid, user["id"])).fetchone()
        if not row:
            raise HTTPException(404, "Playlist not found")
        conn.execute("DELETE FROM songs WHERE playlist_id = ? AND user_id = ?", (pid, user["id"]))
        conn.execute("DELETE FROM playlists WHERE id = ?", (pid,))
    return {"ok": True}


# ---------- songs ----------

class SongIn(BaseModel):
    artist: str = ""
    title: str
    playlist_id: int | None = None


class SongListIn(BaseModel):
    text: str
    playlist_id: int | None = None


class LyricsIn(BaseModel):
    text: str


def _own_song(conn, sid, user_id):
    row = conn.execute("SELECT * FROM songs WHERE id = ? AND user_id = ?", (sid, user_id)).fetchone()
    if not row:
        raise HTTPException(404, "Song not found")
    return row


def _fresh_analysis(conn, row):
    """Return the song's analysis, re-analyzing from its stored lyrics if the
    stored one predates the current analysis version (e.g. new stat fields)."""
    if not row["analysis"]:
        return _adopt_seeded(conn, row)
    a = json.loads(row["analysis"])
    current = analyze.hskdata.config()["analysis_version"]
    if a.get("version") == current or not row["lyrics"]:
        return a
    a = analyze.analyze(row["lyrics"])
    conn.execute("UPDATE songs SET analysis = ? WHERE id = ?", (db.to_json(a), row["id"]))
    return a


def _adopt_seeded(conn, row):
    """Give an un-analyzed song the shared corpus's analysis, if we have one.

    Without this the corpus is invisible to everyone but the account that
    seeded it: a new user importing a playlist we have fully analyzed saw
    0/100, because only the explicit autofetch endpoint ever read the corpus.
    That is the opposite of the point -- one resolution is meant to serve
    everybody.

    Corpus read only. No network, so this is safe on a read path and needs no
    rate limit; a miss simply leaves the song un-analyzed, exactly as before.
    The result is text-free (stats and counts, never lines), so adopting it
    tells the user nothing about anyone else's uploaded lyrics.
    """
    from . import seed
    if row["lyrics"]:
        return None                 # their own lyrics win; nothing to adopt
    version = analyze.hskdata.config()["analysis_version"]
    found = seed.lookup(conn, row["artist"] or "", row["title"], version)
    if not found:
        return None
    ghost, h = found
    conn.execute("UPDATE songs SET analysis = ?, lyrics_hash = ? WHERE id = ?",
                 (db.to_json(ghost), h, row["id"]))
    return ghost


def _own_playlist(conn, pid, user_id):
    if pid is None:
        return
    if not conn.execute("SELECT id FROM playlists WHERE id = ? AND user_id = ?",
                        (pid, user_id)).fetchone():
        raise HTTPException(404, "Playlist not found")


@app.get("/api/songs")
def list_songs(user=Depends(auth.current_user)):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, artist, title, playlist_id, analysis IS NOT NULL AS analyzed "
            "FROM songs WHERE user_id = ? ORDER BY created DESC", (user["id"],)).fetchall()
    return [dict(r) | {"analyzed": bool(r["analyzed"]),
                       "artist_slug": public.slug_for(r["artist"]),
                       "song_path": public.song_path_for(r["artist"], r["title"])}
            for r in rows]


@app.post("/api/songs")
def create_song(body: SongIn, user=Depends(auth.current_user)):
    if not body.title.strip():
        raise HTTPException(400, "Title required")
    with db.connect() as conn:
        _own_playlist(conn, body.playlist_id, user["id"])
        cur = conn.execute("INSERT INTO songs(user_id, playlist_id, artist, title) VALUES (?,?,?,?)",
                           (user["id"], body.playlist_id, body.artist.strip(), body.title.strip()))
    return {"id": cur.lastrowid}


@app.post("/api/songs/import")
def import_songs(body: SongListIn, user=Depends(auth.current_user)):
    """Manual list, one song per line: 'Artist - Title' (or just 'Title')."""
    created = []
    with db.connect() as conn:
        _own_playlist(conn, body.playlist_id, user["id"])
        for line in body.text.splitlines():
            line = line.strip()
            if not line:
                continue
            artist, _, title = line.partition(" - ")
            if not title:
                artist, title = "", line
            cur = conn.execute(
                "INSERT INTO songs(user_id, playlist_id, artist, title) VALUES (?,?,?,?)",
                (user["id"], body.playlist_id, artist.strip(), title.strip()))
            created.append(cur.lastrowid)
    if not created:
        raise HTTPException(400, "No songs found in the list")
    return {"created": len(created), "ids": created}


@app.get("/api/songs/{sid}")
def get_song(sid: int, user=Depends(auth.current_user)):
    with db.connect() as conn:
        row = _own_song(conn, sid, user["id"])
        a = _fresh_analysis(conn, row)
        if a:
            a = analyze.personalize(a, _known_set(conn, user["id"]))
    return {
        "id": row["id"], "artist": row["artist"], "title": row["title"],
        "artist_slug": public.slug_for(row["artist"]),
        "song_path": public.song_path_for(row["artist"], row["title"]),
        "playlist_id": row["playlist_id"], "has_lyrics": row["lyrics"] is not None,
        "analysis": dictionary.annotate(a),
    }


@app.post("/api/songs/{sid}/autofetch")
def autofetch_song(sid: int, request: Request, user=Depends(auth.current_user)):
    """Analyze a song without the user supplying lyrics.

    Served from the shared seed corpus when we've already resolved this
    (artist, title); otherwise the lyrics are fetched transiently, analyzed,
    and the text-free result is stored — both on the song and in the corpus
    for other users. The lyric text itself is never persisted: the song's
    lyrics column stays NULL."""
    from . import seed
    rate_limit(request, "autofetch", 30)
    # Protects the one asset the corpus pipeline can't replace: our standing
    # with the upstream lyric services, which see a single IP.
    global_limit("autofetch", AUTOFETCH_GLOBAL_PER_MIN,
                 message="Auto-analyze is busy right now — try again in a minute, "
                         "or paste the lyrics yourself.")
    with db.connect() as conn:
        row = _own_song(conn, sid, user["id"])
        if row["lyrics"]:
            raise HTTPException(409, "This song already has lyrics")

    # seed.acquire is the single sanctioned path: corpus read, negative cache,
    # fetch, write-back and the text-free guarantee, applied together.
    reason, ghost, h = seed.acquire(row["artist"] or "", row["title"])
    if reason in ("nomatch", "miss-cached"):
        raise HTTPException(404, "No confident lyrics match found — paste them manually")
    if reason == "nochinese":
        raise HTTPException(422, "Found a match but it has no Chinese text")
    with db.connect() as conn:
        _own_song(conn, sid, user["id"])
        conn.execute("UPDATE songs SET analysis = ?, lyrics_hash = ? WHERE id = ?",
                     (db.to_json(ghost), h, sid))
        conn.execute("INSERT OR REPLACE INTO analysis_cache(lyrics_hash, version, stats) VALUES (?,?,?)",
                     (h, ghost["version"], db.to_json(ghost["stats"])))
        ghost = analyze.personalize(ghost, _known_set(conn, user["id"]))
    return {"ok": True, "analysis": dictionary.annotate(ghost)}


@app.put("/api/songs/{sid}/lyrics")
def set_lyrics(sid: int, body: LyricsIn, user=Depends(auth.current_user)):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Lyrics are empty")
    if len(text) > 50000:
        raise HTTPException(413, "Lyrics too long")
    result = analyze.analyze(text)
    if result["stats"]["chinese_tokens"] == 0:
        raise HTTPException(422, "No Chinese text found in those lyrics")
    h = analyze.lyrics_hash(text)
    with db.connect() as conn:
        _own_song(conn, sid, user["id"])
        conn.execute("UPDATE songs SET lyrics = ?, lyrics_hash = ?, analysis = ? WHERE id = ?",
                     (text, h, db.to_json(result), sid))
        conn.execute("INSERT OR REPLACE INTO analysis_cache(lyrics_hash, version, stats) VALUES (?,?,?)",
                     (h, result["version"], db.to_json(result["stats"])))
        result = analyze.personalize(result, _known_set(conn, user["id"]))
    return {"ok": True, "analysis": dictionary.annotate(result)}


@app.delete("/api/songs/{sid}")
def delete_song(sid: int, user=Depends(auth.current_user)):
    with db.connect() as conn:
        _own_song(conn, sid, user["id"])
        conn.execute("DELETE FROM songs WHERE id = ?", (sid,))
    return {"ok": True}


# ---------- static frontend ----------

# Assets are referenced as /static/x.js?v=<content hash>, so they can be
# cached forever; only the small HTML shell revalidates (a 304 on a request
# the browser makes anyway).
def _asset_version():
    h = hashlib.md5()
    for name in ("app.js", "telemetry.js", "style.css"):
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:10]


def _page_html(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as f:
        html = f.read().replace("{{v}}", _asset_version())
    # One nav for the whole site, defined once in public.py.
    html = html.replace("{{nav}}", public.NAV_HTML)
    # The app shell carries its own level control (it also drives the
    # known-words mode); only the static article pages need this one.
    html = html.replace("{{levelbox}}", public.LEVELBOX_HTML)
    html = html.replace("{{author}}", public.author_block())
    # The Turnstile script is only loaded when a site key is configured, so an
    # unconfigured install pulls nothing from Cloudflare.
    tag = (f'<script>window.TURNSTILE_SITE_KEY="{captcha.SITE_KEY}";'
           f'window.TURNSTILE_ACTION="{captcha.ACTION}";</script>'
           f'<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"'
           f' async defer></script>') if captcha.enabled() else \
          '<script>window.TURNSTILE_SITE_KEY="";</script>'
    return html.replace("{{captcha}}", tag)


INDEX_HTML = _page_html("index.html")
ABOUT_HTML = _page_html("about.html")


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif request.url.path in ("/", "/about"):
        response.headers["Cache-Control"] = "no-cache"
    elif request.url.path.startswith(("/artists", "/artist/", "/song/")):
        # Aggregates change only when the corpus is re-seeded.
        response.headers["Cache-Control"] = "public, max-age=1800"
    return response


@app.get("/")
def index():
    return Response(INDEX_HTML, media_type="text/html")


@app.get("/about")
def about():
    return Response(ABOUT_HTML, media_type="text/html")


# --- public difficulty pages (OPTIONAL) --------------------------------------
# The crawlable /artists, /artist/, /song/, /chengyu/ surface is wired in
# app/public_pages.py, which is gitignored from this repo and absent on a fresh
# clone. Registering it here is best-effort: if the file is present (as it is
# on the production deploy) the public pages come up; if not, this app is the
# analysis tool alone and the routes below simply don't exist. The core API and
# app shell never depend on it.
try:
    from . import public_pages  # type: ignore
    _PUBLIC_ORIGIN = public_pages.register(app)
except ImportError:
    _PUBLIC_ORIGIN = None


# Only "/" and "/about" are public; everything else needs a session, so keep
# crawlers out of the API and the app shell rather than letting them collect
# 401s.
_ROBOTS_DISALLOW = ("User-agent: *\n"
                    "Disallow: /api/\n"
                    "Disallow: /static/\n")
_ROBOTS_PUBLIC = ("Allow: /$\n"
                 "Allow: /about\n"
                 # The link-preview card MUST stay crawlable — Twitterbot and
                 # friends honour robots.txt, so disallowing it silently kills
                 # the image on every shared link.
                 "Allow: /static/card.png\n")


@app.get("/robots.txt")
def robots():
    body = _ROBOTS_PUBLIC if _PUBLIC_ORIGIN else ""
    body += _ROBOTS_DISALLOW
    if _PUBLIC_ORIGIN:
        body += f"\nSitemap: {_PUBLIC_ORIGIN}/sitemap.xml\n"
    return Response(body, media_type="text/plain")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
