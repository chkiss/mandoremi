# Seeding the shared analysis corpus

**Read this before running any bulk lyrics/analysis job.** It exists because
the obvious approach — loop over songs, fetch lyrics, write them to the DB —
is wrong here in three separate ways.

## What the corpus is

`seed_analysis` holds **text-free** song analyses (stats, vocab bag,
grammar/idiom counts) keyed by `(artist_id, title_key)`, with `artist_key` as a
fallback. Any user's `POST /api/songs/{id}/autofetch` reads it before hitting
the network, so one resolution serves everybody.

**Lyric text is never stored there.** That is the whole licensing argument for
the feature: what's shared is data *about* a song, never the song. A user's own
uploaded lyrics live on their song row (`songs.lyrics`, gated by `_own_song`)
and are never copied into the corpus. `seed.store()` raises if handed a payload
still carrying `lines` or grammar examples — don't defeat that check, it is the
invariant.

## The lyrics vault (`lyrics_vault`)

Separate table, separate purpose, **scoped to one `user_id`**. It holds the
lyric text that a fetch resolved, so that re-analysing the corpus is a local
CPU pass instead of thousands of network round-trips.

This exists because **analysis is a one-way function**. `strip_text()` discards
the lines, so a stored analysis cannot be recomputed — and any change to the
segmenter's dictionary invalidates every analysis in the corpus at once. The
first chengyu merge cost ~2.4 hours of re-downloading for exactly this reason,
and threw the downloads away again at the end.

The two tables are not interchangeable and must not be merged:

| | `seed_analysis` | `lyrics_vault` |
| --- | --- | --- |
| holds | counts, no text | the text |
| read by | every user | its owner only |
| purpose | serve analyses | recompute them, and QA them |

Text reaches it only through `seed.acquire(..., text_sink=...)` — a callback,
not a return value, so no caller receives lyrics by accident. Never point that
sink at anything shared.

`require_text=True` makes a bulk pass treat "already analysed" as *not* done,
because the point of the pass is the text. Without it a backfill silently skips
every song analysed on an earlier run and leaves a permanent hole. The request
path must leave it off: re-fetching text no page will read is pure cost.

## Non-obvious things that will bite you

1. **A search hit is not a match.** NetEase/Kugou return same-titled songs by
   other artists (its "七里香" is a Montagem track, not Jay Chou — NetEase has
   no licensed Jay Chou catalogue). Always verify artist AND title. Prefer
   fetching into a review manifest first, then applying, so a bad match is
   caught before it lands in the DB.
2. **Romanized stub artist pages.** "Mao Buyi" and "A-Mei Chang" exist as
   NetEase artists with *zero* songs. Matching a name exactly and taking the
   first hit silently yields no coverage. `app/artists.py` prefers the
   candidate with a real catalogue; use it rather than rolling your own search.
3. **`artist/albums?limit=5` returns the most RECENT releases**, not the
   biggest — for a major artist that's mostly 1-track singles. Rank albums by
   overlap with the artist's own hot-songs list instead.
4. **Sourcing is ~100% of the runtime.** Measured: ~8.7s to resolve lyrics,
   ~0.02s to run the HSK analysis. The job is network-bound, so concurrency is
   nearly free in CPU terms — but the real limit is politeness to the upstream
   services from one IP. 8 workers is the tested setting.
5. **Never load a second copy of the models on the server.** pkuseg + CC-CEDICT
   is ~500MB and the deploy box has ~3.7GB total with the app already running. A
   third model-loading process OOM-killed sshd once. Run analysis work inside the
   seeder process, not alongside it.

## The one function you should call

```python
from app import seed
reason, ghost, lyrics_hash = seed.acquire(artist, title)
# reason: hit | fetched | miss-cached | nomatch | nochinese
```

`seed.acquire()` applies the corpus read, the negative cache, the fetch, the
write-back and the text-free guarantee **together**. Both the `autofetch`
endpoint and `tools/seed_corpus.py` go through it, and so should anything you
write. Do not call `lyrics_fetch.resolve_text` yourself.

This is enforced, not merely requested: `tests/test_seed_chokepoint.py` fails
if any file outside `app/seed.py` calls `resolve_text`, or writes
`seed_analysis` with raw SQL. If you have a genuine reason to be an exception,
add yourself to the allow-list in that test — deliberately, in a diff someone
reviews, rather than by accident.

## The negative cache

`seed_miss` records `(artist_key, title_key)` pairs that failed to resolve.
Without it, every re-run re-attempts every failure at ~9s each — a pass that
added 215 songs spent most of 40 minutes retrying ~1,700 known misses.

- `seed.missed(conn, artist, title)` → skip it
- `seed.record_miss(...)` / `seed.clear_miss(...)`
- Entries expire after `seed.SEED_MISS_RETRY_DAYS` (30), since catalogues grow.

`tools/seed_corpus.py` and the `autofetch` endpoint both honour it. **If you
write a new seeding script, honour it too** — and record misses, or the next
person's run pays for yours.

To force a retry of everything (e.g. after improving the matcher):

```sql
DELETE FROM seed_miss;                      -- all
DELETE FROM seed_miss WHERE artist_key='…'; -- one artist
```

## Running it

```bash
# 1. discover tracklists (writes a manifest, touches no DB)
# NB: use the venv interpreter — the system python3 has no fastapi/pkuseg
cd ~/hsk-lyrics
./.venv/bin/python tools/discover_deep.py chart|indie|9ini

# 2. seed the corpus from the manifests (resumable, honours seed_miss)
./.venv/bin/python tools/seed_corpus.py

# 3. map artist name strings -> canonical ids (offline sweep, cron-able)
./.venv/bin/python tools/resolve_artists.py
```

Back up the DB first: `cp hsklyrics.db hsklyrics.db.bak-<reason>`.

Always QA afterwards with `tools/qa_corpus.py`: duplicate lyric hashes across
different artists indicate wrong matches, and thin analyses (<40 Chinese
tokens) are either genuinely short songs or stubs — check before trusting.
