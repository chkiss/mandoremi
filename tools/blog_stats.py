#!/usr/bin/env python3
"""Corpus-wide statistics for the launch blog posts.

Read-only. Answers the questions a post would make a claim about, so the claim
is checked before it is written rather than after someone on Reddit checks it.

Usage:  ./.venv/bin/python tools/blog_stats.py
"""
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict

DB = os.environ.get("HSKLYRICS_DB", os.path.expanduser("~/hsk-lyrics/hsklyrics.db"))

# app/hskdata.py level codes. 1-6 are HSK bands, 7 is the HSK 7-9 band, and
# 8/9 are sentinels, NOT levels: 8 = Chinese but in no HSK band, 9 = not
# Chinese at all (English words, apostrophe fragments). `chinese_tokens`
# counts 1-8 and excludes 9, so every share here uses that denominator.
LEVELS = [str(i) for i in range(1, 8)]      # HSK 1-6 + the 7-9 band
BEYOND = "8"                                 # Chinese, beyond HSK
NONCHINESE = "9"


def load():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT artist_key, title_key, artist_id, analysis FROM seed_analysis"
    ).fetchall()
    names = {r[0]: r[1] for r in c.execute(
        "SELECT artist_id, display FROM artist_alias").fetchall()}
    out = []
    for ak, tk, aid, blob in rows:
        try:
            a = json.loads(blob)
        except Exception:
            continue
        if not a.get("stats"):
            continue
        out.append((names.get(aid, ak), tk, a))
    return out


def pct(x, n):
    return 100.0 * x / n if n else 0.0


