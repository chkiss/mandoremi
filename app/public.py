"""Public, login-free difficulty pages built from the seed corpus.

These are the only pages that expose the corpus, and they are safe to expose
because a seed analysis holds no lyric text at all -- only counts (see
analyze.strip_text). Nothing here reads the `songs` table, so no user's
uploaded lyrics can leak into a public page even by accident.

Server-rendered rather than fetched by the SPA: the entire point is to be
crawlable, and a JS-built table is not reliably indexed. The pages work with
scripting disabled.

The aggregate is cached in-process with a TTL. Building it parses ~5k JSON
blobs, so it is built lazily under a lock, and rows are streamed rather than
fetched as a list -- the service runs under MemoryMax=1200M next to pkuseg,
and holding 5k parsed analyses at once is exactly the kind of spike that gets
the app OOM-killed.
"""
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from urllib.parse import quote

from . import artists as artistlib
from . import chengyu
from . import db
from . import dictionary
from . import hskdata

# 1-6 are HSK bands and 7 is the HSK 7-9 band. 8 and 9 are NOT levels: 8 is
# Chinese outside HSK, 9 is not Chinese (English hooks). chinese_tokens counts
# 1-8 and excludes 9. Summing 1-9 as levels yields >100% totals.
LEVELS = ("1", "2", "3", "4", "5", "6", "7")
BEYOND = "8"
NONCHINESE = "9"
LEVEL_LABELS = {"1": "HSK 1", "2": "HSK 2", "3": "HSK 3", "4": "HSK 4",
                "5": "HSK 5", "6": "HSK 6", "7": "HSK 7-9", "8": "Beyond HSK"}

# Curated English names that NetEase never supplies (e.g. 银临, who has no
# Western stage name). Keyed by the Chinese display name; overrides whatever
# the NetEase backfill wrote, so names can be corrected without a re-crawl.
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
STATIC_IMG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "static", "img")
_EN_OVERRIDE = None
_GENRE_OVERRIDE = None


def _en_overrides():
    global _EN_OVERRIDE
    if _EN_OVERRIDE is None:
        path = os.path.join(DATA_DIR, "artist_names.json")
        try:
            with open(path, encoding="utf-8") as f:
                _EN_OVERRIDE = json.load(f)
        except (OSError, ValueError):
            _EN_OVERRIDE = {}
    return _EN_OVERRIDE


def _genre_overrides():
    """Curated genre tags (keyed by Chinese display name) for colouring the
    scatter. No genre data exists in any upstream source, so this is a small
    hand-maintained map — edit data/artist_genres.json to correct any tag."""
    global _GENRE_OVERRIDE
    if _GENRE_OVERRIDE is None:
        path = os.path.join(DATA_DIR, "artist_genres.json")
        try:
            with open(path, encoding="utf-8") as f:
                _GENRE_OVERRIDE = json.load(f)
        except (OSError, ValueError):
            _GENRE_OVERRIDE = {}
    return _GENRE_OVERRIDE


_HAN = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")


# Compound (two-character) surnames, so 欧阳靖 romanises as "Ouyang Jing" and
# not "Ou Yangjing".
_COMPOUND_SURNAMES = {
    "欧阳", "司马", "上官", "夏侯", "诸葛", "东方", "皇甫", "尉迟", "公孙",
    "澹台", "宗政", "濮阳", "淳于", "单于", "太叔", "申屠", "公羊", "长孙",
    "慕容", "司徒", "司空", "南宫", "西门", "独孤", "轩辕", "令狐", "钟离",
}


# A name ending in one of these is a group, however short. Length alone would
# read 好乐团 as a three-character personal name and romanise it "Hao Yuetuan";
# it is GoodBand.
_BAND_SUFFIXES = ("乐团", "乐队", "樂團", "樂隊", "组合", "組合", "合唱团",
                  "合唱團", "兄弟", "姐妹")


def _en_pinyin_fallback(display_names):
    """Last-resort romanisation, for personal names only.

    This runs only when Wikidata (tools/enrich_names_wikidata.py) and the
    curated file both came up empty, and it is deliberately narrow:

    * PERSONAL NAMES ONLY (2-3 Han characters). A band name transliterated
      syllable by syllable is noise, not a name: 万能青年旅店 is Omnipotent
      Youth Society, and "Wan Neng Qing Nian Lü Dian" tells an English reader
      strictly less than the Chinese does. For those we return nothing and the
      artist simply shows their Chinese name.
    * SURNAME THEN GIVEN NAME, JOINED — "Huang Xiaoyun", not "Huang Xiao Yun",
      which is how Chinese names are written in English everywhere.
    * v_to_u=True, or 旅 comes out "Lv" and 女 comes out "Nv". pypinyin's
      default ASCII fallback for ü is the letter v, which is a keyboard input
      convention and not a spelling anyone uses.

    Names already mixing in Latin (艾志恒Asen) or Latin-only are left alone.
    Requires pypinyin; without it we return {} and nothing breaks.
    """
    try:
        from pypinyin import pinyin, Style
    except Exception:
        return {}
    out = {}
    for nm in display_names:
        if not _HAN.search(nm) or _LATIN.search(nm):
            continue
        if not 2 <= len(nm) <= 3:
            continue                      # too long to be a personal name
        if nm.endswith(_BAND_SUFFIXES):
            continue                      # 好乐团 is GoodBand, not "Hao Yuetuan"
        surname_len = 2 if nm[:2] in _COMPOUND_SURNAMES else 1
        syl = [x[0] for x in pinyin(nm, style=Style.NORMAL, v_to_u=True)]
        if len(syl) != len(nm):
            continue                      # unexpected segmentation; skip
        surname = "".join(syl[:surname_len]).capitalize()
        given = "".join(syl[surname_len:]).capitalize()
        out[nm] = f"{surname} {given}".strip()
    return out



# An artist needs enough analyzed material for a percentage to mean anything.
# Below this the ranking is noise, and a leaderboard that ranks a 3-song artist
# against a 70-song artist is the first thing a reader will (rightly) attack.
MIN_SONGS = 15
MIN_TOKENS = 2000

# A 155-row table of mostly-unrecognised names reads as noise and buries the
# finding. The headline table is capped at the best-known artists (by chart
# score, see popularity below) plus the extreme outliers at each end, because
# the outliers ARE the story. Everything else stays reachable as a link, so no
# page is orphaned and the long tail still gets crawled.
FEATURED_N = int(os.environ.get("MANDOREMI_FEATURED_N", "50"))
OUTLIERS_N = int(os.environ.get("MANDOREMI_OUTLIERS_N", "5"))

TTL = int(os.environ.get("MANDOREMI_PUBLIC_TTL", "3600"))

_lock = threading.Lock()
_cache = {"built": 0.0, "data": None}


# --------------------------------------------------------------------------
# building

def _slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s


def _artist_slugs(conn):
    """artist_id -> url slug, preferring a latin alias over the Chinese name.

    A Chinese slug is perfectly valid in a URL and indexes fine, but a latin
    one survives being pasted into Reddit/Discord without percent-encoding
    turning it into line noise, which is the whole point of a shareable page.
    """
    best, display, keys, english, popularity = {}, {}, {}, {}, {}
    for r in conn.execute("SELECT alias_key, artist_id, display, confidence, "
                          "english, popularity FROM artist_alias"):
        display[r["artist_id"]] = r["display"]
        keys.setdefault(r["artist_id"], set()).add(r["alias_key"])
        # The `english` column holds the presentable spelling ("David Tao");
        # alias_key is the normalized lookup form ('davidtao') and must never
        # be shown to a reader.
        if r["english"]:
            english[r["artist_id"]] = r["english"]
        if r["popularity"]:
            popularity[r["artist_id"]] = r["popularity"]
        cand = _slugify(r["alias_key"])
        if not cand:
            continue
        cur = best.get(r["artist_id"])
        # longest latin alias wins: 'jaychou' over a truncated variant
        if cur is None or len(cand) > len(cur):
            best[r["artist_id"]] = cand
    # Overlay curated names that NetEase doesn't provide (e.g. 银临 -> Yin Lin).
    name_to_id = {disp: aid for aid, disp in display.items()}
    for nm, en in _en_overrides().items():
        aid = name_to_id.get(nm)
        if aid:
            english[aid] = en
    # Last resort: romanise any still-missing pure-Han name with pypinyin.
    for nm, en in _en_pinyin_fallback(display.values()).items():
        aid = name_to_id.get(nm)
        if aid and not english.get(aid):
            english[aid] = en
    return best, display, keys, english, popularity


