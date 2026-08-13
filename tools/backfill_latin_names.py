#!/usr/bin/env python3
"""Give Chinese-named artists a latin URL slug, from NetEase's alias list.

Without this, /artist/<slug> falls back to a numeric handle -- 王菲 becomes
/artist/a9621 rather than /artist/fayewong. That matters because these pages
exist to be pasted into Reddit and Discord, where a numeric slug tells a reader
nothing and an English name they recognise tells them everything.

NetEase carries the English names in artist.alias (["王靖雯", "Faye Wong",
"Shirley Wong"]). We take the first entry that is actually latin, and write it
into artist_alias with confidence='latin-name', so it flows through the normal
slug/lookup machinery instead of being a second source of truth. Also means a
user typing "Faye Wong" now resolves to the same artist.

Idempotent, rate-limited, and skips artists that already have a latin alias.

  ./.venv/bin/python tools/backfill_latin_names.py [--dry-run] [--limit N]
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import artists, db  # noqa: E402

API = "http://music.163.com/api/artist/{}"
HEADERS = {"Cookie": "appver=2.0.2",
           "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
LATIN = re.compile(r"^[A-Za-z][A-Za-z0-9 .'\-]*$")


def latin_alias(names):
    """First alias that is plainly a latin-script personal name."""
    for n in names or []:
        n = (n or "").strip()
        # 2 chars is too short to be a name and is usually an initialism
        # collision ("JJ" is fine, but "AB" as a slug helps nobody).
        if len(n) >= 3 and LATIN.match(n):
            return n
    return None


def fetch(aid, timeout=15):
    req = urllib.request.Request(API.format(aid), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("artist") or {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--refresh-english", action="store_true",
                    help="re-fetch artists that have a latin alias but no "
                         "presentable English spelling stored")
    ap.add_argument("--pause", type=float, default=0.6)
    args = ap.parse_args()

    db.init()
    conn = db.connect()

    # Artists in the public corpus, and which already have a latin alias.
    rows = conn.execute(
        "SELECT artist_id, COUNT(*) n FROM seed_analysis "
        "WHERE artist_id IS NOT NULL GROUP BY artist_id ORDER BY n DESC"
    ).fetchall()
    have = {r["artist_id"] for r in conn.execute(
        "SELECT artist_id, alias_key FROM artist_alias")
        if re.fullmatch(r"[a-z0-9]+", r["alias_key"] or "")}
    display = {r["artist_id"]: r["display"] for r in conn.execute(
        "SELECT artist_id, display FROM artist_alias")}

    if args.refresh_english:
        # Repair pass: rows written before `english` existed carry only the
        # normalized key, so the pages have no presentable spelling to show.
        # Any artist with no presentable spelling yet -- including ones whose
        # latin alias came from ordinary resolution ('crowdlu'), which is a
        # lookup key and not a name a reader should ever see.
        have_english = {r["artist_id"] for r in conn.execute(
            "SELECT artist_id FROM artist_alias "
            "WHERE english IS NOT NULL AND english != ''")}
        todo = [r for r in rows if r["artist_id"] not in have_english]
        print(f"{len(rows)} artists in corpus, {len(todo)} with a latin alias "
              f"but no display spelling")
    else:
        todo = [r for r in rows if r["artist_id"] not in have]
        print(f"{len(rows)} artists in corpus, {len(todo)} without a latin alias")
    if args.limit:
        todo = todo[:args.limit]
        print(f"  (--limit {args.limit}: processing {len(todo)} of them)")

    added = skipped = failed = 0
    for r in todo:
        aid = r["artist_id"]
        name = display.get(aid, str(aid))
        try:
            info = fetch(aid)
        except Exception as exc:
            print(f"  !! {name} ({aid}): {exc}")
            failed += 1
            time.sleep(args.pause)
            continue
        alias = latin_alias(info.get("alias"))
        if not alias:
            print(f"  -- {name} ({aid}, {r['n']} songs): no latin alias")
            skipped += 1
            time.sleep(args.pause)
            continue
        key = artists.alias_key(alias)
        print(f"  ++ {name} ({aid}, {r['n']} songs) -> {alias}  [{key}]")
        if not args.dry_run:
            with conn:
                # alias_key is the normalized lookup form; `english` keeps the
                # presentable spelling so pages can show "周杰伦 (Jay Chou)".
                conn.execute(
                    "INSERT INTO artist_alias "
                    "(alias_key, artist_id, display, confidence, english) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(alias_key) DO UPDATE SET "
                    "english = excluded.english",
                    (key, aid, name, "latin-name", alias))
        added += 1
        time.sleep(args.pause)

    print(f"\nadded {added}, no-alias {skipped}, failed {failed}"
          + (" (dry run, nothing written)" if args.dry_run else ""))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
