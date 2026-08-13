#!/usr/bin/env python3
"""Re-key and de-duplicate the seed corpus.

Two legacy problems, both from rows written before canonical artist ids existed:

  * the same artist stored under two keys ("李荣浩" and "ronghaoli"), because
    one row came from Han-named seeding and the other from a latin-named song
  * collaboration strings keyed whole ("jaychougaryyang"), because seed.key()
    used the raw artist string instead of the primary artist

Rows are re-keyed with the current rule, then merged: for each
(canonical artist, title) we keep one row, preferring the one that already
carries an artist_id and a Han artist_key.

  python3 tools/dedupe_corpus.py [--apply]      (default: dry run)
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import artists, db, normalize, seed  # noqa: E402

HAN_RE = re.compile(r"[一-鿿]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes")
    args = ap.parse_args()

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT rowid, artist_key, title_key, artist_id, version, lyrics_hash "
            "FROM seed_analysis").fetchall()

        # 1. what would each row's key be under the current rule?
        rekey = []
        for r in rows:
            # Same function the corpus keys with, so this can never drift.
            want = artists.alias_key(r["artist_key"])
            if want != r["artist_key"]:
                rekey.append((r["rowid"], r["artist_key"], want))
        print(f"rows whose artist_key changes under the current rule: {len(rekey)}")
        for _rid, old, new in rekey[:8]:
            print(f"   {old}  ->  {new}")

        # 2. group by canonical identity: artist_id when known, else the key
        groups = {}
        for r in rows:
            ident = r["artist_id"]
            if ident is None:
                a = artists.lookup(conn, r["artist_key"])
                ident = a[0] if a else f"key:{r['artist_key']}"
            groups.setdefault((ident, r["title_key"]), []).append(r)

        dupes = {k: v for k, v in groups.items() if len(v) > 1}
        n_drop = sum(len(v) - 1 for v in dupes.values())
        print(f"\nduplicate (artist, title) groups: {len(dupes)}; "
              f"rows that would be removed: {n_drop}")

        mismatched = [k for k, v in dupes.items()
                      if len({r["lyrics_hash"] for r in v}) > 1]
        print(f"groups whose duplicates disagree on lyrics_hash: {len(mismatched)} "
              f"(different sources for one song — keeping the canonical row)")

        if not args.apply:
            print("\ndry run — nothing written. Re-run with --apply")
            return

        def score(r):
            """Prefer a row with a canonical id, then a Han key, then longer."""
            return (r["artist_id"] is not None,
                    bool(HAN_RE.search(r["artist_key"])),
                    len(r["artist_key"]))

        removed = 0
        for (ident, _title), group in dupes.items():
            keep = max(group, key=score)
            for r in group:
                if r["rowid"] != keep["rowid"]:
                    conn.execute("DELETE FROM seed_analysis WHERE rowid = ?", (r["rowid"],))
                    removed += 1
            if isinstance(ident, int) and keep["artist_id"] is None:
                conn.execute("UPDATE seed_analysis SET artist_id = ? WHERE rowid = ?",
                             (ident, keep["rowid"]))

        # 3. apply the new artist_key rule to survivors, skipping any that
        #    would collide with an existing row
        fixed = 0
        for rid, old, new in rekey:
            r = conn.execute("SELECT title_key FROM seed_analysis WHERE rowid = ?",
                             (rid,)).fetchone()
            if not r:
                continue
            clash = conn.execute(
                "SELECT 1 FROM seed_analysis WHERE artist_key = ? AND title_key = ? "
                "AND rowid != ?", (new, r["title_key"], rid)).fetchone()
            if clash:
                conn.execute("DELETE FROM seed_analysis WHERE rowid = ?", (rid,))
                removed += 1
                continue
            conn.execute("UPDATE seed_analysis SET artist_key = ? WHERE rowid = ?",
                         (new, rid))
            fixed += 1

        total = conn.execute("SELECT COUNT(*) c FROM seed_analysis").fetchone()["c"]
        print(f"\nremoved {removed} duplicate rows, re-keyed {fixed}; "
              f"corpus now {total} analyses")


if __name__ == "__main__":
    main()