def full_name(a):
    """周杰伦 (Jay Chou) when we have both, otherwise just what we have.

    Preference order for the parenthetical:
      1. a real English name we have — e.g. 李大奔BENZO -> "李大奔BENZO
         (Li Daben)". Sourced, in order, from data/artist_names.json (curated,
         always wins), NetEase, then the pinyin fallback below. The curated
         file is filled by tools/enrich_names_wikidata.py, which matches on a
         Wikidata *label* and requires the entity to be a musician or group;
         looser matching pulls in unrelated people (searching 小老虎 returns an
         Indian actor whose Chinese nickname it is).
      2. a Latin fragment already baked into the display name by a source
         (NetEase sometimes stores "理想混蛋Bestards" as one dual-script run)
         so both are findable — split it to read "理想混蛋 (Bestards)".
      3. nothing (Beyond, TizzyT).
    """
    name, eng = a["name"], a.get("english")
    if eng and eng.strip() and eng.strip().lower() != name.strip().lower() \
            and eng.lower() not in name.lower():
        return f"{name} ({eng})"
    # NetEase stores some acts as one dual-script run ("理想混蛋Bestards")
    # purely so both names are findable. Split those so they read like every
    # other artist rather than as a run-on.
    dual = artistlib.split_dual_script(name)
    if dual:
        return f"{dual[0]} ({dual[1]})"
    return name


def _blank():
    return {"levels": Counter(), "tokens": 0, "songs": [],
            "cov": {lv: [] for lv in LEVELS}, "beyond": Counter(),
            "idioms": 0, "grammar": Counter(), "words": Counter(),
            "docs": Counter()}


def _build():
    conn = db.connect()
    try:
        slugs, displays, alias_keys, english, popularity = _artist_slugs(conn)
        groups = {}
        # Corpus-wide totals, needed to say what a given artist uses *unusually*
        # often. Counters over ~19k distinct words, so a few MB, not a spike.
        corpus_words, corpus_docs = Counter(), Counter()
        # Songs containing each chengyu. A chengyu dictionary can tell you what
        # an idiom means; only a corpus can tell you whether songwriters
        # actually reach for it, which is the part a learner is choosing on.
        corpus_idioms = Counter()
        n_songs_total = 0
        cur = conn.execute(
            "SELECT artist_key, title_key, artist_id, analysis FROM seed_analysis")
        while True:
            batch = cur.fetchmany(200)
            if not batch:
                break
            for row in batch:
                try:
                    a = json.loads(row["analysis"])
                except Exception:
                    continue
                st = a.get("stats")
                if not st or not st.get("chinese_tokens"):
                    continue
                key = row["artist_id"] or f"k:{row['artist_key']}"
                g = groups.get(key)
                if g is None:
                    g = groups[key] = _blank()
                    g["name"] = displays.get(row["artist_id"], row["artist_key"])
                    g["english"] = english.get(row["artist_id"])
                    g["popularity"] = popularity.get(row["artist_id"], 0)
                    g["genre"] = _genre_overrides().get(g["name"])
                    # A Chinese-only name slugifies to nothing, and an artist
                    # with no canonical id has a key like "k:伍佰" -- neither
                    # is a usable URL, so fall back to a stable ascii handle.
                    g["slug"] = (slugs.get(row["artist_id"])
                                 or _slugify(row["artist_key"])
                                 or (f"a{row['artist_id']}" if row["artist_id"]
                                     else "a" + hashlib.sha1(
                                         row["artist_key"].encode()
                                     ).hexdigest()[:8]))
                cb = st["counts_by_level"]
                zh = st["chinese_tokens"]
                for lv in LEVELS + (BEYOND,):
                    g["levels"][lv] += cb.get(lv, 0)
                g["tokens"] += zh
                easy = sum(cb.get(lv, 0) for lv in ("1", "2", "3"))
                per = st.get("per_level", {})
                for lv in LEVELS:
                    c = per.get(lv, {}).get("coverage")
                    if c:
                        g["cov"][lv].append(c)
                for w, d in a.get("vocab", {}).items():
                    if str(d.get("lvl")) == NONCHINESE:
                        continue          # English hooks are not vocabulary
                    if str(d.get("lvl")) in (BEYOND, "7"):
                        g["beyond"][w] += d["count"]
                    g["words"][w] += d["count"]
                    g["docs"][w] += 1     # songs containing it, for distinctiveness
                    corpus_words[w] += d["count"]
                    corpus_docs[w] += 1
                if a.get("idioms"):
                    g["idioms"] += 1
                    for x in a["idioms"]:
                        corpus_idioms[x["word"]] += 1
                for gr in a.get("grammar", []):
                    g["grammar"][gr["name"]] += 1
                g["songs"].append({
                    "title": row["title_key"],
                    "levels": {lv: cb.get(lv, 0) for lv in LEVELS + (BEYOND,)},
                    "grammar": [(x["name"], x["count"])
                                for x in a.get("grammar", [])][:8],
                    # Uncapped: the corpus averages about one chengyu per song
                    # and the whole set is under 6k rows, so a cap here would
                    # only serve to make /chengyu/<word> quietly incomplete.
                    "idioms": [(x["word"], x["count"])
                               for x in a.get("idioms", [])],
                    "hard": sorted(
                        ((d["count"], w) for w, d in a.get("vocab", {}).items()
                         if str(d.get("lvl")) in ("7", BEYOND)),
                        reverse=True)[:14],
                    "cov": {lv: per.get(lv, {}).get("coverage", 0.0)
                            for lv in LEVELS},
                    "unk": {lv: per.get(lv, {}).get("unique_unknown", 0)
                            for lv in LEVELS},
                    "rep": {lv: per.get(lv, {}).get("avg_reps_unknown", 0.0)
                            for lv in LEVELS},
                    "tokens": zh,
                    "unique": st.get("unique_vocab", 0),
                    "easy": 100.0 * easy / zh,
                    "cov3": per.get("3", {}).get("coverage", 0.0),
                    "cov5": per.get("5", {}).get("coverage", 0.0),
                    "lv3": per.get("3", {}).get("learning_value", 0.0),
                })
                n_songs_total += 1
                del a
        cur.close()
    finally:
        conn.close()

    artists = []
    for key, g in groups.items():
        if not g["tokens"]:
            continue
        n = len(g["songs"])
        # The headline is the MEDIAN song, not a token-weighted average of all
        # of them. Faye Wong's corpus includes a 4,761-token recitation of the
        # Diamond Sutra -- 31% of her total words in one track -- which under
        # token weighting quietly redefines "a Faye Wong song". The median
        # answers the question a learner is actually asking: what is a typical
        # song by this artist like?
        artists.append({
            "key": key,
            "name": g["name"],
            "english": g.get("english"),
            "genre": g.get("genre"),
            "popularity": g.get("popularity", 0),
            "slug": g["slug"],
            "songs_n": n,
            "tokens": g["tokens"],
            "easy_pct": _median([s["easy"] for s in g["songs"]]),
            "easy_weighted": 100.0 * sum(g["levels"][lv] for lv in ("1", "2", "3"))
                             / g["tokens"],
            "levels": {lv: 100.0 * g["levels"][lv] / g["tokens"]
                       for lv in LEVELS + (BEYOND,)},
            "median_cov": {lv: _median(g["cov"][lv]) for lv in LEVELS},
            # Learnability of the UNKNOWN part: few unknown words + those words
            # repeating. This is the non-coverage half of the app's learning
            # value, computed per level so the scatter can pair it with the
            # coverage Y-axis. Weights come from data/config.json.
            "learn": _learnability(g["songs"]),
            "beyond": g["beyond"].most_common(12),
            "distinctive": _distinctive(g, corpus_words, corpus_docs,
                                        n_songs_total),
            "idiom_pct": 100.0 * g["idioms"] / n if n else 0.0,
            "grammar": [(k, 100.0 * v / n) for k, v in g["grammar"].most_common(8)],
            "songs": sorted(g["songs"], key=lambda s: -s["easy"]),
            "ranked": n >= MIN_SONGS and g["tokens"] >= MIN_TOKENS,
        })

    # Song slugs, unique within an artist. A title is often pure Han, which
    # slugifies to nothing, so fall back to a stable hash of the title key.
    for a in artists:
        used = set()
        for s in a["songs"]:
            base = (_slugify(s["title"])
                    or "s" + hashlib.sha1(s["title"].encode()).hexdigest()[:8])
            slug, i = base, 2
            while slug in used:
                slug, i = f"{base}-{i}", i + 1
            used.add(slug)
            s["slug"] = slug

    artists.sort(key=lambda a: -a["easy_pct"])
    by_slug, seen = {}, set()
    for a in artists:
        slug = a["slug"]
        i = 2
        while slug in seen:
            slug = f"{a['slug']}-{i}"
            i += 1
        seen.add(slug)
        a["slug"] = slug
        by_slug[slug] = a
    ranked = [a for a in artists if a["ranked"]]
    # Featured = the best-known artists + the extremes at both ends.
    #
    # NOT the deepest corpus, which is what this used to do. Seeding depth
    # tracks lyric availability, not fame: 周杰伦 has 18 seeded songs because
    # his catalogue is unlicensed on the sources we can read, while deca joins
    # has 42 and is not on the 华语 chart at all. Ordering by corpus depth put
    # the indie band on the front page and the biggest name in Mandopop in a
    # footnote. popularity comes from the chart (tools/backfill_popularity.py).
    known = sorted(ranked, key=lambda a: -(a["popularity"] or 0))[:FEATURED_N]
    keep = {id(a) for a in known}
    if OUTLIERS_N > 0:
        # Guard the zero case: ranked[-0:] is the whole list, not an empty one,
        # which would silently feature every artist.
        keep |= {id(a) for a in ranked[:OUTLIERS_N]}
        keep |= {id(a) for a in ranked[-OUTLIERS_N:]}
    featured = [a for a in ranked if id(a) in keep]     # already easy-sorted
    for a in featured:
        a["featured"] = True
    # Every string that should resolve to a page: each recorded alias, plus the
    # canonical display name (which is not always itself an alias row).
    slug_by_key = {}
    by_id = {a["key"]: a for a in artists}
    for aid, aliases in alias_keys.items():
        a = by_id.get(aid)
        if not a:
            continue
        for k in aliases | {artistlib.alias_key(a["name"])}:
            if k:
                slug_by_key[k] = a["slug"]
    for a in artists:
        k = artistlib.alias_key(a["name"])
        if k:
            slug_by_key.setdefault(k, a["slug"])

    return {"artists": artists, "by_slug": by_slug, "ranked": ranked,
            "featured": featured, "slug_by_key": slug_by_key,
            "rest": [a for a in ranked if id(a) not in keep],
            "idiom_docs": corpus_idioms, "n_songs": n_songs_total,
            "idiom_songs": _idiom_index(artists)}


