#!/usr/bin/env python3
"""Compare the live corpus against the frozen /artists article.

READ THIS IF YOU ARE AN AGENT PICKING UP THE PROJECT
----------------------------------------------------
/artists is a published article with FROZEN numbers
(data/leaderboard_snapshot.json). That is deliberate: its prose makes claims a
reader can check, and a page that silently recomputed would make those claims
wrong as the corpus grew. Do NOT "fix" the page by pointing it back at live
data.

What you should do instead is run this, periodically or after any seeding job:

    ./.venv/bin/python tools/check_leaderboard_drift.py

It reports two different things, and they call for different responses:

  BROKEN  — the article is now wrong or its links 404. Fix it: regenerate with
            tools/snapshot_leaderboard.py and re-read the prose against the new
            numbers, sentence by sentence.
  NEW     — the corpus has moved enough to be interesting. This is not a bug;
            it is raw material for the next post. Note it, leave the published
            article alone unless it is also BROKEN.

Exit code 1 if anything is BROKEN, 0 otherwise, so it can run from cron.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import public                                        # noqa: E402

# How far a headline number may move before the prose around it is suspect.
# The article says "a 31-point spread" and "1 of 55 at HSK 3"; a point or two
# of wobble does not make those sentences false, five does.
PCT_TOLERANCE = 2.0
SPREAD_TOLERANCE = 2


def _live_figures(d):
    """The non-artist figures, recomputed the same way the snapshot builds them.

    Deliberately duplicates a little of snapshot_leaderboard.py rather than
    importing it: that tool is a writer, and a checker that imports its writer
    can only ever agree with it.
    """
    artists = d["artists"]
    total = sum(a["songs_n"] for a in artists)
    with_idiom = sum(1 for a in artists for s in a["songs"] if s.get("idioms"))
    featured = d["featured"]
    levels = []
    for lv in public.LEVELS:
        covs = sorted(100 * a["median_cov"][lv] for a in featured)
        levels.append({
            "lv": lv,
            "median": round(covs[len(covs) // 2]) if covs else 0,
            "over80": sum(1 for a in featured
                          if 100 * a["median_cov"][lv] >= 80),
        })
    return {
        "idiom_song_pct": (100.0 * with_idiom / total) if total else None,
        "levels": levels,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true",
                    help="print only problems")
    args = ap.parse_args()

    snap = public.snapshot()
    if not snap:
        print("BROKEN: no snapshot — run tools/snapshot_leaderboard.py")
        return 1

    d = public.data(force=True)
    live_ranked = d["ranked"]
    by_slug = d["by_slug"]
    broken, new = [], []

    # --- 1. do the article's links still go anywhere? -----------------------
    linked = {x["slug"] for k in ("easiest", "hardest", "learnable", "thin")
              for x in snap["figures"].get(k, [])}
    linked |= set(snap["figures"].get("mentions", {}))
    linked |= {a["slug"] for a in snap["featured"]}
    if snap["figures"].get("jaychou"):
        linked.add(snap["figures"]["jaychou"]["slug"])
    for slug in sorted(linked - set(by_slug)):
        broken.append(f"links to /artist/{slug}, which no longer exists")

    # --- 2. have the headline figures moved out from under the prose? -------
    live_easy = live_ranked[0]
    live_hard = live_ranked[-1]
    f = snap["figures"]
    if live_easy["slug"] != f["easiest"][0]["slug"]:
        broken.append(
            f'the article calls {f["easiest"][0]["label"]} the easiest; it is '
            f'now {public.full_name(live_easy)} '
            f'({live_easy["easy_pct"]:.1f}%)')
    if live_hard["slug"] != f["hardest"][0]["slug"]:
        broken.append(
            f'the article calls {f["hardest"][0]["label"]} the hardest; it is '
            f'now {public.full_name(live_hard)} '
            f'({live_hard["easy_pct"]:.1f}%)')
    live_spread = round(live_easy["easy_pct"] - live_hard["easy_pct"])
    if abs(live_spread - f["spread"]) > SPREAD_TOLERANCE:
        broken.append(f'the article says a {f["spread"]}-point spread; '
                      f'it is now {live_spread}')

    # Per-artist movement in the frozen tables.
    for a in snap["featured"]:
        live = by_slug.get(a["slug"])
        if not live:
            continue
        delta = live["easy_pct"] - a["easy_pct"]
        if abs(delta) > PCT_TOLERANCE:
            broken.append(f'{a["name"]}: table says {a["easy_pct"]:.1f}%, '
                          f'live is {live["easy_pct"]:.1f}% ({delta:+.1f})')

    # --- 2b. the figures that are NOT about artists -------------------------
    # This section exists because the checker once reported OK on a corpus
    # re-analysis that moved the chengyu rate by 1.5 points. Everything above
    # watches the leaderboard; the article also makes checkable claims about
    # vocabulary, and an unwatched figure is one a reader gets to find first.
    live = _live_figures(d)

    if live["idiom_song_pct"] is not None:
        delta = live["idiom_song_pct"] - f["idiom_song_pct"]
        if abs(delta) > PCT_TOLERANCE:
            broken.append(
                f'the article says {f["idiom_song_pct"]:.1f}% of songs contain '
                f'a chengyu; it is now {live["idiom_song_pct"]:.1f}%')
        # The heading claims "half of all songs". That is a stronger claim than
        # the number, and it is the one a reader quotes.
        if live["idiom_song_pct"] < 45:
            broken.append(
                f'the heading says half of all songs contain a chengyu, but '
                f'the rate is now {live["idiom_song_pct"]:.1f}%')

    # The level table drives "at HSK 1-2 nothing is comfortable" and
    # "by HSK n most artists cross 80%" -- prose that inverts on a few points.
    for frozen_row, live_row in zip(f.get("levels", []), live["levels"]):
        if frozen_row["median"] != live_row["median"]:
            d_ = live_row["median"] - frozen_row["median"]
            if abs(d_) > 1:
                broken.append(
                    f'{frozen_row["label"]}: table says the median song is '
                    f'{frozen_row["median"]}% known, live is '
                    f'{live_row["median"]}% ({d_:+d})')
        # "most artists" means most. Crossing the halfway mark at a different
        # level is exactly the change that makes that sentence false.
        n = len(d["featured"])
        was, now = frozen_row["over80"] > n / 2, live_row["over80"] > n / 2
        if was != now:
            broken.append(
                f'{frozen_row["label"]}: artists at or above 80% went '
                f'{frozen_row["over80"]}/{n} -> {live_row["over80"]}/{n}, '
                f'crossing the "most artists" line the prose leans on')

    # --- 3. what is genuinely new? (material for the next post) -------------
    frozen_slugs = {a["slug"] for a in snap["featured"]}
    grew = snap["corpus"]["songs"]
    live_songs = sum(a["songs_n"] for a in d["artists"])
    if live_songs > grew * 1.1:
        new.append(f'corpus grew {grew:,} -> {live_songs:,} songs '
                   f'(+{100.0 * (live_songs - grew) / grew:.0f}%)')
    if len(d["artists"]) > snap["corpus"]["artists"]:
        new.append(f'{len(d["artists"]) - snap["corpus"]["artists"]} new '
                   f'artists in the corpus')
    # An artist who would now break into either extreme is a finding.
    for a in live_ranked[:5]:
        if a["slug"] not in {x["slug"] for x in f["easiest"]}:
            new.append(f'{public.full_name(a)} ({a["easy_pct"]:.1f}%) now '
                       f'ranks in the five easiest')
    for a in live_ranked[-5:]:
        if a["slug"] not in {x["slug"] for x in f["hardest"]}:
            new.append(f'{public.full_name(a)} ({a["easy_pct"]:.1f}%) now '
                       f'ranks in the five hardest')
    # An artist outside the frozen table is NOT news: the table is the 55
    # best-known by chart score, so most ranked artists are meant to be absent
    # and saying so every run is noise. What matters is an artist who would now
    # displace one of them -- i.e. who has become well-known enough to feature.
    # Compare against the same rule the table was built with: the top
    # FEATURED_N by chart score. Comparing against the frozen table's LOWEST
    # popularity would flag everyone, because that table also carries the
    # extreme outliers, which are there on merit and are mostly obscure.
    would_feature = sorted(live_ranked, key=lambda a: -(a["popularity"] or 0)
                           )[:public.FEATURED_N]
    risen = [a for a in would_feature if a["slug"] not in frozen_slugs]
    if risen:
        names = ", ".join(public.full_name(a) for a in risen[:5])
        new.append(f'{len(risen)} artists would now make the featured table '
                   f'but are not in it ({names}'
                   f'{"..." if len(risen) > 5 else ""})')

    if broken:
        print("BROKEN — the published article is wrong; regenerate and re-read "
              "the prose:")
        for x in broken:
            print(f"  - {x}")
    elif not args.quiet:
        print("OK — the frozen article still matches the corpus.")
    if new:
        print("\nNEW — not a bug; material for a future post:")
        for x in new:
            print(f"  - {x}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
