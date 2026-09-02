#!/usr/bin/env python3
"""Populate the shared seed corpus (seed_analysis) from the deep manifest.

Writes ONLY text-free analyses, via app.seed.store, which refuses any payload
still carrying lines or grammar examples. Creates no song rows and touches no
user account: the corpus is keyed by (artist, title) and read by every user's
autofetch.

Resumable -- already-seeded keys are skipped, so it can be re-run after an
interruption. Also backfills from songs already analyzed in the app.
"""
import json
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.expanduser("~/hsk-lyrics"))
os.environ.setdefault("HSKLYRICS_DB", os.path.expanduser("~/hsk-lyrics/hsklyrics.db"))

from app import analyze, db, lyrics_fetch, seed, vault  # noqa: E402

OWNER_USER_ID = 2      # only this account's analyses are backfilled; see backfill()
# Measured: sourcing a song costs ~8.7s of sequential HTTP to Kugou/NetEase,
# the HSK analysis ~0.02s. The job is entirely network-bound, so throughput
# scales with worker count at no CPU cost -- and these are threads in one
# process, so they add no model memory either. Held at 8 out of politeness to
# the upstream services from a single IP, not because the box can't take more.
WORKERS = 8
PAUSE = 0.5            # per-worker pause between songs
STATE = os.path.expanduser("~/seed_state.json")

lock = threading.Lock()
stats = {"seeded": 0, "local": 0, "hit": 0, "miss-cached": 0, "nomatch": 0,
         "nochinese": 0, "error": 0}


def _vault_read(artist, title):
    """Lyrics already held for the owner, so this song costs no network."""
    with db.session() as conn:
        return vault.get(conn, OWNER_USER_ID, artist, title)


def _vault_write(artist, title, text, lyrics_hash):
    """Keep the text that was just fetched, under the owner's id only.

    This is what makes the NEXT dictionary change cheap. Fetching 7,825 songs
    to compute an analysis and then discarding the only input to it is paying
    two hours for something you cannot reuse.
    """
    with db.session() as conn:
        vault.put(conn, OWNER_USER_ID, artist, title, text, lyrics_hash)


def backfill():
    """Seed from songs already analyzed in the OWNER's account (playlists 1, 2).

    Scoped to one user id because that is the only account whose lyrics have
    been eyeballed, NOT because sharing text-free stats derived from a user's
    paste is off limits. It isn't, and the app already does it: analysis_cache
    has no user_id at all, so every analysis of pasted lyrics already lands in
    shared storage. The open question for widening this is quality -- a pasted
    text can be the wrong song, one verse, or a chorus repeated -- not consent.

    Re-analyses from the stored lyrics whenever they are still on the row,
    rather than copying the stored analysis across. Those two used to be the
    same thing; they stop being the same thing the moment analysis_version is
    bumped, and copying a stale analysis into the corpus under a new version
    number is a lie the rest of the pipeline has no way to detect.
    """
    n = stale = 0
    with db.session() as conn:
        rows = conn.execute(
            "SELECT artist, title, lyrics, analysis, lyrics_hash FROM songs "
            "WHERE user_id = ? AND analysis IS NOT NULL AND lyrics_hash IS NOT NULL",
            (OWNER_USER_ID,)).fetchall()
        version = analyze.hskdata.config()["analysis_version"]
        for r in rows:
            # Text the owner already uploaded: copy it into the vault so this
            # song never needs the network again. Free -- it is already here.
            if r["lyrics"]:
                vault.put(conn, OWNER_USER_ID, r["artist"] or "", r["title"],
                          r["lyrics"], r["lyrics_hash"])
            a = json.loads(r["analysis"])
            if a.get("version") != version:
                if not r["lyrics"]:
                    # No text and a stale analysis: leave it to the fetch pass,
                    # which will resolve it from upstream like any other song.
                    stale += 1
                    continue
                a = analyze.strip_text(analyze.analyze(r["lyrics"]))
            elif not a.get("ghost") and "lines" in a:
                # Analyses of user-pasted lyrics carry lines; strip before sharing.
                a = analyze.strip_text(a)
            try:
                seed.store(conn, r["artist"] or "", r["title"], a, r["lyrics_hash"])
                n += 1
            except ValueError as e:
                print(f"  skip {r['artist']} - {r['title']}: {e}")
    print(f"backfilled {n} analyses from existing songs"
          + (f" ({stale} stale with no lyrics, left for the fetch pass)"
             if stale else ""), flush=True)