def _idiom_index(artists):
    """chengyu -> the songs that use it, easiest first.

    Built after slugs are assigned, because the whole value of this index is
    that every entry is a link. Easiest-first because someone who followed the
    "in 75 songs" link is looking for a song to learn from, not a census.
    """
    idx = {}
    for a in artists:
        for s in a["songs"]:
            for word, count in s["idioms"]:
                idx.setdefault(word, []).append({
                    "artist": a["slug"], "artist_label": full_name(a),
                    "genre": a.get("genre"), "slug": s["slug"],
                    "title": s["title"], "count": count, "easy": s["easy"],
                    # The whole level split, not a coverage figure at one or
                    # two chosen levels: the stacked bar answers "is this at my
                    # level" for every level at once, and picking two numbers
                    # out of it just makes the reader do the same work twice.
                    "levels": s["levels"], "tokens": s["tokens"]})
    for rows in idx.values():
        rows.sort(key=lambda r: -r["easy"])
    return idx


def _distinctive(g, corpus_words, corpus_docs, n_songs_total, top=12):
    """Words this artist uses far more than the corpus does.

    Plain frequency is useless here -- every artist's top words are 我, 的, 你.
    So we score the ratio of the artist's rate for a word to the whole corpus's
    rate, which surfaces what is characteristic rather than what is common.

    Scored on how many of the artist's SONGS use the word, not how many times
    it occurs. Token frequency has the same flaw the leaderboard had: 王菲's
    corpus contains a recitation of the 金刚经 and a Song-dynasty poem, and by
    token count those two tracks handed her a "distinctive vocabulary" of 须,
    者, 所 -- classical particles she does not otherwise use. Counting songs
    makes one atypical track worth exactly one song.

    Two further guards:
      * the word must appear in a real share of their catalogue, not 3 songs
        out of 60, or the list fills with one-offs
      * it must be common enough corpus-wide to have a stable comparison; a
        word seen in two songs total has a huge ratio and no meaning
    """
    n = len(g["songs"])
    if not n or not n_songs_total:
        return []
    min_docs = max(3, round(0.08 * n))
    scored = []
    for w, docs in g["docs"].items():
        if docs < min_docs:
            continue
        cdocs = corpus_docs.get(w, 0)
        if cdocs < 8:
            continue
        ratio = (docs / n) / (cdocs / n_songs_total)
        if ratio <= 1.5:
            continue
        scored.append((ratio, w, docs))
    scored.sort(reverse=True)
    return [(w, round(r, 1), d) for r, w, d in scored[:top]]


