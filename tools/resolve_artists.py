#!/usr/bin/env python3
"""Resolve artist strings to canonical ids, offline.

Walks every distinct artist string in the songs table (and the seed corpus),
resolves the ones we don't already have, and caches the mapping in
artist_alias. Deliberately a sweep rather than request-path work: importing a
100-track playlist must not block on 100 third-party lookups.

Safe to re-run and safe to cron -- already-known strings cost nothing, and an
unresolvable string is simply left alone for a future run.

  python3 tools/resolve_artists.py [--limit N] [--dry-run]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import artists, db, lyrics_fetch  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max strings to resolve")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pause", type=float, default=0.5)
    args = ap.parse_args()

    db.init()
    with db.connect() as conn:
        rows = conn.execute("SELECT DISTINCT artist FROM songs "
                            "WHERE artist IS NOT NULL AND artist != ''").fetchall()
        todo, seen = [], set()
        for r in rows:
            name = artists.primary(r["artist"])
            k = artists.alias_key(name)
            if not k or k in seen:
                continue
            seen.add(k)
            if artists.lookup(conn, name) is None:
                todo.append(name)

        # ...and the seed corpus, which the songs table does not cover: seeding
        # creates no song rows, so an artist reached only by a discovery run
        # (every Concrete Avalanche act) was invisible here and its corpus rows
        # kept a NULL artist_id forever. artist_key is already normalized, so it
        # doubles as the search string.
        for r in conn.execute("SELECT DISTINCT artist_key FROM seed_analysis "
                              "WHERE artist_id IS NULL AND artist_key != ''"):
            k = r["artist_key"]
            if not k or k in seen:
                continue
            seen.add(k)
            if artists.lookup(conn, k) is None:
                todo.append(k)

    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(seen)} distinct artists, {len(todo)} unresolved")

    ok = miss = 0
    for name in todo:
        aliases = lyrics_fetch.ARTIST_ALIASES.get(name.lower(), [])
        try:
            found = artists.search(name, aliases)
        except Exception as e:                          # noqa: BLE001
            print(f"  {name}: ERROR {type(e).__name__}: {e}")
            time.sleep(args.pause)
            continue
        if not found:
            miss += 1
            print(f"  {name}: no confident match")
        else:
            aid, display, conf = found
            ok += 1
            print(f"  {name} -> {display} ({aid}, {conf})")
            if not args.dry_run:
                with db.connect() as conn:
                    artists.remember(conn, name, aid, display, conf)
        time.sleep(args.pause)

    print(f"\nresolved {ok}, unresolved {miss}"
          + (" (dry run, nothing written)" if args.dry_run else ""))

    if not args.dry_run:
        # Backfill canonical ids onto corpus rows seeded before their artist
        # had one, so lookups stop depending on the string key.
        with db.connect() as conn:
            n = 0
            for row in conn.execute(
                    "SELECT DISTINCT artist_key FROM seed_analysis WHERE artist_id IS NULL"):
                hit = conn.execute(
                    "SELECT artist_id FROM artist_alias WHERE alias_key = ?",
                    (row["artist_key"],)).fetchone()
                if hit:
                    n += conn.execute(
                        "UPDATE seed_analysis SET artist_id = ? "
                        "WHERE artist_key = ? AND artist_id IS NULL",
                        (hit["artist_id"], row["artist_key"])).rowcount
            print(f"backfilled artist_id on {n} corpus rows")


if __name__ == "__main__":
    main()
