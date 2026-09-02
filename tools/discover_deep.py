#!/usr/bin/env python3
"""Deep tracklists: the 50-song popularity list UNION the 5 highest-signal albums.

Usage:  discover_deep.py [chart|indie|9ini|avalanche]

  chart      NetEase 华语 top-100 leaderboard    -> deep_manifest.json
  indie      r/ChineseLanguage recommendations   -> indie_deep_manifest.json
  9ini       artists in the user's playlist 1    -> 9ini_deep_manifest.json
  avalanche  Concrete Avalanche newsletter       -> avalanche_deep_manifest.json
             (run tools/discover_avalanche.py scrape && resolve first)

Album ranking note: `artist/albums?limit=5` returns the most RECENT releases,
which for a big artist is mostly 1-track singles -- useless for coverage. So we
pull the whole album list and rank albums by how many of their tracks appear in
the artist's own popularity list, with track count as the tie-break.

Discovery only: writes a manifest, touches no database.
"""
import json
import re
import sqlite3
import sys
import os
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.expanduser("~/hsk-lyrics"))

API = "https://music.163.com/api"
HDRS = {
    "Cookie": "appver=2.0.2",
    "Referer": "https://music.163.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
}
TOP_ALBUMS = 5
MIN_ALBUM_TRACKS = 4
SKIP_RE = re.compile(r"(live|remix|instrumental|伴奏|现场|純音樂|纯音乐|demo|"
                     r"cover|karaoke|acoustic version|off vocal)", re.I)
PUNCT_RE = re.compile(r"[\s'’\-_.,!?()（）]+")
# Han suffixes that decorate a band name without changing who it is:
# 法兹 / 法兹乐队, 李高特三重奏, 五条人组合.
GENERIC_SUFFIX_RE = re.compile(r"(乐队|樂隊|乐团|樂團|组合|組合|三重奏|二重奏|四重奏|"
                               r"实验室|實驗室|工作室)")


def key(t):
    return PUNCT_RE.sub("", (t or "").lower())


def latin_tokens(s):
    """{'ronghao','li'} for 'Ronghao Li' -- order-insensitive, so it also
    matches NetEase's alias 'Li Ronghao'."""
    return frozenset(w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w)


