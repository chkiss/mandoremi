#!/usr/bin/env python3
"""Freeze the /artists article's numbers into data/leaderboard_snapshot.json.

WHY THIS EXISTS
---------------
/artists is a published article, not a dashboard. Its prose makes claims a
reader can check -- "a 32-point spread", "rappers are among the easiest",
"1 of 55 artists is 80% known at HSK 3" -- and those sentences were written
against a specific corpus. If the tables kept recomputing while the prose
stayed put, seeding more songs would silently make the article wrong, and the
first person to notice would be a reader on Reddit.

So the page renders from this snapshot. Regenerating it is a deliberate act:
run this tool, then re-read the prose and fix any sentence the new numbers
contradict. `tools/check_leaderboard_drift.py` tells you which ones those are.

Usage:  ./.venv/bin/python tools/snapshot_leaderboard.py [--out PATH]

Read-only against the database. Writes one JSON file.
"""
import argparse
import datetime
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import public                                        # noqa: E402

DB = os.environ.get("HSKLYRICS_DB", os.path.expanduser("~/hsk-lyrics/hsklyrics.db"))
OUT = os.path.join(public.DATA_DIR, "leaderboard_snapshot.json")

# Artists the article names in prose rather than in a table. Frozen here so the
# page never has to reach into live data for a link, and so the drift check can
# tell us if one of them stops resolving.
MENTION_SLUGS = [
    "davidtao", "jaychou", "masiwei", "jonyj", "tizzyt", "asen",
    "a188565", "hedgehog", "crowdlu",
    "a166010",      # 梁博 Liang Bo — the rock artist who scores high
    "ronghaoli",
    "deareloise",   # the three artists at or above 80% known at HSK 3
    "a13416",       # 野孩子 Ye Haizi — also the most approachable at HSK 1
    "explosicum",   # the second artist down at the hard end, beside 银临
    "beyond",       # rock's bottom, now that 刺猬 is out of the featured set
]

# Editorial glosses. The counts are computed; the English is written by hand,
# because no source we have glosses a chengyu in a way worth publishing. A
# chengyu that reaches the top 6 without a gloss here is reported, not guessed.
IDIOM_GLOSS = {
    "不知不觉": "unconsciously; before you know it",
    "小心翼翼": "cautiously; with great care",
    "一无所有": "to have nothing at all",
    "奋不顾身": "to press on regardless of danger",
    "天长地久": "enduring as long as the world lasts",
    "自由自在": "free and easy; carefree",
    "无可奈何": "to have no way out; helpless",
    "莫名其妙": "baffling; without rhyme or reason",
    "一心一意": "wholeheartedly",
    "理所当然": "as a matter of course; naturally",
    "人来人往": "people coming and going; a constant stream of passers-by",
}

# The single characters lyrics lean on whose two-syllable textbook partner is
# the form HSK actually teaches. Editorial: the pairing is the point, and no
# amount of counting produces it. The HSK level of the two-character form is
# verified against the real lists below, so a list update cannot leave a stale
# number here.
GAP_SINGLE = [
    ("星", "星星", "star"),
    ("身", "身体", "body"),
    ("相", "相信", "(mutual / to believe)"),
    ("影", "电影", "(shadow / movie)"),
    ("何", "任何", "(what / any)"),
]

# Emotional-core vocabulary that HSK does teach, but only in the 7-9 band.
# Percentages are computed (share of songs containing the word).
GAP_LATE = [
    ("情", "love / feeling"),
    ("寂寞", "lonely"),
    ("温柔", "tender / gentle"),
    ("思念", "to miss someone"),
    ("孤单", "lone / solitary"),
    ("吻", "kiss"),
]


def label(a):
    return public.full_name(a)


def row(a, rank):
    """Only the fields the page renders. Deliberately not the whole artist
    record -- the snapshot should be readable and diffable by a human deciding
    whether the article still holds."""
    return {
        "rank": rank,
        "slug": a["slug"],
        "name": a["name"],
        "english": a.get("english"),
        "genre": a.get("genre"),
        "songs_n": a["songs_n"],
        "easy_pct": round(a["easy_pct"], 1),
        "levels": {k: round(v, 3) for k, v in a["levels"].items()},
        "median_cov": {k: round(v, 4) for k, v in a["median_cov"].items()},
        "learn": {k: round(v, 1) for k, v in a["learn"].items()},
    }


