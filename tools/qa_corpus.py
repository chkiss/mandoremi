#!/usr/bin/env python3
"""QA the shared seed corpus.

Checks the things that actually go wrong (see tools/SEEDING.md):
  * lyric text leaking into a table every user can read
  * confident-but-WRONG matches, whose fingerprint is one lyrics_hash shared
    by songs credited to different artists
  * stub/fragment analyses that look like data but aren't a song
  * canonical-id coverage, since string-keyed rows miss cross-script lookups
"""
import collections
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db  # noqa: E402


def main():
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT artist_key, title_key, artist_id, version, lyrics_hash, analysis "
            "FROM seed_analysis").fetchall()
        aliases = conn.execute("SELECT COUNT(*) c FROM artist_alias").fetchone()["c"]
        misses = conn.execute("SELECT COUNT(*) c FROM seed_miss").fetchone()["c"]

    print(f"corpus: {len(rows)} analyses, {aliases} alias mappings, "
          f"{misses} cached misses")
    if not rows:
        return

    with_id = sum(1 for r in rows if r["artist_id"] is not None)
    artists_n = len({r["artist_id"] for r in rows if r["artist_id"] is not None})
    print(f"canonical ids: {with_id}/{len(rows)} rows ({artists_n} distinct artists)")

    versions = collections.Counter(r["version"] for r in rows)
    print(f"analysis versions: {dict(versions)}")

    # --- text leakage: the licensing invariant ---
    leaks = []
    for r in rows:
        a = json.loads(r["analysis"])
        if "lines" in a or not a.get("ghost"):
            leaks.append(r["title_key"])
        for g in a.get("grammar", []):
            if "examples" in g or "lines" in g:
                leaks.append(r["title_key"])
    print(f"\nrows carrying lyric text or line data (must be 0): {len(leaks)}")
    if leaks:
        print("  ", leaks[:10])

    # --- wrong matches: same lyrics credited to different artists ---
    by_hash = collections.defaultdict(list)
    for r in rows:
        by_hash[r["lyrics_hash"]].append(r)
    cross = []
    for h, group in by_hash.items():
        if len(group) < 2:
            continue
        keys = {g["artist_key"] for g in group}
        if len(keys) > 1:
            cross.append((h, group))
    print(f"\nidentical lyrics across DIFFERENT artists: {len(cross)} "
          f"(covers or wrong matches — inspect)")
    for _h, group in cross[:12]:
        print("   " + " | ".join(f"{g['artist_key']}-{g['title_key']}" for g in group[:4]))

    same_artist_dupes = sum(1 for h, g in by_hash.items()
                            if len(g) > 1 and len({x["artist_key"] for x in g}) == 1)
    print(f"identical lyrics within one artist: {same_artist_dupes} "
          f"(alt titles / re-releases)")

    # --- thin analyses ---
    thin = []
    tokens = []
    for r in rows:
        a = json.loads(r["analysis"])
        n = a["stats"]["chinese_tokens"]
        tokens.append(n)
        if n < 40:
            thin.append((n, r["artist_key"], r["title_key"]))
    tokens.sort()
    print(f"\nchinese_tokens: min {tokens[0]}, median {tokens[len(tokens)//2]}, "
          f"p90 {tokens[int(len(tokens)*0.9)]}, max {tokens[-1]}")
    print(f"thin analyses (<40 tokens): {len(thin)} "
          f"({len(thin)/len(rows)*100:.1f}%)")
    for n, ak, tk in sorted(thin)[:8]:
        print(f"   {n:3d}  {ak} - {tk}")

    # --- per-artist coverage, worst first ---
    per = collections.Counter(r["artist_key"] for r in rows)
    print(f"\nartists with the fewest seeded songs:")
    for ak, n in per.most_common()[:-9:-1]:
        print(f"   {n:3d}  {ak}")


if __name__ == "__main__":
    main()