def get(path, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(f"{API}/{path}", headers=HDRS)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:                               # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    return None


# Artists whose NetEase page shares no name with the Spotify string, so no
# rule can bridge them. Verified individually before being listed here.
ID_OVERRIDES = {
    "finn liu": 4469,          # 刘凤瑶 -- NetEase exposes no latin alias
    "a-mei chang": 10559,      # 张惠妹 (aliases aMEI/阿妹); the literal
                               # "A-Mei Chang" page is a 0-song stub
}


def pick_best(query, extra_names, candidates):
    """Choose among NetEase search hits, or None. Pure: no network, so the
    ranking rules below are unit-testable against recorded payloads.
    """
    from app import normalize

    want_norm = {key(normalize.to_simplified(q)) for q in (query, *extra_names) if q}
    want_lat = {latin_tokens(q) for q in (query, *extra_names) if latin_tokens(q)}

    def han_related(n):
        """'舒大卫' vs '舒大卫Dizzy Boy', '小老虎J-Fever' vs '小老虎' -- one name
        carries a suffix the other doesn't. Require a Han run of 2+ chars so
        this can't fire on incidental latin overlap."""
        a = key(normalize.to_simplified(n))
        for w in want_norm:
            if not w or not a:
                continue
            short, long = (w, a) if len(w) <= len(a) else (a, w)
            if len(short) < 2 or not long.startswith(short):
                continue
            if not re.search(r"[一-鿿]{2}", short):
                continue
            # The suffix must not be more Han name. '小老虎' + 'J-Fever' is the
            # same act under a stage name; '李志' + '洲' is a different person,
            # as are 张梦/张梦奇, 郑好/郑好儿 and 颜羽/颜羽汐. Generic band words
            # are the one Han suffix that still means the same act.
            rest = long[len(short):]
            rest = GENERIC_SUFFIX_RE.sub("", rest)
            if not re.search(r"[一-鿿]", rest):
                return True
        return False
    # Rank candidates by HOW they matched, then take the largest catalog WITHIN
    # the best tier. NetEase carries romanized STUB pages ("Mao Buyi", "A-Mei
    # Chang") that match perfectly but hold no songs, which is why catalog size
    # is a tie-break at all -- but it must never outrank a better match. It did:
    # searching 野孩子 returned the real 野孩子 (129 songs) AND 羽·泉, who merely
    # list "野孩子" as an alias (375 songs), and the bigger catalog won. Same for
    # 水树 -> 水樹奈々 and 鼠鼠鼠 -> 仓鼠ミツキ.
    #   tier 0: the artist's own name matches
    #   tier 1: one of their aliases or translated names matches
    #   tier 2: prefix-related (小老虎 vs 小老虎J-Fever)
    ok = []
    for a in candidates or []:
        # Drop empty stub pages BEFORE tiering, not after. NetEase carries
        # 0-song romanized pages that match the name perfectly; if one of those
        # takes tier 0 it shuts out the real artist sitting in a lower tier, and
        # the whole query resolves to nothing.
        if not (a.get("musicSize") or 0):
            continue
        own = [a.get("name", "")]
        alt = list(a.get("alias") or []) + list(a.get("transNames") or [])
        tier = None
        for names, t in ((own, 0), (alt, 1)):
            for n in names:
                if (key(normalize.to_simplified(n)) in want_norm
                        or (latin_tokens(n) and latin_tokens(n) in want_lat)):
                    tier = t
                    break
            if tier is not None:
                break
        if tier is None and any(han_related(n) for n in own + alt):
            tier = 2
        if tier is not None:
            ok.append((tier, a))
    if not ok:
        return None
    top = min(t for t, _ in ok)
    best = max((a for t, a in ok if t == top),
               key=lambda a: (a.get("musicSize") or 0, a.get("albumSize") or 0))
    return best.get("name", ""), best["id"]


def search_artist(query, extra_names=()):
    """Resolve an artist string to (name, id), or None if no confident match.

    Accepts a hit when its name/alias equals the query after normalization, or
    matches as an unordered latin token set, or equals a known Chinese alias.
    Anything less confident returns None rather than guessing -- NetEase will
    happily return a same-named unrelated act.
    """
    if query.lower().strip() in ID_OVERRIDES:
        aid = ID_OVERRIDES[query.lower().strip()]
        d = get(f"artist/{aid}")
        return (d or {}).get("artist", {}).get("name", query), aid

    body = urllib.parse.urlencode({"s": query, "type": 100, "limit": 8}).encode()
    req = urllib.request.Request(f"{API}/search/get", data=body, headers=HDRS)
    with urllib.request.urlopen(req, timeout=25) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return pick_best(query, extra_names,
                     (d.get("result") or {}).get("artists") or [])


def load_artists(source):
    """Return ([(name, id)], [unresolved strings])."""
    if source == "chart":
        chart = json.load(open(os.path.expanduser("~/top_artists.json"), encoding="utf-8"))
        return [(a["name"], a["id"]) for a in chart["list"]["artists"]], []
    if source in ("indie", "avalanche"):
        # Both arrive pre-resolved to NetEase ids by their own discovery tool.
        # avalanche's list is already past its lyric gate -- the instrumental
        # and non-Mandarin acts sit in the manifest's low_lyric_yield list and
        # are deliberately not deepened.
        path = {"indie": "~/artists_manifest.json",
                "avalanche": "~/avalanche_manifest.json"}[source]
        man = json.load(open(os.path.expanduser(path), encoding="utf-8"))
        return [(a["netease_name"], a["netease_id"]) for a in man["artists"]], []

    # 9ini: artist strings come from Spotify, so they're latin-named, sometimes
    # collaborations, and must be resolved against NetEase before use.
    from app import lyrics_fetch

    conn = sqlite3.connect(os.path.expanduser("~/hsk-lyrics/hsklyrics.db"))
    rows = conn.execute("SELECT DISTINCT artist FROM songs WHERE playlist_id = 1").fetchall()
    conn.close()
    primaries, seen = [], set()
    for (raw,) in rows:
        for part in re.split(r"[,、&/]", raw or ""):
            p = part.strip()
            if p and p.lower() not in seen:
                seen.add(p.lower())
                primaries.append(p)

    out, bad = [], []
    for p in primaries:
        aliases = lyrics_fetch.ARTIST_ALIASES.get(p.lower(), [])
        try:
            hit = search_artist(p, aliases)
        except Exception as e:                          # noqa: BLE001
            bad.append(f"{p}: {e}")
            continue
        if hit:
            out.append(hit)
        else:
            bad.append(p)
        time.sleep(0.4)
    return out, bad


def collect(artist_name, songs, seen, out, source):
    for s in songs or []:
        title = (s.get("name") or "").strip()
        k = key(title)
        if not title or k in seen or SKIP_RE.search(title):
            continue
        primary = (s.get("artists") or s.get("ar") or [{}])[0].get("name", "")
        if primary and primary != artist_name:
            continue
        seen.add(k)
        out.append({"title": title, "src": source})


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else "chart"
    out_path = {"chart": os.path.expanduser("~/deep_manifest.json"),
                "indie": os.path.expanduser("~/indie_deep_manifest.json"),
                "9ini": os.path.expanduser("~/9ini_deep_manifest.json"),
                "avalanche": os.path.expanduser("~/avalanche_deep_manifest.json")}[source]
    artists, problems = load_artists(source)
    # skip artists already deepened by an earlier run
    done_ids = set()
    for prior in (os.path.expanduser("~/deep_manifest.json"),
                  os.path.expanduser("~/indie_deep_manifest.json"),
                  os.path.expanduser("~/9ini_deep_manifest.json"),
                  os.path.expanduser("~/avalanche_deep_manifest.json")):
        if prior == out_path:
            continue                    # a re-run must not skip its own output
        try:
            done_ids |= {a["netease_id"]
                         for a in json.load(open(prior, encoding="utf-8"))["artists"]}
        except Exception:                               # noqa: BLE001
            pass
    if source != "chart":
        skipped = [n for n, i in artists if i in done_ids]
        artists = [(n, i) for n, i in artists if i not in done_ids]
        print(f"skipping {len(skipped)} already-deepened: {', '.join(skipped[:8])}")
    print(f"source={source}  {len(artists)} artists -> {out_path}", flush=True)
    if problems:
        print(f"unresolved: {problems}")

    result = []
    for rank, (name, aid) in enumerate(artists, 1):
        songs, seen = [], set()
        try:
            hot = (get(f"artist/{aid}") or {}).get("hotSongs") or []
            collect(name, hot, seen, songs, "hot")
            hot_keys = {key(s.get("name")) for s in hot}

            albums = (get(f"artist/albums/{aid}?limit=200") or {}).get("hotAlbums") or []
            ranked = sorted([al for al in albums if (al.get("size") or 0) >= MIN_ALBUM_TRACKS],
                            key=lambda al: -(al.get("size") or 0))
            cands = []
            for al in ranked[:12]:
                try:
                    full = get(f"album/{al['id']}")
                except Exception:                       # noqa: BLE001
                    continue
                tracks = (full or {}).get("album", {}).get("songs") or (full or {}).get("songs") or []
                hits = sum(1 for t in tracks if key(t.get("name")) in hot_keys)
                cands.append((hits, len(tracks), tracks))
                time.sleep(0.35)
            cands.sort(key=lambda c: (-c[0], -c[1]))
            for _h, _n, tracks in cands[:TOP_ALBUMS]:
                collect(name, tracks, seen, songs, "album")
        except Exception as e:                          # noqa: BLE001
            problems.append(f"{name}: {e}")

        result.append({"rank": rank, "artist": name, "netease_id": aid, "songs": songs})
        print(f"{rank:3d} {name[:18]:20s} {len(songs):4d}", flush=True)
        json.dump({"artists": result, "problems": problems},
                  open(out_path, "w", encoding="utf-8"), ensure_ascii=False)
        time.sleep(0.4)

    total = sum(len(x["songs"]) for x in result)
    print(f"\n{len(result)} artists, {total} songs -> {out_path}")
    for p in problems:
        print("PROBLEM:", p)


if __name__ == "__main__":
    main()
