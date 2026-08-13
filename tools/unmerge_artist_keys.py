#!/usr/bin/env python3
"""Split corpus rows keyed to two artists glued together.

Some sources join collaborators with a bare space ("Ronghao Li 陈坤"), and the
key is built after punctuation and spaces are stripped -- so the seam is gone
by the time the row is written and the corpus grows a phantom artist:
'ronghaoli陈坤', 'jaychouashinchen', 'haorweibird', 'ronghaoliameichang'.

app/artists.py now splits these when the string arrives, but that cannot repair
rows already written, because the stored key has no space left to split on.

So we split on evidence instead of a rule: a key is merged when it starts with
the key of an artist we already know AND has something left over. 'jaychou' is
a real artist and 'ashinchen' is the remainder, so 'jaychouashinchen' is a
collaboration credited to Jay Chou. Longest known prefix wins, so 'ronghaoli'
beats a shorter accidental match.

Only touches rows with NO artist_id -- a row NetEase resolved is a real artist,
including genuine dual-script names like 李大奔BENZO, which must not be split.

  ./.venv/bin/python tools/unmerge_artist_keys.py [--apply]
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db  # noqa: E402

# Two latin letters is coincidence; two Han characters is usually a whole name
# (队长, 花粥), so the floor depends on the script.
MIN_PREFIX_LATIN = 3
MIN_PREFIX_HAN = 2
MIN_REMAINDER = 2
HAN = re.compile(r"[一-鿿]")


def min_prefix(k):
    return MIN_PREFIX_HAN if HAN.search(k) else MIN_PREFIX_LATIN


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db.init()
    conn = db.connect()

    # Artists we are confident exist: they resolved to a canonical NetEase id.
    known = {}
    for r in conn.execute(
            "SELECT DISTINCT artist_key, artist_id FROM seed_analysis "
            "WHERE artist_id IS NOT NULL"):
        known[r["artist_key"]] = r["artist_id"]
    for r in conn.execute("SELECT alias_key, artist_id FROM artist_alias"):
        known.setdefault(r["alias_key"], r["artist_id"])

    suspects = conn.execute(
        "SELECT artist_key, COUNT(*) n FROM seed_analysis "
        "WHERE artist_id IS NULL GROUP BY artist_key").fetchall()

    plan = []
    for r in suspects:
        key = r["artist_key"]
        best = None
        for k in known:
            if len(k) < min_prefix(k) or k == key or not key.startswith(k):
                continue
            if len(key) - len(k) < MIN_REMAINDER:
                continue
            if best is None or len(k) > len(best):
                best = k
        if best:
            plan.append((key, best, known[best], r["n"]))

    if not plan:
        print("no merged keys found")
        return 0
    print(f"{len(suspects)} unresolved keys; {len(plan)} look merged:\n")
    for key, target, aid, n in plan:
        print(f"  {key!r:34} -> {target!r} (id {aid})   [{n} song(s), "
              f"remainder {key[len(target):]!r}]")

    if not args.apply:
        print("\ndry run — nothing written. Re-run with --apply")
        return 0

    moved = collided = 0
    with conn:
        for key, target, aid, _n in plan:
            for row in conn.execute(
                    "SELECT title_key FROM seed_analysis WHERE artist_key = ?",
                    (key,)).fetchall():
                title = row["title_key"]
                exists = conn.execute(
                    "SELECT 1 FROM seed_analysis WHERE artist_key = ? "
                    "AND title_key = ?", (target, title)).fetchone()
                if exists:
                    # The canonical artist already has this song; the merged
                    # row is the duplicate, so drop it rather than overwrite.
                    conn.execute("DELETE FROM seed_analysis WHERE artist_key = ?"
                                 " AND title_key = ?", (key, title))
                    collided += 1
                else:
                    conn.execute(
                        "UPDATE seed_analysis SET artist_key = ?, artist_id = ? "
                        "WHERE artist_key = ? AND title_key = ?",
                        (target, aid, key, title))
                    moved += 1
            conn.execute("DELETE FROM seed_miss WHERE artist_key = ?", (key,))
    total = conn.execute("SELECT COUNT(*) c FROM seed_analysis").fetchone()["c"]
    print(f"\nre-keyed {moved}, dropped {collided} duplicates; corpus now {total}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
