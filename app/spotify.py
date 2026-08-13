"""Best-effort track-name extraction from a public Spotify playlist page.
No OAuth: parses the embed page's __NEXT_DATA__ JSON. Subject to breakage if
Spotify changes their embed markup — fail soft with a clear error."""
import json
import re

import requests

PLAYLIST_ID_RE = re.compile(r"(?:playlist[/:])([A-Za-z0-9]{16,})")
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S)

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")


class SpotifyScrapeError(Exception):
    pass


def playlist_tracks(url):
    """Return {"name": playlist_name, "tracks": [{"artist","title"}]}"""
    m = PLAYLIST_ID_RE.search(url)
    if not m:
        raise SpotifyScrapeError("Not a Spotify playlist URL")
    pid = m.group(1)
    resp = requests.get(f"https://open.spotify.com/embed/playlist/{pid}",
                        headers={"User-Agent": UA}, timeout=20)
    if resp.status_code != 200:
        raise SpotifyScrapeError(f"Spotify returned HTTP {resp.status_code}")
    m = NEXT_DATA_RE.search(resp.text)
    if not m:
        raise SpotifyScrapeError("Could not find playlist data in the page "
                                 "(Spotify may have changed their embed format)")
    try:
        data = json.loads(m.group(1))
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
        name = entity.get("name", "Spotify playlist")
        tracks = []
        for item in entity.get("trackList", []):
            title = item.get("title") or ""
            artist = item.get("subtitle") or ""
            if title:
                tracks.append({"artist": artist, "title": title})
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        raise SpotifyScrapeError(f"Unexpected playlist data layout: {e}")
    if not tracks:
        raise SpotifyScrapeError("No tracks found — playlist may be private or empty")
    # the embed page serves at most 100 tracks and no total count
    return {"name": name, "tracks": tracks, "capped": len(tracks) >= 100}
