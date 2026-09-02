"""Canonical artist identity.

The same artist reaches us under many strings, because playlist imports carry
whatever the source platform used: Spotify says "Ronghao Li", NetEase says
李荣浩, a user might type 李榮浩. Resolving all of them to one canonical id is
what lets the shared seed corpus hit regardless of which name was typed.

Resolution is cached in the artist_alias table, so it is paid once per string
across all users. In a request path we only ever read that cache
(``allow_network=False``); the network lookup belongs to the offline sweep in
tools/resolve_artists.py, so importing a 100-track playlist never blocks on
100 third-party calls.

Matching is deliberately conservative -- a search hit is not an identity. Three
traps found while seeding, each of which this guards against:
  * romanized STUB pages that match a name exactly but hold zero songs
    ("A-Mei Chang", "Mao Buyi") -- we prefer the candidate with a real catalog
  * suffixed names (舒大卫 vs 舒大卫Dizzy Boy, 小老虎J-Fever vs 小老虎)
  * unrelated acts sharing a name (a band literally called "The Chairs")
"""
import json
import re
import urllib.parse
import urllib.request

from . import normalize

# Full-width forms matter: NetEase writes "hush！" with U+FF01, and without it
# here that artist keys as 'hush！' and never matches the 'hush' we resolved.
_PUNCT_RE = re.compile(r"[\s'’\-_.,!?()（）\[\]【】·・~～\"“”"
                       r"！？。：；　]+")
# Full-width comma and slash are collaboration separators, so they must be
# split on BEFORE _PUNCT_RE strips them -- otherwise "李荣浩，陈坤" keys as one
# glued artist, which is the exact bug this module exists to prevent.
_SPLIT_RE = re.compile(r"[,、&/，／＆]|\bfeat\.?\b|\bft\.?\b", re.I)

API = "https://music.163.com/api"
_HDRS = {
    "Cookie": "appver=2.0.2",
    "Referer": "https://music.163.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
}

# Artists whose canonical page shares no name with the string users supply, so
# no rule can bridge them. Each verified by hand against the live catalog.
ID_OVERRIDES = {
    "finn liu": 4469,        # 刘凤瑶 — no latin alias published
    "a-mei chang": 10559,    # 张惠妹 (aMEI); the literal page is a 0-song stub
}


def primary(artist):
    """First credited artist: 'Jay Chou, Gary Yang' -> 'Jay Chou'."""
    parts = [p.strip() for p in _SPLIT_RE.split(artist or "") if p.strip()]
    return _first_credit(parts[0]) if parts else ""


# Some sources join collaborators with a bare space instead of a separator, so
# "Ronghao Li 陈坤" arrives as one string and keys as 'ronghaoli陈坤' -- a
# nonexistent artist with its own orphaned corner of the corpus.
#
# We cannot simply split on a Han/latin boundary: 李大奔BENZO, 舒大卫Dizzy Boy
# and 艾志恒Asen are single artists whose own names mix scripts. The signal that
# separates the two cases is the SPACE. A collaborator boundary has one
# ("Ronghao Li 陈坤", "队长 郑润泽"); a mixed-script stage name does not.
#
# Only a space introducing a CJK run counts, because a sequence of latin words
# ("No Party For Cao Dong") is indistinguishable from two latin artists, and
# guessing there would break far more names than it fixed.
#
# One more restriction, from "G.E.M. 邓紫棋" -- a single artist, latin stage
# name then a space then the Han name, which the rule above would have cut in
# half. So we only cut when what precedes the space is already itself a name:
# either it contains CJK (队长 郑润泽) or it is two or more latin words
# (Ronghao Li 陈坤). A lone latin token before a Han run reads as one artist
# writing their own name twice, so it stays whole.
_HAN_RE = re.compile(r"[一-鿿]")


# Words that follow a Han name without being an English name for it.
_NOT_A_NAME = {"official", "music", "records", "vevo", "dj", "feat", "ft",
               "topic", "channel", "studio", "band"}
