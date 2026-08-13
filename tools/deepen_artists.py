#!/usr/bin/env python3
"""Re-discover tracklists for artists the first deep pass under-covered.

Why this exists (the 周杰伦 case):

discover_deep.py ranks an artist's albums by how many of their tracks appear in
that artist's NetEase `hotSongs` list. For most artists that works. For a
prolific songwriter it fails badly -- 39 of 周杰伦's 50 "hot songs" are credited
to 蔡依林, 王力宏, 李玟 and others, because he wrote or featured on them. His own
albums therefore score ~zero overlap and rank last, while the 12 largest albums
it does examine are concert recordings whose every track SKIP_RE discards. Net
result: 16 songs seeded for the single most important artist in Mandopop,
against 42 for deca joins.

So this pass changes three things:

  * consider EVERY album with enough tracks, not the 12 largest
  * drop live albums, compilations and soundtracks by ALBUM name -- previously
    they were only caught per-track, after they had already crowded out the
    studio records
  * accept a track if the artist is credited ANYWHERE on it, not only first,
    since duets on an artist's own album list the guest first often enough

Discovery only: writes a manifest, touches no database. Feed it to
tools/seed_corpus.py, which honours the negative cache and the text-free
guarantee.

  ./.venv/bin/python tools/deepen_artists.py --min-songs 45 [--dry-run]
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import discover_deep as D  # noqa: E402

OUT = os.path.expanduser("~/deepen_manifest.json")
DB = os.path.expanduser("~/hsk-lyrics/hsklyrics.db")

# Album-level exclusions. A concert album's tracks are the studio songs again,
# but degraded and usually unmatchable for lyrics; a compilation is duplicates;
# a soundtrack is mostly other artists.
ALBUM_SKIP = re.compile(
    r"(演唱会|巡回|巡迴|live|精选|精選|新歌\+?精选|合辑|合輯|典藏|全集|"
    r"原声带|原聲帶|ost|karaoke|伴奏|remix|instrumental|单曲|單曲)", re.I)
MIN_ALBUM_TRACKS = 4


def credited(song, name, first_only=False):
    """Is `name` credited on this track?

    first_only is the right rule for the hot-songs list, which is full of other
    artists' records this one guested on (布拉格广场 is 蔡依林's song, not
    周杰伦's). It is the wrong rule for tracks on the artist's OWN album, where
    a duet often lists the guest first -- hence the two modes.
    """
    people = song.get("artists") or song.get("ar") or []
    if first_only:
        people = people[:1]
    return any((a.get("name") or "").strip() == name for a in people)


def current_counts():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    counts = {}
    for aid, n in conn.execute(
            "SELECT artist_id, COUNT(*) FROM seed_analysis "
            "WHERE artist_id IS NOT NULL GROUP BY artist_id"):
        counts[aid] = n
    names = {}
    for aid, disp in conn.execute(
            "SELECT artist_id, display FROM artist_alias"):
        names[aid] = disp
    conn.close()
    return counts, names


def deepen(aid, name, want, pause=0.35):
    """Return a list of {title, src} for one artist."""
    songs, seen = [], set()
    hot = (D.get(f"artist/{aid}") or {}).get("hotSongs") or []
    # Hot songs still count, but only the ones actually by this artist.
    for s in hot:
        if credited(s, name, first_only=True):
            t = (s.get("name") or "").strip()
            k = D.key(t)
            if t and k not in seen and not D.SKIP_RE.search(t):
                seen.add(k)
                songs.append({"title": t, "src": "hot"})

    albums = (D.get(f"artist/albums/{aid}?limit=200") or {}).get("hotAlbums") or []
    studio = [al for al in albums
              if (al.get("size") or 0) >= MIN_ALBUM_TRACKS
              and not ALBUM_SKIP.search(al.get("name") or "")]
    # Biggest studio albums first: they carry the singles a listener knows.
    studio.sort(key=lambda al: -(al.get("size") or 0))
    for al in studio:
        if len(songs) >= want:
            break
        try:
            full = D.get(f"album/{al['id']}")
        except Exception as exc:                        # noqa: BLE001
            print(f"      album {al.get('name')!r}: {exc}")
            continue
        tracks = ((full or {}).get("album", {}).get("songs")
                  or (full or {}).get("songs") or [])
        for s in tracks:
            t = (s.get("name") or "").strip()
            k = D.key(t)
            if not t or k in seen or D.SKIP_RE.search(t):
                continue
            if not credited(s, name):
                continue
            seen.add(k)
            songs.append({"title": t, "src": "album"})
        time.sleep(pause)
    return songs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-songs", type=int, default=45,
                    help="artists below this many seeded songs get deepened")
    ap.add_argument("--want", type=int, default=90,
                    help="stop collecting an artist at this many titles")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    counts, names = current_counts()
    todo = []
    for aid, n in sorted(counts.items(), key=lambda kv: kv[1]):
        if n < args.min_songs:
            todo.append((aid, names.get(aid, str(aid)), n))
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(counts)} artists with ids; {len(todo)} under {args.min_songs} songs")

    result, problems = [], []
    for i, (aid, name, have) in enumerate(todo, 1):
        try:
            songs = deepen(aid, name, args.want)
        except Exception as exc:                        # noqa: BLE001
            problems.append(f"{name}: {exc}")
            print(f"{i:3d}/{len(todo)} {name[:18]:20s} FAILED {exc}", flush=True)
            continue
        result.append({"artist": name, "netease_id": aid, "songs": songs})
        print(f"{i:3d}/{len(todo)} {name[:18]:20s} had {have:3d} -> found "
              f"{len(songs):3d} candidates", flush=True)
        if not args.dry_run:
            json.dump({"artists": result, "problems": problems},
                      open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
        time.sleep(0.4)

    total = sum(len(a["songs"]) for a in result)
    print(f"\n{len(result)} artists, {total} candidate titles"
          + (" (dry run, nothing written)" if args.dry_run else f" -> {OUT}"))
    for p in problems:
        print("PROBLEM:", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