def _median(xs):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[len(xs) // 2]


def _learnability(songs):
    """Per-level learnability of the *unknown* part of an artist's songs.

    This is the non-coverage half of the app's learning value: how few of the
    distinct words sit above your level, and how often those unknown words
    repeat (so they can actually be learned). We take the median song at each
    level. Returns {lv: 0-100}.

    Kept separate from the coverage Y-axis so the scatter's two axes are more
    orthogonal: up = "how much you already know", right = "how learnable the
    rest is".
    """
    cfg = hskdata.config()["learning_value"]
    cap = cfg["repetition_cap"]
    w_rep = cfg["weight_repetition"]
    w_few = cfg["weight_few_unknown"]
    w_sum = w_rep + w_few
    out = {}
    for lv in LEVELS:
        fews, reps = [], []
        for s in songs:
            tot = s.get("unique") or 0
            unk = s.get("unk", {}).get(lv, 0)
            if tot:
                fews.append(1.0 - min(unk / tot, 1.0))
            reps.append(min(s.get("rep", {}).get(lv, 0.0), cap) / cap)
        if not fews:
            out[lv] = 0.0
            continue
        few = _median(fews)
        rep = _median(reps)
        out[lv] = 100.0 * (w_few * few + w_rep * rep) / w_sum
    return out


def data(force=False):
    with _lock:
        if force or _cache["data"] is None or time.time() - _cache["built"] > TTL:
            _cache["data"] = _build()
            _cache["built"] = time.time()
        return _cache["data"]


# --------------------------------------------------------------------------
# rendering

def e(s):
    return html.escape(str(s), quote=True)


def _bar(levels, cut_after=None):
    """Stacked level bar. Inline styles so it survives being screenshotted.

    `cut_after` draws a tick at the cumulative boundary after that level. In a
    table sorted by HSK 1-3 share, the eye follows the big dark HSK 1 block
    instead -- and that block is NOT the sort key, so a correctly sorted table
    reads as unsorted. The tick gives the sorted quantity a visible edge, which
    then steps down the column monotonically.
    """
    colors = {"1": "#2e7d32", "2": "#66a83a", "3": "#a8c14a", "4": "#e0b83a",
              "5": "#e08a3a", "6": "#d9603a", "7": "#a94a8c", "8": "#6b6b8c"}
    segs, cum, at, cums = [], 0.0, None, []
    for lv in LEVELS + (BEYOND,):
        w = levels.get(lv, 0)
        cum += w
        cums.append(f"{min(cum, 100):.2f}")
        if lv == cut_after:
            at = cum
        if w < 0.35:
            continue
        segs.append(f'<span title="{e(LEVEL_LABELS[lv])}: {w:.1f}%" '
                    f'style="width:{w:.2f}%;background:{colors[lv]}"></span>')
    tick = ''
    if at is not None:
        tick = (f'<i class="lvcut" style="left:{min(at, 100):.2f}%" '
                f'title="{at:.0f}% known at HSK 1-3">')
    # Cumulative share known at each level, so the level slider can move the
    # tick and re-sort the table without another request.
    data = f' data-cum="{",".join(cums)}"' if at is not None else ''
    return f'<span class="lvbar"{data}>{"".join(segs)}{tick}</span>'


SMALL_SET = 8          # at or below this many visible points, name them all
TOP_COV, TOP_LEARN = 3, 3


def _label_slugs(pts):
    """Slugs to label, given the points currently visible at the chosen level.

    `pts` is [{slug, cov, learn}]. The extremes of BOTH axes get a name: the
    highest and lowest coverage, and the most and least learnable. A small
    selection (after a genre filter) is labelled outright, since there is room
    and a filtered view of four dots with one name reads as broken.

    Ties are broken by slug so the choice is deterministic — otherwise two
    artists a tenth of a point apart could swap names on an unrelated redraw.
    """
    if len(pts) <= SMALL_SET:
        return {p["slug"] for p in pts}
    by_cov = sorted(pts, key=lambda p: (p["cov"], p["slug"]))
    by_learn = sorted(pts, key=lambda p: (p["learn"], p["slug"]))
    picked = (by_cov[:TOP_COV] + by_cov[-TOP_COV:]
              + by_learn[:TOP_LEARN] + by_learn[-TOP_LEARN:])
    return {p["slug"] for p in picked}


def _scatter(rows, width=760, height=460):
    """Two per-level axes, as a plain inline SVG.

    Y = share of a typical song you already know at the chosen HSK level
    (coverage), pinned to 0-100% (a true percentage). X = how learnable the
    *rest* is: few unknown words, and those words repeating (the non-coverage
    half of the app's learning value). The X axis is pinned to the actual
    spread of that metric across HSK 1..HSK 7-9 (the outer levels) rather than
    a fixed 0-100, since a song is almost never 0/100 learnable and a 0-100
    range would crush every dot into a sliver. Both axes are per-level, so the
    dots slide (both axes) when the level changes.

    The page server-renders the default level (HSK 3) with no script, so it is
    crawlable and screenshot-safe; a `data-pts` blob carries all levels so the
    slider can re-plot the dots client-side with no framework.
    """
    if len(rows) < 3:
        return ""
    pad_l, pad_r, pad_t, pad_b = 62, 18, 18, 52
    # Y is a true percentage -> 0-100. X (learnability) is pinned to the real
    # data extremes across all levels so the cloud uses the full width.
    y0, y1 = 0.0, 100.0
    learn_vals = [v for a in rows for lv in LEVELS for v in (a["learn"][lv],)]
    if learn_vals:
        x0 = max(0.0, math.floor(min(learn_vals)) - 2)
        x1 = min(100.0, math.ceil(max(learn_vals)) + 2)
    else:
        x0, x1 = 0.0, 100.0
    if x1 - x0 < 10:
        x1 = x0 + 10
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b

    def px(v):
        return pad_l + (v - x0) / (x1 - x0) * iw

    def py(v):
        return pad_t + (1 - (v - y0) / (y1 - y0)) * ih

    parts = [f'<svg class="scatter" viewBox="0 0 {width} {height}" '
             f'role="img" aria-label="Chinese artists plotted by how much of a '
             f'typical song a learner at the chosen HSK level already knows '
             f'(up), against how learnable the remaining words are (right)">']
    for i in range(5):
        yv = y0 + (y1 - y0) * i / 4
        y = py(yv)
        parts.append(f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" '
                     f'x2="{width-pad_r}" y2="{y:.1f}"/>')
        parts.append(f'<text class="ax" x="{pad_l-8}" y="{y+4:.1f}" '
                     f'text-anchor="end">{yv:.0f}%</text>')
        xv = x0 + (x1 - x0) * i / 4
        x = px(xv)
        parts.append(f'<line class="grid" x1="{x:.1f}" y1="{pad_t}" '
                     f'x2="{x:.1f}" y2="{height-pad_b}"/>')
        parts.append(f'<text class="ax" x="{x:.1f}" y="{height-pad_b+18}" '
                     f'text-anchor="middle">{xv:.0f}%</text>')
    parts.append(f'<text class="axlabel" x="{pad_l+iw/2:.0f}" y="{height-8}" '
                 f'text-anchor="middle">Learnability of what\'s unknown — few '
                 f'new words, and they repeat</text>')
    parts.append(f'<text class="axlabel" id="yaxis-label" '
                 f'transform="translate(14,{pad_t+ih/2:.0f}) rotate(-90)" '
                 f'text-anchor="middle">Share of a typical song you know at '
                 f'HSK 3</text>')

    # Which points get a name is recomputed from whatever is on screen: the
    # extremes of the CURRENT level and the CURRENT genre filter. Filter to
    # Rock and you get Rock's extremes, not the whole corpus's; a dimmed point
    # is never labelled. _label_slugs() is the rule, and the script mirrors it
    # exactly so the server's first paint matches what the client recomputes.
    named = _label_slugs(
        [{"slug": a["slug"], "cov": 100 * a["median_cov"]["3"],
          "learn": a["learn"]["3"]} for a in rows])

    # Per-level payload for the client-side slider. X (learn) and Y (cov) are
    # both 0-100 fractions; the script maps them with the fixed scale.
    pts = {
        "scale": {"pad_l": pad_l, "pad_r": pad_r, "pad_t": pad_t,
                  "pad_b": pad_b, "iw": iw, "ih": ih, "w": width, "h": height,
                  "x0": x0, "x1": x1, "y0": y0, "y1": y1,
                  "small": SMALL_SET, "topc": TOP_COV, "topl": TOP_LEARN},
        "points": [{
            "slug": a["slug"], "name": a["name"], "full": full_name(a),
            "genre": a.get("genre"),
            "learn": {lv: round(a["learn"][lv], 1) for lv in LEVELS},
            "cov": {lv: round(100 * a["median_cov"][lv], 1) for lv in LEVELS},
        } for a in rows],
    }

    for a in rows:
        x, y = px(a["learn"]["3"]), py(100 * a["median_cov"]["3"])
        label = e(full_name(a))
        fill = _genre_color(a)
        g = a.get("genre") or ""
        parts.append(
            f'<a href="/artist/{e(a["slug"])}" class="ptlink" '
            f'data-name="{e(a["name"])}" data-slug="{e(a["slug"])}" '
            f'data-genre="{e(g)}">'
            f'<circle class="pt" cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{fill}">'
            f'<title>{label}: {100*a["median_cov"]["3"]:.0f}% known at HSK 3, '
            f'learnability {a["learn"]["3"]:.0f}/100</title></circle></a>')
    parts.append(
        f'<script type="application/json" id="scatter-data" '
        f'data-default="3">{json.dumps(pts, ensure_ascii=False)}</script>')

    for a in rows:
        x, y = px(a["learn"]["3"]), py(100 * a["median_cov"]["3"])
        anchor = "end" if x > pad_l + iw * 0.75 else "start"
        dx = -9 if anchor == "end" else 9
        cls = "ptlabel" if a["slug"] in named else "ptlabel hidden"
        g = a.get("genre") or ""
        parts.append(f'<text class="{cls}" data-slug="{e(a["slug"])}" '
                     f'data-genre="{e(g)}" '
                     f'data-dx="{dx}" x="{x+dx:.1f}" y="{y+4:.1f}" '
                     f'text-anchor="{anchor}">{e(a["name"])}</text>')
    parts.append('</svg>')
    return _genre_legend(rows) + "".join(parts)


GENRE_COLORS = {
    "Pop": "#4e79a7", "Rap": "#f28e2b", "Rock": "#2e8b57", "Folk": "#76b7b2",
    "R&B": "#b07aa1", "Gufeng": "#d4302a", "Indie": "#edc948",
    "Ballad": "#9c755f", "Electronic": "#ff9da7", "Jazz": "#bab0ac",
    "Soundtrack": "#86bcb6",
}
GENRE_FALLBACK = "#9aa0a6"  # neutral grey for untagged artists


def _genre_color(g):
    return GENRE_COLORS.get(g.get("genre"), GENRE_FALLBACK)


def _legend():
    colors = {"1": "#2e7d32", "2": "#66a83a", "3": "#a8c14a", "4": "#e0b83a",
              "5": "#e08a3a", "6": "#d9603a", "7": "#a94a8c", "8": "#6b6b8c"}
    return ('<div class="legend lvlegend">' + "".join(
        f'<span><i style="background:{c}"></i>{e(LEVEL_LABELS[lv])}</span>'
        for lv, c in colors.items()) + "</div>")


def _genre_legend(rows):
    present = {a.get("genre") for a in rows if a.get("genre")}
    items = "".join(
        f'<span class="glitem" data-genre="{g}" role="button" tabindex="0" '
        f'aria-pressed="false"><i style="background:{c}"></i>{g}</span>'
        for g, c in GENRE_COLORS.items() if g in present)
    return (f'<div class="legend genrelegend" id="genre-legend" '
            f'aria-label="Filter by genre">{items}'
            f'<span class="glclear" id="genre-clear" hidden>clear ✕</span></div>')


_LEVEL_ORDER = ("1", "2", "3", "4", "5", "6", "7")


def _level_slider():
    """HSK level control for the scatter. Default HSK 3; client-side JS reads
    #scatter-data and re-plots the dots (and the Y-axis label) for the level.

    A range input rather than a select: the levels are an ordered scale, and
    dragging along it animates the dots rising, which is the whole point of the
    control. It also matches the level slider in the app's own header.
    """
    # Ticks are absolutely positioned at the fraction of the track each value
    # sits at, not spaced evenly by flexbox: a range thumb's CENTRE travels from
    # half-a-thumb in to half-a-thumb from the end, so evenly-spaced labels
    # drift out of line with the values they name. The container insets itself
    # by half a thumb (--thumb) so 0% and 100% land on the real end positions.
    n = len(_LEVEL_ORDER)
    ticks = "".join(
        f'<span style="left:{(i / (n - 1)) * 100:.4f}%">{e(LEVEL_LABELS[lv])}</span>'
        for i, lv in enumerate(_LEVEL_ORDER))
    return ('<div class="lvslider">'
            '<div class="lvslider-head">'
            '<label for="lvselect">Your HSK level</label>'
            '<b id="lvselect-out">HSK 3</b>'
            '</div>'
            f'<input type="range" id="lvselect" min="1" max="{n}"'
            ' step="1" value="3" aria-label="Choose your HSK level"'
            ' aria-valuetext="HSK 3">'
            f'<div class="lvticks" aria-hidden="true">{ticks}</div></div>')


def _scatter_script():
    """Inline script: re-plots the scatter for the chosen level and genre.

    No framework, no network -- reads the JSON blob already in the page. The
    server-rendered default (HSK 3, no filter) is the no-JS / crawler fallback,
    and the script recomputes it identically on load, so the two cannot drift.

    Labels are chosen from what is ON SCREEN: the extremes of the current level
    among the current genre. Both inputs matter -- a dot that moves is a dot
    whose label must move with it, and a dot that is dimmed must lose its name.
    """
    return (
        '<script>\n'
        '(function(){\n'
        '  var blob = document.getElementById("scatter-data");\n'
        '  var sel = document.getElementById("lvselect");\n'
        '  if (!blob || !sel) return;\n'
        '  var data = JSON.parse(blob.textContent);\n'
        '  var pts = data.points, s = data.scale;\n'
        '  var out = document.getElementById("lvselect-out");\n'
        '  var LABELS = {"1":"HSK 1","2":"HSK 2","3":"HSK 3","4":"HSK 4",\n'
        '                "5":"HSK 5","6":"HSK 6","7":"HSK 7-9"};\n'
        '  var bySlug = {}, active = null;\n'
        '  pts.forEach(function(p){ bySlug[p.slug] = p; });\n'
        '  function cxFor(p, lv){\n'
        '    return s.pad_l + (p.learn[lv] - s.x0) / (s.x1 - s.x0) * s.iw;\n'
        '  }\n'
        '  function cyFor(p, lv){\n'
        '    return s.pad_t + (1 - (p.cov[lv] - s.y0) / (s.y1 - s.y0)) * s.ih;\n'
        '  }\n'
        '  function visible(){\n'
        '    return pts.filter(function(p){\n'
        '      return !active || (p.genre || "") === active;\n'
        '    });\n'
        '  }\n'
        '  // Mirror of _label_slugs() in app/public.py -- keep the two in step.\n'
        '  function labelSet(lv){\n'
        '    var v = visible();\n'
        '    var out = {};\n'
        '    if (v.length <= s.small) {\n'
        '      v.forEach(function(p){ out[p.slug] = 1; });\n'
        '      return out;\n'
        '    }\n'
        '    function pick(key, n){\n'
        '      var a = v.slice().sort(function(p, q){\n'
        '        return (p[key][lv] - q[key][lv]) ||\n'
        '               (p.slug < q.slug ? -1 : p.slug > q.slug ? 1 : 0);\n'
        '      });\n'
        '      a.slice(0, n).concat(a.slice(-n)).forEach(function(p){\n'
        '        out[p.slug] = 1;\n'
        '      });\n'
        '    }\n'
        '    pick("cov", s.topc);\n'
        '    pick("learn", s.topl);\n'
        '    return out;\n'
        '  }\n'
        '  function apply(){\n'
        '    var lv = String(sel.value);\n'
        '    var text = LABELS[lv] || ("HSK " + lv);\n'
        '    if (out) out.textContent = text;\n'
        '    sel.setAttribute("aria-valuetext", text);\n'
        '    var ylab = document.getElementById("yaxis-label");\n'
        '    if (ylab) ylab.textContent =\n'
        '      "Share of a typical song you know at " + text;\n'
        '    document.querySelectorAll("a.ptlink").forEach(function(a){\n'
        '      var p = bySlug[a.getAttribute("data-slug")];\n'
        '      if (!p) return;\n'
        '      var c = a.querySelector("circle.pt");\n'
        '      if (!c) return;\n'
        '      c.setAttribute("cx", cxFor(p, lv));\n'
        '      c.setAttribute("cy", cyFor(p, lv));\n'
        '      a.classList.toggle("dim", !!active && (p.genre || "") !== active);\n'
        '      var t = c.querySelector("title");\n'
        '      if (t) t.textContent = p.full + ": " + Math.round(p.cov[lv]) +\n'
        '        "% known at " + text + ", learnability " + Math.round(p.learn[lv]) +\n'
        '        "/100";\n'
        '    });\n'
        '    // Labels follow BOTH coordinates. Updating only y left names\n'
        '    // stranded up to 197px from their dot once the x axis became\n'
        '    // level-dependent too.\n'
        '    var show = labelSet(lv);\n'
        '    var dots = [], live = [];\n'
        '    pts.forEach(function(p){\n'
        '      if (active && (p.genre || "") !== active) return;\n'
        '      dots.push({slug: p.slug, x: cxFor(p, lv), y: cyFor(p, lv)});\n'
        '    });\n'
        '    document.querySelectorAll("text.ptlabel").forEach(function(t){\n'
        '      var p = bySlug[t.getAttribute("data-slug")];\n'
        '      if (!p) return;\n'
        '      var on = !!show[p.slug];\n'
        '      t.classList.toggle("hidden", !on);\n'
        '      if (on) live.push({t: t, p: p, x: cxFor(p, lv), y: cyFor(p, lv),\n'
        '                         w: t.getComputedTextLength() + 4});\n'
        '    });\n'
        '    // Place each name in the least-crowded spot around its dot.\n'
        '    // A fixed offset put names straight through the cloud: at HSK 7-9\n'
        '    // one label covered 11 other dots, which hides the very data the\n'
        '    // chart is for. Candidates are tried right, left, above, below and\n'
        '    // the diagonals; the cheapest wins.\n'
        '    var CAND = [[9, 4, "start"], [-9, 4, "end"], [0, -9, "middle"],\n'
        '                [0, 16, "middle"], [9, -7, "start"], [9, 15, "start"],\n'
        '                [-9, -7, "end"], [-9, 15, "end"],\n'
        '                // Further out, for dots in a dense cluster where every\n'
        '                // near position is taken. Penalised by distance below,\n'
        '                // so a name only drifts when it has to.\n'
        '                [0, -22, "middle"], [0, 29, "middle"],\n'
        '                [22, -20, "start"], [-22, -20, "end"],\n'
        '                [22, 27, "start"], [-22, 27, "end"]];\n'
        '    var H = 12;\n'
        '    function box(l, c){\n'
        '      var x = l.x + c[0], y = l.y + c[1];\n'
        '      var left = c[2] === "end" ? x - l.w : c[2] === "middle" ? x - l.w / 2 : x;\n'
        '      return {l: left, r: left + l.w, t: y - H + 2, b: y + 3, x: x, y: y, a: c[2]};\n'
        '    }\n'
        '    function cost(l, b){\n'
        '      var n = 0, i;\n'
        '      for (i = 0; i < dots.length; i++) {\n'
        '        var d = dots[i];\n'
        '        if (d.slug === l.p.slug) continue;\n'
        '        var nx = Math.max(b.l, Math.min(d.x, b.r));\n'
        '        var ny = Math.max(b.t, Math.min(d.y, b.b));\n'
        '        var dx = nx - d.x, dy = ny - d.y;\n'
        '        if (dx * dx + dy * dy <= 30) n += 3;\n'
        '      }\n'
        '      for (i = 0; i < placedBoxes.length; i++) {\n'
        '        var o = placedBoxes[i];\n'
        '        if (b.l < o.r && b.r > o.l && b.t < o.b && b.b > o.t) n += 5;\n'
        '      }\n'
        '      if (b.l < s.pad_l || b.r > s.pad_l + s.iw ||\n'
        '          b.t < s.pad_t || b.b > s.pad_t + s.ih) n += 8;\n'
        '      return n;\n'
        '    }\n'
        '    var placedBoxes = [];\n'
        '    // Deterministic order, so the same view always lays out the same.\n'
        '    live.sort(function(a, b){\n'
        '      return (a.y - b.y) || (a.p.slug < b.p.slug ? -1 : 1);\n'
        '    });\n'
        '    live.forEach(function(l){\n'
        '      var best = null, bestC = Infinity;\n'
        '      for (var i = 0; i < CAND.length; i++) {\n'
        '        var b = box(l, CAND[i]);\n'
        '        // Prefer the nearest workable spot: order breaks ties, and\n'
        '        // distance keeps a name tethered to the dot it belongs to.\n'
        '        var d = Math.abs(CAND[i][0]) + Math.abs(CAND[i][1]);\n'
        '        var c = cost(l, b) + i * 0.05 + d * 0.02;\n'
        '        if (c < bestC) { bestC = c; best = b; }\n'
        '      }\n'
        '      l.t.setAttribute("text-anchor", best.a);\n'
        '      l.t.setAttribute("x", best.x);\n'
        '      l.t.setAttribute("y", best.y);\n'
        '      placedBoxes.push(best);\n'
        '    });\n'
        '  }\n'
        '  sel.addEventListener("input", apply);\n'
        '  sel.addEventListener("change", apply);\n'
        '  // Genre filter: click a legend swatch to keep only that genre.\n'
        '  var legend = document.getElementById("genre-legend");\n'
        '  var clearBtn = document.getElementById("genre-clear");\n'
        '  function syncLegend(){\n'
        '    if (legend) legend.querySelectorAll(".glitem").forEach(function(it){\n'
        '      var on = it.getAttribute("data-genre") === active;\n'
        '      it.classList.toggle("active", on);\n'
        '      it.setAttribute("aria-pressed", on ? "true" : "false");\n'
        '    });\n'
        '    if (clearBtn) clearBtn.hidden = !active;\n'
        '  }\n'
        '  function toggle(g){\n'
        '    active = (active === g) ? null : g;\n'
        '    syncLegend();\n'
        '    apply();\n'
        '  }\n'
        '  if (legend) legend.querySelectorAll(".glitem").forEach(function(it){\n'
        '    var g = it.getAttribute("data-genre");\n'
        '    it.addEventListener("click", function(){ toggle(g); });\n'
        '    it.addEventListener("keydown", function(e){\n'
        '      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(g); }\n'
        '    });\n'
        '  });\n'
        '  if (clearBtn) clearBtn.addEventListener("click", function(){\n'
        '    active = null; syncLegend(); apply();\n'
        '  });\n'
        '  apply();\n'
        '})();\n'
        '</script>')


# The one top bar. Every page — the app, /about and the public analytics pages
# — renders this exact markup, so the nav never changes shape as you move
# around. Static pages get it via the {{nav}} placeholder in main._page_html.
# Each entry is (href, label, live?); a dead entry is an article that is
# written but not published yet, shown greyed so the shape of the section is
# visible without promising a link that goes nowhere.
ANALYTICS_MENU = [
    ("/artists", "Artists", True),
    ("/songs", "Songs", False),
    ("/core-words", "Core words", False),
]


def _nav_html():
    items = []
    for href, label, live in ANALYTICS_MENU:
        items.append(f'<a href="{href}">{label}</a>' if live
                     else f'<span class="soon">{label}<em>soon</em></span>')
    return ('<nav id="topnav">'
            '<a href="/about" id="aboutNav">About</a>'
            '<span class="navdrop">'
            '<a href="/artists" id="analyticsNav">Analytics <b>&#9662;</b></a>'
            '<span class="navmenu">' + "".join(items) + '</span>'
            '</span></nav>')


NAV_HTML = _nav_html()

# --- the written layer -----------------------------------------------------
# The article prose and the author's identity live in app/article.py, which is
# gitignored: what is on this site is authored writing, not part of the
# analysis tool, so it ships with the deploy rather than with the source. This
# module is the renderers; that one is the words.
#
# The import is lazy and by function, not at module scope, because article.py
# imports this module -- doing it at the top would be a cycle. Absent, every
# accessor below returns nothing and the pages simply carry no prose, no
# byline and no author name, which is exactly what a fresh clone should be.

def _article_mod():
    try:
        from . import article  # type: ignore
        return article
    except ImportError:
        return None


def author_meta():
    """Head tags naming the author, when there is an author to name."""
    m = _article_mod()
    return m.author_meta() if m else ""


def byline():
    m = _article_mod()
    return m.byline() if m else ""


def author_block():
    """The fuller version, for the foot of /about."""
    m = _article_mod()
    return m.author_block() if m else ""

# The same control the app carries, on every public page, reading the same
# localStorage key. Server-rendered at HSK 3: that is the no-JS view and the
# one a crawler indexes, and level.js recomputes it identically on load so the
# two cannot drift.
LEVELBOX_HTML = (
    '<div id="levelbox">'
    '<label for="levelSlider" title="What a learner at this level already '
    'knows. Sorts and marks the tables on this page.">'
    'My level: <b id="levelLabel">HSK3</b></label>'
    '<input type="range" id="levelSlider" min="1" max="7" step="1" value="3" '
    'aria-label="Your HSK level">'
    '</div>')


HEAD = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{desc}">
<title>{title}</title>
<link rel="canonical" href="{origin}{path}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="Mandoremi">
<meta property="og:url" content="{origin}{path}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="{origin}/static/card.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{origin}/static/card.png">
{author_meta}<link rel="stylesheet" href="/static/style.css?v={v}">"""


def page(origin, path, title, desc, body, v):
    return (f'<!DOCTYPE html><html lang="en"><head>'
            + HEAD.format(origin=origin, path=e(path), title=e(title),
                          desc=e(desc), v=v, author_meta=author_meta())
            + '</head><body><header>'
              '<h1 id="brand"><a href="/" style="color:inherit">Mandoremi</a></h1>'
            + NAV_HTML
            + LEVELBOX_HTML
            + '<div id="userbox" style="margin-left:auto">'
              '<a href="/">Open the app →</a>'
              '</div></header><main>' + body
            + '</main>'
              f'<script src="/static/level.js?v={v}"></script>'
              f'<script src="/static/telemetry.js?v={v}"></script>'
            + '</body></html>')


SNAPSHOT_PATH = os.environ.get(
    "MANDOREMI_SNAPSHOT", os.path.join(DATA_DIR, "leaderboard_snapshot.json"))
_SNAPSHOT = {"loaded": False, "data": None}


def snapshot():
    """The frozen numbers behind the /artists article, or None.

    /artists is a published article whose prose makes checkable claims, so its
    figures are pinned at the moment it was written (tools/snapshot_leaderboard
    .py) rather than recomputed on every request. Seeding more songs must not be
    able to quietly contradict a sentence.

    Returning None falls back to building the page from live data, which is what
    the tests exercise and what a fresh install with no snapshot yet does.
    """
    if not _SNAPSHOT["loaded"]:
        try:
            with open(SNAPSHOT_PATH, encoding="utf-8") as f:
                _SNAPSHOT["data"] = json.load(f)
        except (OSError, ValueError):
            _SNAPSHOT["data"] = None
        _SNAPSHOT["loaded"] = True
    return _SNAPSHOT["data"]


def _live_snapshot():
    """Same shape as the frozen file, built from live data.

    Only the fields the page actually renders; the editorial parts of the
    article (chengyu glosses, the HSK-gap tables) have no live equivalent and
    are simply omitted, so those sections disappear rather than render wrong.
    """
    d = data()
    ranked = d["ranked"]
    rank_of = {id(a): i for i, a in enumerate(ranked, 1)}

    def brief(a):
        return {"slug": a["slug"], "label": full_name(a),
                "pct": round(a["easy_pct"], 1),
                "cov": round(100 * a["median_cov"]["3"]),
                "learn": round(a["learn"]["3"])}

    return {
        "corpus": {"songs": sum(a["songs_n"] for a in d["artists"]),
                   "artists": len(d["artists"]), "ranked": len(ranked),
                   "featured": len(d["featured"])},
        "figures": {
            "easiest": [brief(a) for a in ranked[:5]],
            "hardest": [brief(a) for a in ranked[-5:][::-1]],
            "spread": round(ranked[0]["easy_pct"] - ranked[-1]["easy_pct"])
            if ranked else 0,
            "levels": [], "idioms": [], "gap_single": [], "gap_late": [],
            "learnable": [], "thin": [], "jaychou": None, "mentions": {},
            "idiom_song_pct": 0.0,
        },
        "featured": [{"rank": rank_of[id(a)], "slug": a["slug"],
                      "name": a["name"], "english": a.get("english"),
                      "genre": a.get("genre"), "songs_n": a["songs_n"],
                      "easy_pct": round(a["easy_pct"], 1),
                      "levels": a["levels"], "median_cov": a["median_cov"],
                      "learn": a["learn"]}
                     for a in d["featured"]],
    }


def _alink(x):
    """Link to an artist page from a frozen {slug,label} record."""
    return f'<a href="/artist/{e(x["slug"])}">{e(x["label"])}</a>'


def _mention(snap, slug):
    """Link an artist the prose names. Falls back to the bare slug's page if the
    snapshot predates the mention, so a sentence never loses its link."""
    label = snap["figures"].get("mentions", {}).get(slug)
    return _alink({"slug": slug, "label": label or slug})


def _gif(name, alt, width, height):
    """An animated aside. WebP first, GIF only for browsers that cannot read
    animated WebP -- the same Chernobyl clip is 2.4MB as a GIF and 282KB as
    WebP, and a meme is not worth two megabytes on a phone. Width/height are
    set so the text below does not jump when it loads."""
    return (f'<figure class="gif">'
            f'<picture>'
            f'<source srcset="/static/img/{name}.webp" type="image/webp">'
            f'<img src="/static/img/{name}.gif" alt="{e(alt)}" '
            f'width="{width}" height="{height}" loading="lazy" decoding="async">'
            f'</picture></figure>')


def _clip(name, alt, width, height, caption=None):
    """A silent looping video used where a GIF would be.

    VP9 does in 60KB what the same three seconds cost 1MB as a GIF, which is
    the difference between a decorative aside and a decorative aside that
    dominates the page weight. The GIF stays as the fallback INSIDE the video
    element, so a browser that cannot play WebM still sees the clip.

    autoplay needs `muted` to be honoured at all, and `playsinline` or iOS
    Safari takes the video fullscreen instead of leaving it in the article.
    width/height are the intrinsic size and the CSS holds the ratio, so the
    space is reserved before the file arrives and nothing below it jumps.
    """
    cap = f'<figcaption>{caption}</figcaption>' if caption else ''
    return (f'<figure class="gif">'
            f'<video width="{width}" height="{height}" autoplay muted loop '
            f'playsinline preload="metadata" aria-label="{e(alt)}">'
            f'<source src="/static/img/{name}.webm" type="video/webm">'
            f'<img src="/static/img/{name}.gif" alt="{e(alt)}" '
            f'width="{width}" height="{height}" loading="lazy" decoding="async">'
            f'</video>{cap}</figure>')


def _still(name, alt, width, height, caption=None):
    """A plain image aside. No animation, no weight worth discussing."""
    cap = f'<figcaption>{caption}</figcaption>' if caption else ''
    return (f'<figure class="gif">'
            f'<img src="/static/img/{name}" alt="{e(alt)}" '
            f'width="{width}" height="{height}" loading="lazy" decoding="async">'
            f'{cap}</figure>')


def _pct_table(rows, head):
    out = [f'<table class="board"><thead><tr><th>Artist</th>'
           f'<th class="num">{head}</th></tr></thead><tbody>']
    for x in rows:
        out.append(f'<tr><td>{_alink(x)}</td>'
                   f'<td class="num"><b>{x["pct"]:.0f}%</b></td></tr>')
    out.append('</tbody></table>')
    return "".join(out)


def leaderboard_html(origin, v):
    """The /artists post, or None when the written layer is not installed.

    The route treats None as a 404: a leaderboard page with the analysis but
    none of the writing is not a page anyone asked for.
    """
    m = _article_mod()
    return m.leaderboard_html(origin, v) if m else None


def chengyu_section(f, total_songs):
    m = _article_mod()
    return m.chengyu_section(f, total_songs) if m else ""


def artist_html(origin, slug, v):
    d = data()
    a = d["by_slug"].get(slug)
    if not a:
        return None
    rank = None
    if a["ranked"]:
        rank = d["ranked"].index(a) + 1
    cov3 = a["median_cov"]["3"] * 100
    cov5 = a["median_cov"]["5"] * 100
    body = [
        '<div class="panel">',
        f'<h2>{e(full_name(a))} — how hard are the lyrics?</h2>',
        f'<p>Based on <b>{a["songs_n"]} songs</b> ({a["tokens"]:,} words of '
        f'Chinese) analyzed against HSK 3.0.</p>',
        '<table class="kv"><tbody>',
        f'<tr><th>Typical song: share of words in HSK 1-3</th>'
        f'<td><b>{a["easy_pct"]:.1f}%</b>'
        + (f' — rank {rank} of {len(d["ranked"])} '
           f'(<a href="/artists">full leaderboard</a>)' if rank else
           ' <span class="muted">— too few songs to rank</span>') + '</td></tr>',
        f'<tr><th>Typical song at HSK 3</th><td>you would already know about '
        f'<b>{cov3:.0f}%</b> of the words</td></tr>',
        f'<tr><th>Typical song at HSK 5</th><td>about <b>{cov5:.0f}%</b></td></tr>',
        f'<tr><th>Songs with a chengyu</th><td>{a["idiom_pct"]:.0f}%</td></tr>',
        '</tbody></table>',
        _legend(), _bar(a["levels"]),
        '</div>',
    ]

    body.append('<div class="panel"><h2>Songs, easiest to hardest</h2>'
                '<p class="muted">Ranked by the share of HSK 1-3 vocabulary. '
                'Lyrics are not shown or stored — open a song to see its '
                'vocabulary breakdown, or <a href="/">paste the lyrics</a> for '
                'the colour-coded view.</p>'
                '<table class="board"><thead><tr><th>Song</th>'
                '<th class="num">HSK 1-3</th><th class="num">Known at HSK 5</th>'
                '<th class="num">Words</th>'
                '<th class="num">Distinct</th></tr></thead><tbody>')
    for s in a["songs"]:
        # "HSK 1-3" and coverage-at-HSK-3 are the same number by definition,
        # so the second column shows HSK 5 instead of restating the first.
        body.append(f'<tr><td><a href="/song/{e(a["slug"])}/{e(s["slug"])}">'
                    f'{e(s["title"])}</a></td>'
                    f'<td class="num">{s["easy"]:.0f}%</td>'
                    f'<td class="num">{s["cov5"]*100:.0f}%</td>'
                    f'<td class="num muted">{s["tokens"]}</td>'
                    f'<td class="num muted">{s["unique"]}</td></tr>')
    body.append('</tbody></table></div>')

    if a["distinctive"]:
        body.append(
            '<div class="panel"><h2>Most distinctive words</h2>'
            '<p class="muted">Words this artist uses far more often than the '
            'rest of the corpus does — what makes their writing recognisable, '
            'rather than simply what they say most.</p>'
            '<table class="board"><thead><tr><th>Word</th>'
            '<th class="num">vs. other artists</th>'
            '<th class="num">in their songs</th></tr></thead><tbody>'
            + "".join(
                f'<tr><td><span class="tok next">{e(w)}</span></td>'
                f'<td class="num">{r:.1f}&times;</td>'
                f'<td class="num muted">{d} of {a["songs_n"]}</td></tr>'
                for w, r, d in a["distinctive"])
            + '</tbody></table></div>')

    if a["beyond"]:
        body.append('<div class="panel"><h2>Words this artist leans on that HSK 1-6 '
                    'never teaches</h2><p class="muted">Most frequent vocabulary in '
                    'the HSK 7-9 band or outside the lists entirely.</p><p>'
                    + " ".join(f'<span class="tok hard">{e(w)}</span>'
                               f'<span class="muted" style="font-size:.8rem">'
                               f'&nbsp;{c}&times;&nbsp;&nbsp;</span>'
                               for w, c in a["beyond"]) + '</p></div>')

    if a["grammar"]:
        body.append('<div class="panel"><h2>Grammar you will meet</h2>'
                    '<table class="kv"><tbody>' + "".join(
                        f'<tr><th>{e(k)}</th><td>{p:.0f}% of songs</td></tr>'
                        for k, p in a["grammar"]) + '</tbody></table></div>')

    return page(origin, f"/artist/{slug}",
                f"{a['name']}: Chinese lyrics by HSK level",
                f"{a['name']} lyrics analyzed by HSK level across "
                f"{a['songs_n']} songs: {a['easy_pct']:.0f}% of the vocabulary is "
                f"HSK 1-3. Which songs to learn first.",
                "".join(body), v)


def _chengyu_panel(idioms, d):
    """The four things every good chengyu reference shows — characters,
    reading, what the characters literally say, what the phrase actually
    means — plus the one thing only a lyrics corpus can add: how many songs
    reach for it.

    The literal column is the reason this is a table and not a word list. A
    chengyu is opaque precisely because its meaning is not the sum of its
    characters, and a learner who never sees both halves has to memorise it as
    an arbitrary four-syllable blob.
    """
    dictionary.load()
    docs, n_songs = d.get("idiom_docs") or {}, d.get("n_songs") or 0
    rows = []
    for word, count in idioms:
        cy = chengyu.entry(word)
        lit = cy["literal"] or "—"
        # An assembled breakdown is a reading aid, not a translation; the
        # dotted underline is the same signal the app uses for idiom tokens.
        lit_html = (f'<span class="litguess" title="Assembled character by '
                    f'character from the dictionary, not a set translation">'
                    f'{e(lit)}</span>' if lit != "—" and not cy["literal_exact"]
                    else e(lit))
        py = cy["pinyin"] or ""
        py_html = (f'<span class="pyguess" title="Reading assembled character '
                   f'by character — this chengyu has no dictionary entry of '
                   f'its own">{e(py)}</span>'
                   if py and not cy["pinyin_exact"] else e(py))
        seen = docs.get(word, 0)
        rows.append(
            f'<tr><td class="cy"><b>{e(word)}</b>'
            + (f'<span class="times">&times;{count}</span>' if count > 1 else '')
            + f'<br><span class="py">{py_html}</span></td>'
            f'<td class="lit">{lit_html}</td>'
            f'<td class="mean">{e(cy["meaning"]) if cy["meaning"] else "&mdash;"}'
            f'</td>'
            + (f'<td class="num"><a href="/chengyu/{quote(word)}">{seen:,}</a>'
               f'</td>' if seen
               else '<td class="num none">&mdash;</td>') + '</tr>')
    note = ('<p class="muted">A chengyu is a four-character idiom that packs a '
            'whole scene into four syllables. The literal column is what the '
            'characters say; the meaning is what the phrase does'
            + (f' &mdash; and the last column is how many of the {n_songs:,} '
               f'songs analysed so far use it.' if n_songs else '.')
            + '</p>')
    head = ('<tr><th class="cy">Chengyu</th><th class="lit">Literally</th>'
            '<th class="mean">Meaning</th><th class="num">Songs</th></tr>')
    return ('<div class="panel"><h2>Chengyu</h2>' + note
            + '<table class="cytbl"><thead>' + head + '</thead><tbody>'
            + "".join(rows) + '</tbody></table></div>')


def chengyu_html(origin, word, v):
    """Every song in the corpus that uses one chengyu.

    The page a reader lands on from the "in 75 songs" number, so it answers the
    question that number provokes: which songs, and are any of them at my
    level? Hence the coverage column — a list of 75 titles with no difficulty
    on it would just be a longer version of the number they clicked.
    """
    d = data()
    rows = (d.get("idiom_songs") or {}).get(word)
    if not rows:
        return None
    dictionary.load()
    cy = chengyu.entry(word)

    facts = []
    if cy["pinyin"]:
        facts.append(f'<tr><th>Pinyin</th><td>{e(cy["pinyin"])}'
                     + ('' if cy["pinyin_exact"] else
                        ' <span class="muted">(assembled character by '
                        'character — no dictionary entry for the phrase)</span>')
                     + '</td></tr>')
    if cy["literal"]:
        facts.append(f'<tr><th>Literally</th><td>{e(cy["literal"])}'
                     + ('' if cy["literal_exact"] else
                        ' <span class="muted">(character by character)</span>')
                     + '</td></tr>')
    if cy["meaning"]:
        facts.append(f'<tr><th>Meaning</th><td>{e(cy["meaning"])}</td></tr>')
    if cy["hsk"]:
        facts.append(f'<tr><th>HSK level</th><td>{LEVEL_LABELS.get(str(cy["hsk"]), cy["hsk"])}</td></tr>')
    n_songs = d.get("n_songs") or 0
    facts.append(
        f'<tr><th>Songs using it</th><td>{len(rows):,}'
        + (f' of {n_songs:,} analysed' if n_songs else '') + '</td></tr>')

    body = [
        '<div class="panel">',
        f'<p class="muted" style="margin:0 0 .3rem">'
        f'<a href="/artists">← Chinese artists by HSK difficulty</a></p>',
        f'<h2 class="cyhead">{e(word)}</h2>',
        '<table class="kv"><tbody>' + "".join(facts) + '</tbody></table>',
        '</div>',
        '<div class="panel">',
        f'<h2>Songs that use {e(word)}</h2>',
        '<p class="muted">Easiest first. Each bar is the whole song&rsquo;s '
        'vocabulary split by HSK level.</p>',
        _legend(),
        '<table class="board cyidx sortbylevel"><thead><tr><th>Song</th>'
        '<th>Artist</th>'
        '<th class="bar">HSK level split</th></tr></thead><tbody>',
    ]
    for r in rows:
        times = (f' <span class="muted" style="font-size:.8rem">&times;'
                 f'{r["count"]}</span>' if r["count"] > 1 else '')
        body.append(
            f'<tr><td><a href="/song/{e(r["artist"])}/{e(r["slug"])}">'
            f'{e(r["title"])}</a>{times}</td>'
            f'<td><a class="artistlink" href="/artist/{e(r["artist"])}">'
            f'{e(r["artist_label"])}</a></td>'
            f'<td class="bar">' + _bar(
                {lv: 100.0 * r["levels"].get(lv, 0) / r["tokens"]
                 for lv in LEVELS + (BEYOND,)} if r["tokens"] else {},
                cut_after="3")
            + '</td></tr>')
    body.append('</tbody></table></div>')

    gloss = cy["meaning"] or cy["literal"] or "a Chinese four-character idiom"
    return page(origin, f"/chengyu/{quote(word)}",
                f"{word} — the chengyu in Mandarin song lyrics",
                f"{word}: {gloss}. Used in {len(rows)} of the "
                f"{n_songs:,} Mandarin songs analysed, listed easiest first "
                f"with the HSK level you need for each.",
                "".join(body), v)


def song_html(origin, artist_slug, song_slug, v):
    """One song's difficulty profile. Counts only -- never the lyrics."""
    d = data()
    a = d["by_slug"].get(artist_slug)
    if not a:
        return None
    s = next((x for x in a["songs"] if x["slug"] == song_slug), None)
    if not s:
        return None

    zh = s["tokens"]
    body = [
        '<div class="panel">',
        f'<p class="muted" style="margin:0 0 .3rem"><a href="/artist/'
        f'{e(a["slug"])}">← {e(full_name(a))}</a></p>',
        f'<h2>{e(s["title"])}</h2>',
        f'<p>{s["easy"]:.0f}% of this song\'s words are HSK 1-3. '
        f'{s["unique"]} distinct words across {zh} words of Chinese.</p>',
        _legend(), _bar({lv: 100.0 * s["levels"].get(lv, 0) / zh
                         for lv in LEVELS + (BEYOND,)}),
        '<table class="kv"><tbody>',
    ]
    for lv in ("2", "3", "4", "5", "6"):
        # data-forlevel lets the top-bar slider highlight the reader's own row
        # instead of making them find it in a list of five.
        body.append(f'<tr data-forlevel="{lv}"><th>If you are at HSK {lv}</th>'
                    f'<td>you already know about '
                    f'<b>{s["cov"][lv]*100:.0f}%</b> of the words</td></tr>')
    body.append('</tbody></table></div>')

    if s["hard"]:
        body.append('<div class="panel"><h2>The words that will slow you down</h2>'
                    '<p class="muted">HSK 7-9 or outside the lists entirely, most '
                    'frequent first.</p><p>' + " ".join(
                        f'<span class="tok hard">{e(w)}</span>'
                        f'<span class="muted" style="font-size:.8rem">'
                        f'&nbsp;{c}&times;&nbsp;&nbsp;</span>'
                        for c, w in s["hard"]) + '</p></div>')

    if s["idioms"]:
        body.append(_chengyu_panel(s["idioms"], d))

    if s["grammar"]:
        body.append('<div class="panel"><h2>Grammar in this song</h2>'
                    '<table class="kv"><tbody>' + "".join(
                        f'<tr><th>{e(k)}</th><td>{c}&times;</td></tr>'
                        for k, c in s["grammar"]) + '</tbody></table></div>')

    # The conversion point: a visitor who knows this song can paste the lyrics
    # and get the coloured view, and it lands in their own saved songs.
    body.append(
        '<div class="panel"><h2>See the lyrics colour-coded</h2>'
        '<p>We do not store lyric text, so this page shows counts only. Paste '
        f'the words to <b>{e(s["title"])}</b> and you get every word tinted by '
        'difficulty, its HSK level, an English gloss, and the vocabulary worth '
        'learning first — scored against your own level.</p>'
        f'<p><a class="btnlink" href="/?artist={quote(a["name"])}'
        f'&title={quote(s["title"])}">Paste the lyrics →</a></p></div>')

    return page(origin, f"/song/{artist_slug}/{song_slug}",
                f"{s['title']} — {a['name']}: HSK difficulty",
                f"{s['title']} by {a['name']}: {s['easy']:.0f}% of its words are "
                f"HSK 1-3, {s['unique']} distinct words. Which HSK level you "
                f"need, and the vocabulary to learn first.",
                "".join(body), v)


def slugs():
    return [a["slug"] for a in data()["artists"]]


def song_paths():
    return [f"/song/{a['slug']}/{s['slug']}"
            for a in data()["artists"] for s in a["songs"]]


SITEMAP_MIN_SONGS = 3


def chengyu_paths():
    """Chengyu pages worth submitting to a crawler.

    Only those used by at least a few songs. A page listing one song is a
    thinner version of that song's own page, and filling a sitemap with
    hundreds of them is how a small site trains a crawler to ignore it.
    """
    return [f"/chengyu/{w}" for w, rows in (data().get("idiom_songs") or {}).items()
            if len(rows) >= SITEMAP_MIN_SONGS]


def song_path_for(artist, title):
    """Public song-page path for a free-text (artist, title), or None.

    Only returns a path when that exact song is in the public corpus, so a
    user's own unseeded song renders as plain text instead of a dead link.
    """
    slug = slug_for(artist)
    if not slug or not title:
        return None
    a = data()["by_slug"].get(slug)
    if not a:
        return None
    want = artistlib.alias_key(title)
    for s in a["songs"]:
        if artistlib.alias_key(s["title"]) == want:
            return f"/song/{slug}/{s['slug']}"
    return None


def slug_for(artist):
    """Public page slug for a free-text artist string, or None.

    Resolved server-side because the lookup key depends on artists.alias_key,
    which needs OpenCC to fold traditional to simplified -- a JS reimplementation
    would silently drift and quietly stop linking 王菲 typed as 王菲.

    Returns None whenever we have no public page, so the caller renders plain
    text rather than a link to a 404.
    """
    if not artist:
        return None
    d = data()
    key = artistlib.alias_key(artist)
    if not key:
        return None
    return d["slug_by_key"].get(key)
