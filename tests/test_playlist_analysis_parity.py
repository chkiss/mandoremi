"""Regression guard: a song in a playlist must render exactly like a
standalone song, and the playlist listing must stay cheap.

History, in two parts.

1. The symptom: opening a song that lived in a playlist showed no chengyu,
   grammar or vocabulary. The song view reads `currentSong.analysis.{idioms,
   grammar,vocab,lines}`.

2. The fix that overshot: `/api/playlists/{pid}` was changed to attach every
   song's FULL analysis. But `showSong()` fetches `/api/songs/{id}` for the one
   song being opened, and nothing in the front end reads `analysis` off a
   playlist row -- so that payload was never used, while a 177-song playlist
   grew from 220 KB to 3.1 MB and the shared `_stats_cache` (capped by entry
   count, not bytes) started holding 17 KB analyses instead of 1 KB stats.

So the contract is: the SONG endpoint carries everything, the PLAYLIST endpoint
carries stats. Both halves are asserted here — the first stops the panels going
missing again, the second stops the payload being re-inflated to fix it.

No network: the song is analyzed with the local pipeline (a chengyu is planted
in the lyrics so we can assert it survives).
"""
import json

from conftest import register

SONG_WITH_IDIOM = """不知不觉我爱上了你
一无所有也不后悔
小心翼翼地守护
"""


def _make_playlist(client, name="p1"):
    return client.post("/api/playlists", json={"name": name}).json()["id"]


def _add_to_playlist(client, pid, artist, title):
    return client.post("/api/songs",
                       json={"artist": artist, "title": title,
                             "playlist_id": pid}).json()["id"]


def _analyze_into_song(client, sid):
    # Offline analysis (no NetEase): run the local pipeline via /api/analyze,
    # then write the result into the song row the way autofetch would.
    from app import db
    resp = client.post("/api/analyze", json={"text": SONG_WITH_IDIOM})
    assert resp.status_code == 200, resp.text
    analysis = resp.json()["analysis"]
    with db.connect() as conn:
        conn.execute("UPDATE songs SET analysis = ? WHERE id = ?",
                     (json.dumps(analysis), sid))
        conn.commit()


def test_song_endpoint_serves_full_analysis_for_a_playlist_song(client):
    """The path the song view actually takes. This is what stops the chengyu,
    grammar and vocabulary panels going blank for a song inside a playlist."""
    register(client)
    pid = _make_playlist(client)
    sid = _add_to_playlist(client, pid, "测试歌手", "测试歌")
    _analyze_into_song(client, sid)

    a = client.get(f"/api/songs/{sid}").json()["analysis"]
    assert a, "playlist song has no analysis on the song endpoint"
    for key in ("idioms", "grammar", "vocab", "lines", "stats"):
        assert key in a, f"analysis missing {key} for a playlist song"
    words = {i["word"] for i in a["idioms"]}
    assert "不知不觉" in words and "一无所有" in words, "chengyu lost"
    assert a["stats"]["chinese_tokens"] > 0


def test_playlist_listing_carries_stats_only(client):
    """The listing must not ship every song's full analysis: the table renders
    from `stats`, and the front end re-fetches the one song you open."""
    register(client)
    pid = _make_playlist(client)
    sid = _add_to_playlist(client, pid, "测试歌手", "测试歌")
    _analyze_into_song(client, sid)

    song = client.get(f"/api/playlists/{pid}").json()["songs"][0]
    assert song["analyzed"] is True
    assert song["stats"]["chinese_tokens"] > 0
    assert "analysis" not in song, (
        "playlist rows must stay stats-only — attaching the full analysis "
        "made a 177-song playlist 14x larger for data nothing reads")
    # the fields the table and its links genuinely need
    for key in ("id", "artist", "title", "artist_slug", "song_path"):
        assert key in song


def test_playlist_listing_stays_small(client):
    """Size guard with teeth: the listing must stay far closer to stats-only
    than to full-analysis, whatever future fields get added."""
    register(client)
    pid = _make_playlist(client)
    for i in range(8):
        sid = _add_to_playlist(client, pid, "测试歌手", f"歌{i}")
        _analyze_into_song(client, sid)

    listing = client.get(f"/api/playlists/{pid}").json()
    per_song = len(json.dumps(listing["songs"], ensure_ascii=False)) / 8
    full = len(json.dumps(
        client.get(f"/api/songs/{sid}").json()["analysis"], ensure_ascii=False))
    assert per_song < full / 2, (
        f"playlist row is {per_song:.0f}B against a {full:.0f}B analysis — "
        "the full analysis has leaked back into the listing")
