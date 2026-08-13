#!/usr/bin/env python3
"""Record how popular each artist actually is, independent of our coverage.

The leaderboard originally featured artists by how many songs we had seeded,
on the assumption that seeding depth tracked popularity. It does not, and the
deepening run proved it: seeding depth tracks *lyric availability*. 周杰伦 has
54.9M chart score and 18 seeded songs, because his catalogue is not licensed on
the sources we can read; deca joins has 42 seeded songs and does not appear in
the 华语 top-100 at all. Featuring by corpus depth therefore ranked a niche
indie band above the most recognisable artist in Mandopop.

So popularity comes from the NetEase 华语 leaderboard score instead, with the
artist's catalogue size as a weak fallback for anyone off the chart. Stored on
artist_alias so the public pages can order by it without a network call.

  ./.venv/bin/python tools/backfill_popularity.py [--dry-run]
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db  # noqa: E402

CHART = os.path.expanduser("~/top_artists.json")
API = "http://music.163.com/api/artist/{}"
HEADERS = {"Cookie": "appver=2.0.2",
           "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}


def chart_scores():
    if not os.path.exists(CHART):
        print(f"(no {CHART}; every artist falls back to catalogue size)")
        return {}
    d = json.load(open(CHART, encoding="utf-8"))
    return {a["id"]: a.get("score") or 0 for a in d["list"]["artists"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pause", type=float, default=0.5)
    args = ap.parse_args()

    db.init()
    conn = db.connect()
    scores = chart_scores()
    ids = [r["artist_id"] for r in conn.execute(
        "SELECT DISTINCT artist_id FROM seed_analysis WHERE artist_id IS NOT NULL")]
    names = {r["artist_id"]: r["display"] for r in conn.execute(
        "SELECT artist_id, display FROM artist_alias")}
    print(f"{len(ids)} artists; {len(scores)} on the 华语 chart")

    charted = fetched = 0
    for aid in ids:
        if aid in scores:
            pop, src = scores[aid], "chart"
            charted += 1
        else:
            # Off-chart: catalogue size is a poor proxy, but it beats corpus
            # depth and keeps the ordering stable. Scaled far below any real
            # chart score so a charting artist always outranks a non-charting.
            try:
                info = json.loads(urllib.request.urlopen(
                    urllib.request.Request(API.format(aid), headers=HEADERS),
                    timeout=15).read()).get("artist") or {}
                pop = min(info.get("musicSize") or 0, 9999)
                src = "catalogue"
                fetched += 1
            except Exception as exc:                    # noqa: BLE001
                print(f"  !! {names.get(aid, aid)}: {exc}")
                continue
            time.sleep(args.pause)
        if not args.dry_run:
            with conn:
                conn.execute("UPDATE artist_alias SET popularity = ? "
                             "WHERE artist_id = ?", (pop, aid))
        print(f"  {names.get(aid, aid)[:22]:24s} {pop:>10,}  ({src})")

    print(f"\n{charted} from the chart, {fetched} by catalogue size"
          + (" (dry run, nothing written)" if args.dry_run else ""))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