def main():
    songs = load()
    print(f"corpus: {len(songs)} analyses, "
          f"{len({s[0] for s in songs})} artists\n")

    # --- Q1: what share of an average song is HSK 1-3 / by level -------------
    tot = Counter()
    nonzh = 0
    n = 0
    for _, _, a in songs:
        cb = a["stats"]["counts_by_level"]
        for lv in LEVELS + [BEYOND]:
            tot[lv] += cb.get(lv, 0)
        nonzh += cb.get(NONCHINESE, 0)
        n += a["stats"]["chinese_tokens"]
    print("=== token share by HSK level (whole corpus) ===")
    run = 0
    for lv in LEVELS:
        run += tot[lv]
        label = "HSK 7-9" if lv == "7" else f"HSK {lv}  "
        print(f"  {label}: {pct(tot[lv], n):5.1f}%   cumulative {pct(run, n):5.1f}%")
    print(f"  beyond HSK: {pct(tot[BEYOND], n):5.1f}%   (Chinese, in no HSK band)")
    print(f"  [excluded: {nonzh} non-Chinese tokens, "
          f"{pct(nonzh, n + nonzh):.1f}% of all text]")

    # --- Q2: coverage curve per song, per level -----------------------------
    print("\n=== coverage of an average song, by the level you have ===")
    for lv in ["1", "2", "3", "4", "5", "6", "7"]:
        vals = [a["stats"]["per_level"].get(lv, {}).get("coverage", 0)
                for _, _, a in songs]
        vals = [v for v in vals if v]
        if not vals:
            continue
        vals.sort()
        med = vals[len(vals) // 2]
        share90 = pct(sum(1 for v in vals if v >= 0.90), len(vals))
        share80 = pct(sum(1 for v in vals if v >= 0.80), len(vals))
        print(f"  HSK {lv}: median coverage {med*100:5.1f}%  |  "
              f"{share80:5.1f}% of songs >=80%  |  {share90:5.1f}% >=90%")

    # --- Q3: artist difficulty leaderboard ----------------------------------
    print("\n=== artist leaderboard (>=15 songs): share of tokens HSK 1-3 ===")
    by_artist = defaultdict(lambda: [Counter(), 0, 0])  # levels, total, songs
    for art, _, a in songs:
        cb = a["stats"]["counts_by_level"]
        rec = by_artist[art]
        for lv in LEVELS:
            rec[0][lv] += cb.get(lv, 0)
        rec[1] += a["stats"]["chinese_tokens"]
        rec[2] += 1
    board = []
    for art, (lvls, total, cnt) in by_artist.items():
        if cnt < 15 or not total:
            continue
        easy = sum(lvls[lv] for lv in ("1", "2", "3"))
        board.append((pct(easy, total), art, cnt, total))
    board.sort(reverse=True)
    for label, rows in (("EASIEST", board[:12]), ("HARDEST", board[-12:])):
        print(f"  -- {label} --")
        for share, art, cnt, total in rows:
            print(f"   {share:5.1f}%  {art:<24} {cnt:4d} songs, {total:6d} tokens")

    # --- Q4: Zipf -- how many words to read most of Mandopop ----------------
    print("\n=== how many distinct words cover the corpus ===")
    freq = Counter()
    lvl_of = {}
    for _, _, a in songs:
        for w, d in a["vocab"].items():
            if str(d.get("lvl")) == NONCHINESE:
                continue          # English hooks are not Chinese vocabulary
            freq[w] += d["count"]
            lvl_of[w] = d.get("lvl", 0)
    total_tok = sum(freq.values())
    run = 0
    marks = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
    mi = 0
    for i, (w, c) in enumerate(freq.most_common(), 1):
        run += c
        while mi < len(marks) and run / total_tok >= marks[mi]:
            print(f"  top {i:6d} words  ->  {marks[mi]*100:4.0f}% of all tokens")
            mi += 1
    print(f"  corpus vocabulary: {len(freq)} distinct words, {total_tok} tokens")

    # --- Q5: high-frequency words HSK doesn't teach -------------------------
    # Level 9 is non-Chinese, so it is excluded — otherwise the list is just
    # "I", "you", "the" and apostrophe fragments from English-language hooks.
    print("\n=== most common words at HSK 7-9, and beyond HSK entirely ===")
    seen_in = Counter()
    for _, _, a in songs:
        for w in a["vocab"]:
            seen_in[w] += 1
    for code, label in ((7, "HSK 7-9 band"), (8, "beyond HSK")):
        hard = sorted(((c, w) for w, c in freq.items()
                       if lvl_of.get(w) == code), reverse=True)
        print(f"  -- {label} --")
        for c, w in hard[:20]:
            print(f"   {w:<10} {c:6d}x  in {seen_in[w]:5d} songs "
                  f"({pct(seen_in[w], len(songs)):4.1f}%)")

    # --- Q6: grammar patterns ------------------------------------------------
    print("\n=== grammar patterns: how much of the corpus contains each ===")
    gs = Counter()
    gc = Counter()
    gname = {}
    glevel = {}
    for _, _, a in songs:
        for g in a.get("grammar", []):
            gs[g["key"]] += 1
            gc[g["key"]] += g["count"]
            gname[g["key"]] = g["name"]
            glevel[g["key"]] = g["level"]
    for k, s in gs.most_common():
        print(f"  {pct(s, len(songs)):5.1f}% of songs  {gc[k]:6d}x  "
              f"HSK{glevel[k]}  {gname[k]}")

    # --- Q7: chengyu ---------------------------------------------------------
    print("\n=== idioms (chengyu) ===")
    idi = Counter()
    with_idiom = 0
    for _, _, a in songs:
        if a.get("idioms"):
            with_idiom += 1
        for d in a.get("idioms", []):
            idi[d["word"]] += d["count"]
    print(f"  {pct(with_idiom, len(songs)):.1f}% of songs contain at least one "
          f"({with_idiom}/{len(songs)}); {len(idi)} distinct")
    for w, c in idi.most_common(20):
        print(f"   {w:<10} {c:4d}x")

    # --- Q8: best songs to learn at each level -------------------------------
    print("\n=== highest learning value per level (>=120 tokens) ===")
    for lv in ["1", "2", "3", "4", "5"]:
        cand = []
        for art, title, a in songs:
            if a["stats"]["chinese_tokens"] < 120:
                continue
            p = a["stats"]["per_level"].get(lv)
            if not p:
                continue
            cand.append((p["learning_value"], p["coverage"],
                         p["unique_unknown"], art, title))
        cand.sort(reverse=True)
        print(f"  -- HSK {lv} --")
        for lvv, cov, unk, art, title in cand[:8]:
            print(f"   LV {lvv:5.1f}  cov {cov*100:5.1f}%  {unk:3d} new  "
                  f"{art} — {title}")

    # --- Q9: extremes --------------------------------------------------------
    print("\n=== easiest / hardest individual songs (>=150 tokens) ===")
    ss = []
    for art, title, a in songs:
        st = a["stats"]
        if st["chinese_tokens"] < 150:
            continue
        cb = st["counts_by_level"]
        easy = sum(cb.get(lv, 0) for lv in ("1", "2", "3"))
        ss.append((pct(easy, st["chinese_tokens"]), art, title,
                   st["unique_vocab"], st["chinese_tokens"]))
    ss.sort(reverse=True)
    for label, rows in (("EASIEST", ss[:10]), ("HARDEST", ss[-10:])):
        print(f"  -- {label} --")
        for share, art, title, uv, tk in rows:
            print(f"   {share:5.1f}% HSK1-3  {uv:3d} unique / {tk:4d} tok  "
                  f"{art} — {title}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
