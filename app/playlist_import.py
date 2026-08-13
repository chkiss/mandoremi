"""Public-playlist import: Spotify, NetEase, YouTube, Apple Music.
All best-effort scrapes of public pages/APIs — no auth, fail soft with a
clear error. Parsers are pure functions over fetched data for testability."""
import html
import json
import re

import requests

from . import spotify

UA = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")}


class PlaylistImportError(Exception):
    pass


def fetch(url):
    """Return {"name", "tracks": [{"artist","title"}], "capped": bool}."""
    u = url.lower()
    if "spotify" in u:
        try:
            return spotify.playlist_tracks(url)
        except spotify.SpotifyScrapeError as e:
            raise PlaylistImportError(str(e))
    if "music.163.com" in u or "163cn.tv" in u:
        return _fetch_netease(url)
    if "youtube.com" in u or "youtu.be" in u:
        return _fetch_youtube(url)
    if "music.apple.com" in u:
        return _fetch_apple(url)
    raise PlaylistImportError(
        "Unsupported playlist URL — Spotify, Apple Music, YouTube and "
        "NetEase (music.163.com) links work")


# ---------- NetEase 网易云 ----------

def _fetch_netease(url):
    m = re.search(r"[?&]id=(\d+)", url) or re.search(r"/playlist/(\d+)", url)
    if not m:
        raise PlaylistImportError("Could not find a NetEase playlist id in that URL")
    r = requests.get(f"http://music.163.com/api/playlist/detail?id={m.group(1)}",
                     headers=UA | {"Referer": "http://music.163.com/",
                                   "Cookie": "appver=2.0.2"}, timeout=20)
    if r.status_code != 200:
        raise PlaylistImportError(f"NetEase returned HTTP {r.status_code}")
    return parse_netease(r.json())


def parse_netease(data):
    res = data.get("result") or {}
    tracks = [{"artist": (t.get("artists") or [{}])[0].get("name", ""),
               "title": t.get("name", "")}
              for t in res.get("tracks", []) if t.get("name")]
    if not tracks:
        raise PlaylistImportError("No tracks found — NetEase playlist may be "
                                  "private, empty, or the API shape changed")
    return {"name": res.get("name", "NetEase playlist"), "tracks": tracks,
            "capped": len(tracks) >= 1000}


# ---------- YouTube ----------

YT_NOISE = re.compile(
    r"\s*[(\[【][^)\]】]*(?:official|video|audio|lyric|lyrics|mv|m/v|hd|4k|visualizer"
    r"|color coded|eng sub)[^)\]】]*[)\]】]", re.I)


def _fetch_youtube(url):
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url)
    if not m:
        raise PlaylistImportError("Could not find a YouTube playlist id (list=…) in that URL")
    r = requests.post(
        "https://www.youtube.com/youtubei/v1/browse?key=AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
        json={"context": {"client": {"clientName": "WEB",
                                     "clientVersion": "2.20240701.00.00", "hl": "en"}},
              "browseId": "VL" + m.group(1)},
        headers=UA, timeout=20)
    if r.status_code != 200:
        raise PlaylistImportError(f"YouTube returned HTTP {r.status_code}")
    return parse_youtube(r.json())


def parse_youtube(data):
    try:
        items = (data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"][0]
                 ["tabRenderer"]["content"]["sectionListRenderer"]["contents"][0]
                 ["itemSectionRenderer"]["contents"])
    except (KeyError, IndexError, TypeError):
        raise PlaylistImportError("Could not read the YouTube playlist "
                                  "(private, deleted, or format changed)")
    tracks = []
    for it in items:
        title = ""
        lv = it.get("lockupViewModel")
        if lv:  # current markup
            title = (lv.get("metadata", {}).get("lockupMetadataViewModel", {})
                     .get("title", {}).get("content", ""))
        elif "playlistVideoRenderer" in it:  # older markup
            runs = it["playlistVideoRenderer"].get("title", {}).get("runs", [])
            title = runs[0].get("text", "") if runs else ""
        if not title:
            continue
        title = YT_NOISE.sub("", title).strip()
        artist = ""
        if " - " in title:
            artist, title = (s.strip() for s in title.split(" - ", 1))
        tracks.append({"artist": artist, "title": title})
    if not tracks:
        raise PlaylistImportError("No videos found in that YouTube playlist")
    name = (data.get("metadata", {}).get("playlistMetadataRenderer", {})
            .get("title", "YouTube playlist"))
    # the browse response serves 100 items; the rest need continuations
    return {"name": name, "tracks": tracks, "capped": len(tracks) >= 100}


# ---------- Apple Music ----------

APPLE_DATA_RE = re.compile(
    r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', re.S)
APPLE_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')


def _fetch_apple(url):
    r = requests.get(url, headers=UA, timeout=20)
    if r.status_code != 200:
        raise PlaylistImportError(f"Apple Music returned HTTP {r.status_code}")
    r.encoding = "utf-8"
    return parse_apple(r.text)


def parse_apple(page):
    m = APPLE_DATA_RE.search(page)
    if not m:
        raise PlaylistImportError("Could not find playlist data in the Apple "
                                  "Music page (format may have changed)")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise PlaylistImportError(f"Bad Apple Music data: {e}")
    tracks = []

    def walk(node, depth=0):
        if depth > 14 or len(tracks) > 2000:
            return
        if isinstance(node, dict):
            if node.get("title") and node.get("artistName"):
                tracks.append({"artist": str(node["artistName"]),
                               "title": str(node["title"])})
                return
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(data)
    if not tracks:
        raise PlaylistImportError("No tracks found in that Apple Music playlist")
    tm = APPLE_TITLE_RE.search(page)
    name = html.unescape(tm.group(1)) if tm else "Apple Music playlist"
    return {"name": name, "tracks": tracks, "capped": False}
