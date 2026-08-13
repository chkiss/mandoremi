from conftest import SONG, register


# ---------- anonymous free tier ----------

def test_analyze_anonymous_ok_with_glosses(client):
    r = client.post("/api/analyze", json={"text": SONG})
    assert r.status_code == 200
    a = r.json()["analysis"]
    assert a["stats"]["chinese_tokens"] > 0
    assert "moon" in a["vocab"]["月亮"]["g"]


def test_analyze_rejects_bad_input(client):
    assert client.post("/api/analyze", json={"text": "  "}).status_code == 400
    assert client.post("/api/analyze", json={"text": "hello world only"}).status_code == 422
    assert client.post("/api/analyze", json={"text": "月" * 50001}).status_code == 413


def test_analyze_rate_limited(client):
    codes = [client.post("/api/analyze", json={"text": "月亮"}).status_code
             for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


# ---------- auth ----------

def test_register_validation(client):
    bad = {"email": "not-an-email", "password": "longenough"}
    assert client.post("/api/register", json=bad).status_code == 400
    short = {"email": "a@b.co", "password": "short"}
    assert client.post("/api/register", json=short).status_code == 400
    register(client)
    dup = {"email": "alice@example.com", "password": "whatever123"}
    assert client.post("/api/register", json=dup).status_code == 409


def test_register_rate_limited(client):
    bad = {"email": "x", "password": "y"}
    codes = [client.post("/api/register", json=bad).status_code for _ in range(6)]
    assert codes[5] == 429


def test_login_lockout_after_three_failures(client):
    register(client)
    client.post("/api/logout")
    wrong = {"email": "alice@example.com", "password": "wrongwrong"}
    codes = [client.post("/api/login", json=wrong).status_code for _ in range(4)]
    assert codes[:2] == [401, 401]
    assert codes[2] == 403  # third failure locks
    assert codes[3] == 403  # and stays locked even for the right password
    good = {"email": "alice@example.com", "password": "hunter2secret"}
    assert client.post("/api/login", json=good).status_code == 403


def test_login_logout_cycle(client):
    register(client)
    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401
    r = client.post("/api/login", json={"email": "alice@example.com",
                                        "password": "hunter2secret"})
    assert r.status_code == 200
    assert client.get("/api/me").json()["email"] == "alice@example.com"


def test_protected_endpoints_require_auth(client):
    assert client.get("/api/playlists").status_code == 401
    assert client.get("/api/songs/1").status_code == 401
    assert client.put("/api/known", json={"text": "你"}).status_code == 401


# ---------- songs, lyrics, ownership ----------

def _make_song(client, title="月亮代表我的心", artist="邓丽君"):
    r = client.post("/api/songs", json={"artist": artist, "title": title})
    assert r.status_code == 200, r.text
    return r.json()["id"] if "id" in r.json() else r.json()["ids"][0]


def test_song_lyrics_flow(client):
    register(client)
    sid = _make_song(client)
    r = client.put(f"/api/songs/{sid}/lyrics", json={"text": SONG})
    assert r.status_code == 200
    a = r.json()["analysis"]
    assert a["stats"]["per_level"]["3"]["coverage"] > 0
    r = client.get(f"/api/songs/{sid}")
    assert r.status_code == 200
    assert "g" in r.json()["analysis"]["vocab"]["月亮"]


def test_lyrics_rejects_bad_input(client):
    register(client)
    sid = _make_song(client)
    assert client.put(f"/api/songs/{sid}/lyrics", json={"text": ""}).status_code == 400
    assert client.put(f"/api/songs/{sid}/lyrics", json={"text": "english"}).status_code == 422
    assert client.put(f"/api/songs/{sid}/lyrics", json={"text": "月" * 50001}).status_code == 413


def test_ownership_isolation(client):
    register(client, "alice@example.com")
    sid = _make_song(client)
    client.post("/api/logout")
    register(client, "bob@example.com", "bobpassword1")
    assert client.get(f"/api/songs/{sid}").status_code == 404
    assert client.put(f"/api/songs/{sid}/lyrics", json={"text": SONG}).status_code == 404
    assert client.delete(f"/api/songs/{sid}").status_code == 404


# ---------- known words + personalization ----------

def test_known_words_personalize_stats(client):
    register(client)
    sid = _make_song(client)
    client.put(f"/api/songs/{sid}/lyrics", json={"text": "轰轰烈烈的爱情"})
    base = client.get(f"/api/songs/{sid}").json()["analysis"]["stats"]
    r = client.put("/api/known", json={"text": "轰轰烈烈, 爱情"})
    assert r.status_code == 200 and r.json()["count"] >= 2
    pers = client.get(f"/api/songs/{sid}").json()["analysis"]["stats"]
    assert pers["per_level"]["0"]["coverage"] > base["per_level"]["0"]["coverage"]
    words = client.get("/api/known").json()["words"]
    assert "轰轰烈烈" in words
    # clearing works
    client.put("/api/known", json={"text": ""})
    assert client.get("/api/known").json()["count"] == 0


def test_known_add_appends(client):
    register(client)
    client.put("/api/known", json={"text": "没有"})
    r = client.post("/api/known/add", json={"words": ["没"]})
    assert r.status_code == 200
    words = client.get("/api/known").json()["words"]
    assert "没" in words and "没有" in words  # appended, not replaced
    # idempotent, normalizes traditional, rejects non-Chinese
    client.post("/api/known/add", json={"words": ["沒", "没"]})
    assert client.get("/api/known").json()["words"].count("没") == 1
    assert client.post("/api/known/add", json={"words": ["abc"]}).status_code == 400


def test_me_reports_known_count(client):
    register(client)
    assert client.get("/api/me").json()["known_count"] == 0
    client.put("/api/known", json={"text": "没有 学习"})
    assert client.get("/api/me").json()["known_count"] >= 2


def test_known_words_traditional_converted(client):
    register(client)
    client.put("/api/known", json={"text": "愛情"})
    assert "爱情" in client.get("/api/known").json()["words"]


# ---------- playlists ----------

def test_playlist_crud_and_averages(client):
    register(client)
    r = client.post("/api/playlists", json={"name": "Test"})
    pid = r.json()["id"]
    r = client.post("/api/songs", json={"artist": "a", "title": "t", "playlist_id": pid})
    sid = r.json()["id"]
    client.put(f"/api/songs/{sid}/lyrics", json={"text": SONG})

    pls = client.get("/api/playlists").json()
    assert pls[0]["songs"] == 1 and pls[0]["analyzed"] == 1
    assert 0 < pls[0]["avg"]["per_level"]["3"]["coverage"] <= 1

    detail = client.get(f"/api/playlists/{pid}").json()
    assert detail["songs"][0]["analyzed"] is True
    assert "stats" in detail["songs"][0]

    assert client.delete(f"/api/playlists/{pid}").status_code == 200
    assert client.get(f"/api/playlists/{pid}").status_code == 404


def test_playlist_needs_name_or_url(client):
    register(client)
    assert client.post("/api/playlists", json={}).status_code == 400


def test_stats_cache_invalidates_on_known_change(client):
    """Playlist stats are memoized; a known-list update must change the key."""
    register(client)
    pid = client.post("/api/playlists", json={"name": "P"}).json()["id"]
    sid = client.post("/api/songs", json={"title": "t", "playlist_id": pid}).json()["id"]
    client.put(f"/api/songs/{sid}/lyrics", json={"text": "轰轰烈烈的爱情"})
    before = client.get("/api/playlists").json()[0]["avg"]["per_level"]["0"]["coverage"]
    client.get("/api/playlists")  # warm the cache
    client.put("/api/known", json={"text": "轰轰烈烈 爱情"})
    after = client.get("/api/playlists").json()[0]["avg"]["per_level"]["0"]["coverage"]
    assert after > before


# ---------- transport hardening ----------

def test_gzip_on_large_json(client):
    register(client)
    sid = _make_song(client)
    client.put(f"/api/songs/{sid}/lyrics", json={"text": SONG * 20})
    r = client.get(f"/api/songs/{sid}", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"


def test_cache_headers(client):
    assert "no-cache" in client.get("/").headers.get("cache-control", "")
    r = client.get("/static/app.js")
    assert "immutable" in r.headers.get("cache-control", "")


def test_about_page(client):
    r = client.get("/about")
    assert r.status_code == 200
    assert "What is Mandoremi" in r.text
    assert "Anki" in r.text and "Roadmap" in r.text
    assert "no-cache" in r.headers.get("cache-control", "")