def brief(a, rank_of):
    return {"slug": a["slug"], "label": label(a),
            "pct": round(a["easy_pct"], 1),
            "cov": round(100 * a["median_cov"]["3"]),
            "learn": round(a["learn"]["3"]),
            "rank": rank_of.get(id(a))}


def word_doc_share(words):
    """Share of songs whose vocabulary contains each word.

    One pass over the corpus. The public aggregate keeps only each song's top
    hard words, so it cannot answer this -- hence reading the analyses here.
    """
    want = set(words)
    hits = {w: 0 for w in want}
    n = 0
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        cur = conn.execute("SELECT analysis FROM seed_analysis")
        while True:
            batch = cur.fetchmany(200)
            if not batch:
                break
            for (blob,) in batch:
                try:
                    a = json.loads(blob)
                except Exception:
                    continue
                if not a.get("stats"):
                    continue
                n += 1
                for w in want & set(a.get("vocab", {})):
                    hits[w] += 1
    finally:
        conn.close()
    return {w: (100.0 * hits[w] / n if n else 0.0) for w in want}, n


def idiom_table(artists, top=6):
    """Most common chengyu across the corpus, with a song that uses each.

    Counted by SONGS containing the idiom, not occurrences: "appears in 75
    songs" is a claim about how widespread it is, and one song repeating a
    phrase in every chorus should not outrank sixty songs using it once.
    """
    songs_with, example = {}, {}
    for a in artists:
        for s in a["songs"]:
            for word, _count in s.get("idioms", []):
                songs_with[word] = songs_with.get(word, 0) + 1
                # First by the artist ordering (easiest first) is arbitrary but
                # stable; prefer a song by a well-known artist where we can.
                cur = example.get(word)
                if cur is None or a.get("popularity", 0) > cur[0]:
                    example[word] = (a.get("popularity", 0), a, s)
    ranked = sorted(songs_with.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    out, missing = [], []
    for word, n in ranked:
        if word not in IDIOM_GLOSS:
            missing.append(word)
        _pop, a, s = example[word]
        out.append({
            "word": word,
            "gloss": IDIOM_GLOSS.get(word, ""),
            "songs": n,
            "url": f"/song/{a['slug']}/{s['slug']}",
            "label": f"{label(a)} — {s['title']}",
        })
    return out, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    d = public.data(force=True)
    artists, ranked, featured = d["artists"], d["ranked"], d["featured"]
    rank_of = {id(a): i for i, a in enumerate(ranked, 1)}
    total_songs = sum(a["songs_n"] for a in artists)

    # --- the level table: what a learner at each level actually understands --
    levels = []
    for lv in public.LEVELS:
        covs = sorted(100 * a["median_cov"][lv] for a in featured)
        over = [a for a in featured if 100 * a["median_cov"][lv] >= 80]
        levels.append({
            "lv": lv,
            "label": public.LEVEL_LABELS[lv],
            "median": round(covs[len(covs) // 2]) if covs else 0,
            "over80": len(over),
            # When only a handful clear 80%, naming them is the whole point of
            # the row; a bare "3 of 58" invites the question and answers none.
            # Cap raised from 2 to 3 when HSK 3 went 1 -> 3: the article's next
            # paragraph names those artists, and the table above it had quietly
            # stopped naming anyone.
            "who": [label(a) for a in sorted(
                over, key=lambda a: -a["median_cov"][lv])] if len(over) <= 3
            else [],
        })

    # The top-RIGHT corner: comfortable *and* learnable. Sorting by
    # learnability alone answers a different question -- 周杰伦 tops that list
    # at 71% known, which is below the median and not where a learner should
    # start. He is the point of the paragraph after the table, not a row in it.
    COMFORTABLE = 74
    by_learn3 = sorted((a for a in featured
                        if 100 * a["median_cov"]["3"] >= COMFORTABLE),
                       key=lambda a: -a["learn"]["3"])
    # "Easy to listen to, thin to study from": high coverage, low learnability.
    thin = sorted((a for a in featured if 100 * a["median_cov"]["3"] >= 75),
                  key=lambda a: a["learn"]["3"])[:3]
    hsk1_best = max(featured, key=lambda a: a["median_cov"]["1"])
    hard6 = min(featured, key=lambda a: a["median_cov"]["6"])
    jay = next((a for a in artists if a["slug"] == "jaychou"), None)

    mentions, missing_mention = {}, []
    for slug in MENTION_SLUGS:
        a = d["by_slug"].get(slug)
        if a:
            mentions[slug] = label(a)
        else:
            missing_mention.append(slug)

    idioms, missing_gloss = idiom_table(artists)
    with_idiom = sum(1 for a in artists for s in a["songs"] if s.get("idioms"))

    shares, n_scanned = word_doc_share([w for w, _ in GAP_LATE])

    hsk = public.hskdata.hsk_dict()
    gap_single = []
    for ch, two, meaning in GAP_SINGLE:
        gap_single.append({
            "char": ch, "two": two, "meaning": meaning,
            "char_level": hsk.get(ch),          # expected: absent
            "two_level": hsk.get(two),
        })

    snap = {
        "generated": datetime.datetime.now(datetime.timezone.utc)
                             .replace(microsecond=0).isoformat(),
        "corpus": {"songs": total_songs, "artists": len(artists),
                   "ranked": len(ranked), "featured": len(featured)},
        "figures": {
            "easiest": [brief(a, rank_of) for a in ranked[:5]],
            "hardest": [brief(a, rank_of) for a in ranked[-5:][::-1]],
            "spread": round(ranked[0]["easy_pct"] - ranked[-1]["easy_pct"]),
            "learnable": [brief(a, rank_of) for a in by_learn3[:5]],
            "thin": [brief(a, rank_of) for a in thin],
            "jaychou": brief(jay, rank_of) if jay else None,
            "mentions": mentions,
            "levels": levels,
            "hsk1_best": brief(hsk1_best, rank_of)
            | {"cov1": round(100 * hsk1_best["median_cov"]["1"])},
            "hard6": brief(hard6, rank_of)
            | {"cov6": round(100 * hard6["median_cov"]["6"])},
            "idiom_song_pct": round(100.0 * with_idiom / total_songs, 1)
            if total_songs else 0.0,
            "idioms": idioms,
            "gap_single": gap_single,
            "gap_late": [{"word": w, "meaning": m,
                          "pct": round(shares.get(w, 0.0), 1)}
                         for w, m in GAP_LATE],
        },
        "featured": [row(a, rank_of[id(a)]) for a in featured],
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")

    print(f"wrote {args.out}")
    print(f"  {total_songs:,} songs / {len(artists)} artists / "
          f"{len(featured)} featured / scanned {n_scanned:,}")
    print(f"  easiest {snap['figures']['easiest'][0]['label']} "
          f"{snap['figures']['easiest'][0]['pct']}%  "
          f"hardest {snap['figures']['hardest'][0]['label']} "
          f"{snap['figures']['hardest'][0]['pct']}%  "
          f"spread {snap['figures']['spread']}")
    if missing_mention:
        print(f"  NOTE: the article names {', '.join(missing_mention)} but no "
              f"such artist page exists — the link would 404.")
    if missing_gloss:
        print(f"  NOTE: no gloss for {', '.join(missing_gloss)} — add it to "
              f"IDIOM_GLOSS and re-run, or the table ships a blank cell.")
    for g in gap_single:
        if g["char_level"] is not None:
            print(f"  NOTE: {g['char']} is now HSK {g['char_level']}; the "
                  f"'not on any HSK list' claim no longer holds for it.")
        if g["two_level"] is None:
            print(f"  NOTE: {g['two']} is not on the HSK lists at all.")
    print("\nRe-read the article prose against these numbers before deploying.")


if __name__ == "__main__":
    main()