# 理想混蛋Bestards, 李大奔BENZO, 弹壳Danko: one artist publishing both scripts,
# which NetEase stores as a single run. Also the spaced (法兹乐队 FAZI), the
# hyphenated (塞壬唱片-MSR) and the parenthesised (昨夜派对（L.N Party）) forms.
_DUAL_HAN_FIRST = re.compile(
    r"^\s*([一-鿿·]{2,}?)\s*[-–—]?\s*[（(]?\s*"
    r"([A-Za-z][A-Za-z0-9 .'&\-]*?)\s*[）)]?\s*$")
# Ghost (王琳凯) and G.E.M.邓紫棋: the same thing written the other way round,
# parenthesised or not.
_DUAL_LATIN_FIRST = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 .'&\-]*?)\s*[（(]\s*([一-鿿·]{2,})\s*[）)]\s*$")
_DUAL_LATIN_BARE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9.'\-]*?)\s*([一-鿿·]{2,})\s*$")


def split_dual_script(name):
    """('理想混蛋', 'Bestards') for one artist written in two scripts, else None.

    NetEase stores these as a single name so both scripts are findable, not
    because the act is called "理想混蛋Bestards". Splitting them lets the pages
    render "理想混蛋 (Bestards)" like every other artist, instead of a run-on.

    Deliberately conservative. It declines:
      * anything with a collaboration separator ("伍佰 & China Blue" is a band)
      * a latin part that is a label or role rather than a name (洛天依Official)
      * a latin part too short to be a name (DJ阿智)
    """
    name = (name or "").strip()
    if not name or _SPLIT_RE.search(name):
        return None
    for rx, order in ((_DUAL_HAN_FIRST, "han"),
                      (_DUAL_LATIN_FIRST, "latin"),
                      (_DUAL_LATIN_BARE, "latin")):
        m = rx.match(name)
        if not m:
            continue
        han, eng = (m.group(1), m.group(2)) if order == "han" \
            else (m.group(2), m.group(1))
        eng = eng.strip(" -–—")
        if len(eng) < 2 or eng.lower() in _NOT_A_NAME:
            return None
        if all(w.lower() in _NOT_A_NAME for w in eng.split()):
            return None
        return han, eng
    return None


def _first_credit(name):
    segs = name.split()
    if len(segs) < 2:
        return name
    for j in range(len(segs) - 1):
        if not _HAN_RE.match(segs[j + 1]):
            continue
        left = " ".join(segs[:j + 1])
        if _HAN_RE.search(left) or j >= 1:
            return left
    return name


def alias_key(artist):
    """Normalized lookup key: simplify Han, casefold, drop punctuation."""
    return _PUNCT_RE.sub("", normalize.to_simplified(primary(artist)).lower())


def latin_tokens(s):
    """{'ronghao','li'} — order-insensitive, so a Spotify 'Ronghao Li' matches
    NetEase's alias 'Li Ronghao'."""
    return frozenset(w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w)


# Han suffixes that decorate a band name without changing who it is.
_GENERIC_SUFFIX_RE = re.compile(r"(乐队|樂隊|乐团|樂團|组合|組合|三重奏|二重奏|四重奏|"
                                r"实验室|實驗室|工作室)")


def _han_related(cand_key, want_keys):
    """One name carrying a suffix the other lacks. Requires a 2+ character Han
    run so it cannot fire on incidental latin overlap."""
    for w in want_keys:
        if not w or not cand_key:
            continue
        short, long = (w, cand_key) if len(w) <= len(cand_key) else (cand_key, w)
        if len(short) < 2 or not long.startswith(short):
            continue
        if not re.search(r"[一-鿿]{2}", short):
            continue
        # The suffix must not be more Han name: '小老虎' + 'J-Fever' is one act
        # under a stage name, but '李志' + '洲' is a different person, as are
        # 张梦/张梦奇 and 水树/水树奈奈. Generic band words are the one Han
        # suffix that still means the same act.
        rest = _GENERIC_SUFFIX_RE.sub("", long[len(short):])
        if not re.search(r"[一-鿿]", rest):
            return True
    return False