def worker(q, version):
    while True:
        try:
            artist, title = q.get_nowait()
        except queue.Empty:
            return
        try:
            # One sanctioned path for corpus read + negative cache + fetch +
            # write-back. Do NOT call lyrics_fetch here; see docs/SEEDING.md.
            reason, _ghost, _h = seed.acquire(
                artist, title, version,
                text_source=_vault_read, text_sink=_vault_write,
                # This pass is about filling the vault, so a song already
                # analysed on the earlier run still needs its text.
                require_text=True)
            with lock:
                stats["seeded" if reason == "fetched" else reason] += 1
        except Exception as e:                          # noqa: BLE001
            with lock:
                stats["error"] += 1
            if stats["error"] < 15:
                print(f"  ERROR {artist} - {title}: {type(e).__name__}: {e}", flush=True)
        finally:
            q.task_done()
            time.sleep(PAUSE)


def main():
    # chart = top-100 leaderboard, indie = the reddit recommendations, 9ini =
    # the artists in the user's own playlist. Missing files are fine: a later
    # pass picks them up once their discovery finishes.
    manifests = [os.path.expanduser("~/deep_manifest.json"),
                 os.path.expanduser("~/indie_deep_manifest.json"),
                 os.path.expanduser("~/9ini_deep_manifest.json"),
                 # tools/discover_avalanche.py -> discover_deep.py avalanche.
                 # Already past that tool's lyric gate: the instrumental and
                 # non-Mandarin acts never reach this list.
                 os.path.expanduser("~/avalanche_deep_manifest.json"),
                 # tools/deepen_artists.py: the re-discovery pass for artists
                 # whose first crawl under-covered them (see its docstring for
                 # the 周杰伦 case -- 16 songs for the biggest name in Mandopop).
                 os.path.expanduser("~/deepen_manifest.json"),
                 # tools/resolve_artists.py's earlier pass; names its artists
                 # "display" rather than "artist". Left in its own format
                 # deliberately -- rewriting a file we only read is churn.
                 os.path.expanduser("~/artists_manifest.json"),
                 os.path.expanduser("~/top_manifest.json")]
    pairs, seen = [], set()
    for m in manifests:
        if not os.path.exists(m):
            print(f"(no {os.path.basename(m)} yet -- skipping)")
            continue
        d = json.load(open(m, encoding="utf-8"))
        for a in d["artists"]:
            name = a.get("artist") or a.get("display") or a.get("netease_name")
            if not name:
                print(f"  (no artist name in {os.path.basename(m)} entry -- skipped)")
                continue
            for s in a["songs"]:
                title = s["title"] if isinstance(s, dict) else s
                k = seed.key(name, title)
                if k in seen:
                    continue
                seen.add(k)
                pairs.append((name, title))
    print(f"{len(pairs)} unique (artist, title) pairs", flush=True)

    # Songs already in the corpus but in no manifest -- a manifest can be
    # deleted or superseded, and a re-analysis pass that silently dropped
    # rows it could not find a source line for would quietly shrink the
    # corpus. The keys are normalized, which is a weaker query than the
    # original display strings, so these go last and are reported separately.
    orphans = 0
    with db.session() as conn:
        for r in conn.execute("SELECT artist_key, title_key FROM seed_analysis"):
            k = (r["artist_key"], r["title_key"])
            if k in seen:
                continue
            seen.add(k)
            pairs.append((r["artist_key"], r["title_key"]))
            orphans += 1
    if orphans:
        print(f"  + {orphans} corpus rows not covered by any manifest "
              f"(retried from their normalized keys)", flush=True)

    db.init()
    backfill()

    version = analyze.hskdata.config()["analysis_version"]
    q = queue.Queue()
    for p in pairs:
        q.put(p)
    total = q.qsize()

    threads = [threading.Thread(target=worker, args=(q, version), daemon=True)
               for _ in range(WORKERS)]
    for t in threads:
        t.start()

    start = time.time()
    while any(t.is_alive() for t in threads):
        time.sleep(30)
        done = total - q.qsize()
        rate = done / max(time.time() - start, 1)
        left = (total - done) / rate / 60 if rate else 0
        with lock:
            print(f"  {done}/{total}  {dict(stats)}  ~{left:.0f} min left", flush=True)
        json.dump(stats, open(STATE, "w"))

    for t in threads:
        t.join()
    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM seed_analysis").fetchone()["c"]
    print(f"\nDONE {dict(stats)}; corpus now holds {n} analyses")


if __name__ == "__main__":
    main()
