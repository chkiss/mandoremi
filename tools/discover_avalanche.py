#!/usr/bin/env python3
"""Artists from Concrete Avalanche (jakenewby.substack.com), a newsletter on
non-pop music from China.

Usage:  discover_avalanche.py scrape     -> avalanche_raw.json
        discover_avalanche.py resolve    -> avalanche_manifest.json

Then `discover_deep.py avalanche` consumes the manifest exactly like `indie`.

WHY TWO STAGES
--------------
`scrape` only talks to Substack; `resolve` only talks to NetEase. Keeping them
apart means re-running the name extraction costs nothing at the music APIs, and
a NetEase outage never forces a re-scrape.

WHY THE LYRIC GATE (the reason this source needs one)
-----------------------------------------------------
The newsletter's editorial line is "non-pop music from China", which sweeps in
instrumental post-rock, ambient, noise, and Mongolian- and Kazakh-language acts.
Measured on a 10-artist / 60-song sample, only 27 songs carried substantial
Chinese lyrics -- but the split is by ARTIST, not by song:

    动物园钉子户 6/6   小老虎 6/6   大梦 5/6   缺省 5/6   杨海崧 3/6
    惘闻 0/6   花伦 0/6   李剑鸿 0/6   沼泽 1/6   胡格吉乐图 1/6

So `resolve` spends ~5 cheap requests per artist checking whether the artist can
produce Chinese text at all, before seed_corpus.py spends ~8.7s per song on one
who can't. Rejects are recorded as "low lyric yield", not deleted: an
instrumental act's next release may have vocals, and a re-run re-checks them.

NAMES
-----
Substack's Bandcamp embed metadata carries the artist in the publisher's own
spelling -- "Hualun（花伦）", "马木尔 Mamer" -- so both halves are kept and handed
to search_artist() as aliases. That matters here: underground acts are exactly
where NetEase serves 0-song romanized stub pages.

Discovery only: writes JSON, touches no database.
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

PUB = "https://jakenewby.substack.com"
CACHE = os.path.expanduser("~/avalanche_cache")
RAW = os.path.expanduser("~/avalanche_raw.json")
MANIFEST = os.path.expanduser("~/avalanche_manifest.json")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HDRS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

# Lyric gate.
GATE_SONGS = 5          # hot songs sampled per artist
GATE_MIN_HITS = 2       # ...of which this many must carry Chinese text
GATE_MIN_HANZI = 80     # characters, after stripping [mm:ss] timestamps

# Channels that upload other people's music: the channel name is not an artist.
NOT_ARTISTS = {
    "boiler room", "vice", "vice asia", "npr music", "kexp", "colors",
    "various artists", "va", "nts radio", "resident advisor", "the wire",
    "bandcamp", "topic", "youtube", "spotify",
}
# Bracketed second spelling: "Hualun（花伦）", "Pocari Sweet (波卡利甜)".
BRACKET_RE = re.compile(r"[（(]([^（()）]{1,40})[)）]")
# "竇唯 & 朝簡", "X feat. Y", "A、B" -- the first billing is the artist to seed.
COLLAB_RE = re.compile(r"\s*(?:&|\+|,|、|/|\bfeat\.?|\bft\.?|\bwith\b|\bx\b)\s+", re.I)
HAN_RE = re.compile(r"[一-鿿]")
TS_RE = re.compile(r"\[[^\]]*\]")


def get(url, tries=3, sleep=0.5):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=HDRS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:                               # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(sleep * (i + 1))


def get_json(url, **kw):
    return json.loads(get(url, **kw))


# --------------------------------------------------------------------------
# scrape
# --------------------------------------------------------------------------

def archive():
    """Every post in the publication. All 82 are audience=everyone; a future
    paywalled one simply yields no body and drops out."""
    posts, offset = [], 0
    while True:
        batch = get_json(f"{PUB}/api/v1/archive?sort=new&limit=50&offset={offset}")
        if not batch:
            break
        posts += batch
        offset += len(batch)
        time.sleep(0.5)
    return {p["id"]: p for p in posts}.values()


def post_html(slug):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{slug}.html")
    if os.path.exists(path) and os.path.getsize(path) > 20000:
        return open(path, encoding="utf-8", errors="replace").read()
    body = get(f"{PUB}/p/{urllib.parse.quote(slug)}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    time.sleep(0.7)
    return body


def embeds(body, component):
    """Substack renders every embed as data-attrs="<json>" data-component-name=X."""
    out = []
    for m in re.finditer(r'data-attrs="(.*?)" data-component-name="%s"' % component,
                         body, re.S):
        try:
            out.append(json.loads(html.unescape(m.group(1))))
        except ValueError:
            pass
    return out


def split_name(raw):
    """'Hualun（花伦）' -> ('花伦', ['Hualun', '花伦']); prefers the Han spelling
    as the primary, because NetEase indexes those and not the romanisation."""
    raw = re.sub(r"\s+", " ", (raw or "").strip())
    parts = [raw]
    bracketed = []
    for b in BRACKET_RE.findall(raw):
        parts.append(b.strip())
        bracketed.append(b.strip())
        raw = raw.replace(f"（{b}）", " ").replace(f"({b})", " ")
    bare = re.sub(r"\s+", " ", raw).strip()
    bare = re.sub(r"\s*(&|and)\s+Various Artists\s*$", "", bare, flags=re.I).strip()
    if bare:
        parts.append(bare)
    # "马木尔 Mamer" -> the Han run and the latin run are both usable names.
    # Split on the SCRIPT boundary only, never on whitespace: splitting
    # "Carsick Cars" into "Carsick" and "Cars" hands search_artist a one-word
    # query that will happily match an unrelated band.
    # Collaborations bill several acts; seed the primary one, as everywhere else
    # in this project. The others stay in `aliases` so a later pass can find them.
    billed = [b.strip() for b in COLLAB_RE.split(bare) if b.strip()]
    lead = billed[0] if billed else bare
    parts += billed
    han_run = " ".join(re.findall(r"[一-鿿·]+", lead)).strip()
    lat_run = " ".join(re.findall(r"[A-Za-z0-9'’.\-]+", lead)).strip()
    if han_run and lat_run:
        parts += [han_run, lat_run]

    aliases, seen = [], set()
    for p in parts:
        p = p.strip()
        if len(p) > 1 and re.search(r"[一-鿿A-Za-z0-9]", p) and p.lower() not in seen:
            seen.add(p.lower())
            aliases.append(p)
    if not aliases:
        return None, []             # punctuation-only or single-character billing
    # The primary comes from the LEAD billing only. Scanning every alias would
    # pick a Han-named collaborator over a latin-named lead: "Howie Lee feat.
    # 老丹" would seed 老丹.
    # "Hualun（花伦）" carries its Han name only in the bracket.
    han_opts = [h for h in [han_run] + bracketed if h and HAN_RE.search(h)]
    primary = min(han_opts, key=len) if han_opts else (
        lead if len(lead) > 1 else aliases[0])
    return primary, aliases


def youtube_artist(vid):
    """oEmbed gives channel + video title. '<name> - Topic' is YouTube's
    auto-generated artist channel and is the one fully trustworthy case."""
    try:
        d = get_json("https://www.youtube.com/oembed?" + urllib.parse.urlencode(
            {"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}), tries=2)
    except Exception:                                   # noqa: BLE001
        return None
    chan = (d.get("author_name") or "").strip()
    title = (d.get("title") or "").strip()
    if chan.lower().endswith(" - topic"):
        return chan[:-len(" - topic")].strip(), title, "yt-topic"
    if chan.lower() in NOT_ARTISTS:
        return None
    left = title.split(" - ")[0].strip() if " - " in title else ""
    if left and left.lower() == chan.lower():
        return chan, title, "yt-title"
    if chan and chan.lower() in title.lower():
        return chan, title, "yt-channel"
    return None                                         # unattributable upload


def scrape():
    posts = list(archive())
    print(f"{len(posts)} posts", flush=True)
    found, labels, videos = {}, set(), []
    for n, p in enumerate(posts, 1):
        try:
            body = post_html(p["slug"])
        except Exception as e:                          # noqa: BLE001
            print(f"  ! {p['slug']}: {e}")
            continue
        for e in embeds(body, "BandcampToDOM"):
            title = e.get("title") or ""
            if ", by " not in title:
                continue
            release, raw = title.rsplit(", by ", 1)
            if raw.strip().lower() in NOT_ARTISTS:
                continue
            name, aliases = split_name(raw)
            if not name:
                continue
            rec = found.setdefault(name, {"name": name, "aliases": [], "releases": [],
                                          "posts": [], "srcs": []})
            rec["aliases"] = sorted(set(rec["aliases"]) | set(aliases))
            rec["releases"].append(release.strip())
            rec["srcs"].append("bandcamp")
            if p["slug"] not in rec["posts"]:
                rec["posts"].append(p["slug"])
            if e.get("author"):
                labels.add(e["author"].strip())
        for e in embeds(body, "Youtube2ToDOM"):
            if e.get("videoId"):
                videos.append((e["videoId"], p["slug"]))
        if n % 10 == 0:
            print(f"  {n}/{len(posts)} posts, {len(found)} artists", flush=True)

    seen_v = set()
    print(f"resolving {len(videos)} YouTube embeds", flush=True)
    for vid, slug in videos:
        if vid in seen_v:
            continue
        seen_v.add(vid)
        hit = youtube_artist(vid)
        time.sleep(0.3)
        if not hit:
            continue
        raw, title, src = hit
        name, aliases = split_name(raw)
        if not name:
            continue
        rec = found.setdefault(name, {"name": name, "aliases": [], "releases": [],
                                      "posts": [], "srcs": []})
        rec["aliases"] = sorted(set(rec["aliases"]) | set(aliases))
        rec["releases"].append(title)
        rec["srcs"].append(src)
        if slug not in rec["posts"]:
            rec["posts"].append(slug)

    # A name that only ever appeared as a Bandcamp page owner is a label.
    artists = [a for a in found.values() if a["name"] not in labels]
    dropped = sorted(set(found) & labels)
    for a in artists:
        a["mentions"] = len(a["releases"])
        a["srcs"] = sorted(set(a["srcs"]))
    artists.sort(key=lambda a: (-a["mentions"], a["name"]))
    out = {"source": PUB, "posts": len(posts), "artists": artists,
           "dropped_as_label": dropped, "labels": sorted(labels)}
    json.dump(out, open(RAW, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n{len(artists)} artists -> {RAW}  "
          f"({len(dropped)} dropped as labels, {len(labels)} labels seen)")


# --------------------------------------------------------------------------
# resolve
# --------------------------------------------------------------------------

def lyric_yield(artist_id):
    """(hits, sampled) over the artist's top hot songs. Cheap by design: this
    runs on every candidate, seeding runs only on survivors."""
    import discover_deep as dd

    hot = (dd.get(f"artist/{artist_id}") or {}).get("hotSongs") or []
    hits = 0
    sampled = hot[:GATE_SONGS]
    for s in sampled:
        try:
            lyr = dd.get(f"song/lyric?id={s['id']}&lv=1&kv=1&tv=-1")
        except Exception:                               # noqa: BLE001
            continue
        text = TS_RE.sub("", ((lyr.get("lrc") or {}).get("lyric") or ""))
        if len(HAN_RE.findall(text)) > GATE_MIN_HANZI:
            hits += 1
        time.sleep(0.3)
    return hits, len(sampled)


def dedupe_by_id(records):
    """Two spellings can resolve to one artist -- 马木尔 and 馬木爾 both land on
    马木尔Mamer. Seeding that twice is wasted sourcing time, so merge on the
    NetEase id and keep every spelling that led there."""
    out = {}
    for r in records:
        prior = out.get(r["netease_id"])
        if prior is None:
            out[r["netease_id"]] = dict(r)
            continue
        prior["mentions"] += r["mentions"]
        prior["aliases"] = sorted(set(prior["aliases"]) | set(r["aliases"]))
        prior["posts"] = sorted(set(prior["posts"]) | set(r["posts"]))
    return list(out.values())


def resolve():
    import discover_deep as dd

    raw = json.load(open(RAW, encoding="utf-8"))
    keep, low, unresolved = [], [], []
    total = len(raw["artists"])
    for n, a in enumerate(raw["artists"], 1):
        if n % 10 == 0:                 # before the early-continues, or an
            print(f"  {n}/{total}  keep={len(keep)} low={len(low)} "   # unresolved
                  f"unresolved={len(unresolved)}", flush=True)         # artist
        try:                                                           # eats the line
            hit = dd.search_artist(a["name"], tuple(a["aliases"]))
        except Exception as e:                          # noqa: BLE001
            unresolved.append({"name": a["name"], "why": str(e)})
            continue
        time.sleep(0.4)
        if not hit:
            unresolved.append({"name": a["name"], "why": "no NetEase match"})
            continue
        ne_name, ne_id = hit
        hits, sampled = lyric_yield(ne_id)
        rec = {"netease_name": ne_name, "netease_id": ne_id,
               "avalanche_name": a["name"], "aliases": a["aliases"],
               "mentions": a["mentions"], "posts": a["posts"],
               "lyric_hits": hits, "lyric_sampled": sampled}
        (keep if hits >= GATE_MIN_HITS else low).append(rec)

    keep = dedupe_by_id(keep)
    low = dedupe_by_id(low)
    keep.sort(key=lambda a: (-a["lyric_hits"], -a["mentions"]))
    out = {"source": PUB,
           "gate": {"songs": GATE_SONGS, "min_hits": GATE_MIN_HITS,
                    "min_hanzi": GATE_MIN_HANZI},
           "artists": keep,
           "low_lyric_yield": sorted(low, key=lambda a: -a["mentions"]),
           "unresolved": unresolved}
    json.dump(out, open(MANIFEST, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n{len(keep)} artists pass the lyric gate -> {MANIFEST}")
    print(f"{len(low)} low lyric yield (kept for re-check), "
          f"{len(unresolved)} unresolved")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scrape"
    if cmd == "scrape":
        scrape()
    elif cmd == "resolve":
        resolve()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