def lookup(conn, artist):
    """Cached identity for an artist string, or None. Never touches network."""
    k = alias_key(artist)
    if not k:
        return None
    row = conn.execute(
        "SELECT artist_id, display FROM artist_alias WHERE alias_key = ?", (k,)).fetchone()
    return (row["artist_id"], row["display"]) if row else None


def remember(conn, artist, artist_id, display, confidence):
    k = alias_key(artist)
    if not k:
        return
    conn.execute(
        "INSERT OR REPLACE INTO artist_alias(alias_key, artist_id, display, confidence) "
        "VALUES (?,?,?,?)", (k, artist_id, display, confidence))


def search(artist, extra_names=(), timeout=20):
    """Resolve against NetEase. Returns (artist_id, display, confidence) or None.

    Network call — never use in a request path.
    """
    q = primary(artist)
    if not q:
        return None
    if q.lower().strip() in ID_OVERRIDES:
        aid = ID_OVERRIDES[q.lower().strip()]
        req = urllib.request.Request(f"{API}/artist/{aid}", headers=_HDRS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return aid, (d.get("artist") or {}).get("name", q), "override"

    want_norm = {alias_key(n) for n in (q, *extra_names) if n}
    want_lat = {latin_tokens(n) for n in (q, *extra_names) if latin_tokens(n)}

    body = urllib.parse.urlencode({"s": q, "type": 100, "limit": 8}).encode()
    req = urllib.request.Request(f"{API}/search/get", data=body, headers=_HDRS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))

    # Rank by HOW well each candidate matched, and only then by catalog size.
    # Catalog size alone picked the wrong artist: searching 野孩子 returns both
    # the real 野孩子 and 羽·泉, who merely list "野孩子" as an alias and carry a
    # bigger catalog. Tier 0 = the artist's own name matched, tier 1 = one of
    # their aliases or translated names, tier 2 = prefix-related.
    hits = []
    for a in (d.get("result") or {}).get("artists") or []:
        # Stub pages match a name perfectly and hold nothing. Drop them BEFORE
        # ranking: a stub sitting in tier 0 would shut out the real artist in a
        # lower tier and the whole query would resolve to nothing.
        if not (a.get("musicSize") or 0):
            continue
        own = [a.get("name", "")]
        alt = list(a.get("alias") or []) + list(a.get("transNames") or [])
        hit = None
        for names, tier in ((own, 0), (alt, 1)):
            for n in names:
                ck = alias_key(n)
                if ck in want_norm:
                    hit = (tier, "exact")
                    break
                if latin_tokens(n) and latin_tokens(n) in want_lat:
                    hit = (tier, "latin")
                    break
            if hit:
                break
        if not hit and any(_han_related(alias_key(n), want_norm) for n in own + alt):
            hit = (2, "han-prefix")
        if hit:
            hits.append((hit[0], a, hit[1]))
    if not hits:
        return None
    top = min(t for t, _, _ in hits)
    _, best, conf = max((h for h in hits if h[0] == top),
                        key=lambda h: (h[1].get("musicSize") or 0,
                                       h[1].get("albumSize") or 0))
    return best["id"], best.get("name", q), conf


def resolve(conn, artist, allow_network=False, extra_names=()):
    """Cached identity, optionally resolving and caching a miss."""
    hit = lookup(conn, artist)
    if hit or not allow_network:
        return hit
    try:
        found = search(artist, extra_names)
    except Exception:                                   # noqa: BLE001
        return None
    if not found:
        return None
    aid, display, conf = found
    remember(conn, artist, aid, display, conf)
    return aid, display
