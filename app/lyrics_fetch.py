"""Transient lyrics sourcing for text-free difficulty analysis.

Fetches lyrics for (artist, title) from Kugou (primary) / NetEase (fallback),
returns cleaned plain text to be analyzed AND DISCARDED — callers must never
store the text (see analyze.strip_text). Ported from the proven offline
importer (fetch2.py): a match is only accepted when the candidate's title
matches exactly after normalization and the singer matches the expected
artist; near-misses return None rather than another song's lyrics."""
import base64
import json
import re
import time
import urllib.parse
import urllib.request

from . import normalize

SLEEP = 0.4

UA_M = "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/120 Mobile"
NE_HDRS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120",
    "Referer": "https://music.163.com/",
    "Cookie": "appver=2.0.2",
    "Content-Type": "application/x-www-form-urlencoded",
}

# Romanized/stage name as playlist exports carry it -> names the music
# services actually index the artist under. Compared normalized, lowercase.
ARTIST_ALIASES = {
    "ronghao li": ["李荣浩"], "jay chou": ["周杰伦"], "mao buyi": ["毛不易"],
    "weibird": ["韦礼安"], "wei bird": ["韦礼安"], "eric chou": ["周兴哲"],
    "crowd lu": ["卢广仲"], "stefanie sun": ["孙燕姿"], "khalil fong": ["方大同"],
    "wu bai": ["伍佰"], "a-mei chang": ["张惠妹", "阿密特"], "ashin chen": ["陈信宏", "阿信"],
    "jj lin": ["林俊杰"], "jolin tsai": ["蔡依林"], "mayday": ["五月天"],
    "eason chan": ["陈奕迅"], "g.e.m.": ["邓紫棋"], "gem": ["邓紫棋"],
    "hebe tien": ["田馥甄"], "leehom wang": ["王力宏"], "david tao": ["陶喆"],
    "faye wong": ["王菲"], "teresa teng": ["邓丽君"], "cyndi wang": ["王心凌"],
    "rainie yang": ["杨丞琳"], "angela chang": ["张韶涵"], "fish leong": ["梁静茹"],
    "silence wang": ["汪苏泷"], "joker xue": ["薛之谦"], "accusefive": ["告五人"],
    "no party for cao dong": ["草东没有派对"], "sodagreen": ["苏打绿"],
    "f.i.r.": ["飞儿乐团"], "s.h.e": ["s.h.e"], "yoga lin": ["林宥嘉"],
    "lala hsu": ["徐佳莹"], "waa wei": ["魏如萱"], "karencici": ["karencici"],
    "hush": ["hush"], "deca joins": ["deca joins"],
}

VARIANT_RE = re.compile(r"(live|dj版|伴奏|纯音乐|remix|cover|翻唱|钢琴|instrumental|原唱)", re.I)


def norm(s):
    s = normalize.to_simplified(s or "").lower()
    return re.sub(r"[^0-9a-z一-鿿]", "", s)


def core_title(title):
    """Strip promo suffixes: ' - 電影…主題曲', '(Demo版)', '(feat. X)'."""
    t = title.split(" - ")[0]
    t = re.sub(r"[\(（【\[].*?[\)）】\]]", " ", t)
    return re.sub(r"\s+", " ", t).strip() or title.split(" - ")[0].strip()


def artist_targets(artist):
    primary = re.split(r"[,&/]| feat\.?| with ", artist, flags=re.I)[0].strip()
    out = [norm(primary)] if primary else []
    out += [norm(a) for a in ARTIST_ALIASES.get(primary.lower(), [])]
    return [x for x in out if x]


def singer_ok(artist, singer):
    """Accept when any expected-artist target equals or prefixes a singer part
    ('地磁卡Dizkar' glues the bilingual name) — prefix only, never substring."""
    tgts = artist_targets(artist)
    if not tgts:
        return True  # no artist given: title match must carry it
    parts = [norm(p) for p in re.split(r"[、,&/]", singer or "") if norm(p)]
    return any(p == t or p.startswith(t) for t in tgts for p in parts)


def title_ok(ct, name):
    a, b = norm(ct), norm(name)
    if a == b:
        return True
    if re.search(r"[一-鿿]", ct) and b.startswith(a):
        rest = b[len(a):]
        return rest.isascii() and 0 < len(rest) <= 24
    return False


def _get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA_M})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------- Kugou ----------------

def kg_search(kw, n=8):
    u = ("http://mobilecdn.kugou.com/api/v3/search/song?format=json&keyword="
         + urllib.parse.quote(kw) + f"&page=1&pagesize={n}&showtype=1")
    try:
        return (_get(u).get("data") or {}).get("info") or []
    except Exception:
        return []


def kg_lyric(hash_):
    """Several uploaded LRCs may exist per track and candidates[0] is not
    always good — score a few and keep the fullest."""
    try:
        cands = _get("http://krcs.kugou.com/search?ver=1&man=yes&client=mobi&hash="
                     + hash_).get("candidates") or []
    except Exception:
        return None
    best, best_score = None, -1
    for c in cands[:4]:
        try:
            d = _get(f"http://lyrics.kugou.com/download?ver=1&client=pc&id={c['id']}"
                     f"&accesskey={c['accesskey']}&fmt=lrc&charset=utf8")
            if not d.get("content"):
                continue
            lrc = base64.b64decode(d["content"]).decode("utf-8", "replace")
        except Exception:
            continue
        lines = {l for l in lrc_to_text(lrc, "", "").split("\n") if l.strip()}
        if len(lines) > best_score:
            best, best_score = lrc, len(lines)
    return best


# ---------------- NetEase (fallback) ----------------

def ne_search(kw, n=8):
    try:
        data = urllib.parse.urlencode({"s": kw, "type": 1, "offset": 0, "limit": n}).encode()
        req = urllib.request.Request("https://music.163.com/api/search/get/",
                                     data=data, headers=NE_HDRS, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        return (j.get("result") or {}).get("songs") or []
    except Exception:
        return []


def ne_lyric(sid):
    try:
        r = _get(f"https://music.163.com/api/song/lyric?id={sid}&lv=1&kv=1&tv=-1", NE_HDRS)
        if r.get("nolyric") or r.get("uncollected"):
            return None
        return ((r.get("lrc") or {}).get("lyric") or "").strip() or None
    except Exception:
        return None


# ---------------- LRC cleanup ----------------

META_RE = re.compile(r"^\s*\[[a-z]{2,}:.*?\]\s*$", re.I)
TS_RE = re.compile(r"\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]")
CREDIT_WORDS = ("吉他", "贝斯", "鼓", "和声", "录音", "编曲", "作词", "作曲", "制作",
                "监制", "混音", "母带", "弦乐", "键盘", "配唱", "统筹", "出品", "发行",
                "推广", "营销", "企划", "宣发", "策划", "演唱", "词", "曲", "唱片",
                "企宣", "后期", "工程师", "工作室", "词曲", "鸣谢", "特别感谢",
                "总监", "宣传", "封面", "版权", "人声", "编辑", "演奏", "主唱", "合音",
                "萨克斯", "钢琴", "提琴", "小号", "长号", "合成器", "打击乐", "口琴",
                "手风琴", "铜管", "木管", "笛", "琵琶", "二胡", "古筝",
                "producer", "mixing", "mastering", "lyricist", "composer",
                "arrangement", "vocal", "guitar", "bass", "drums", "mix",
                "recording", "arranger", "studio", "engineer", "saxophone",
                "piano", "strings", "keyboard", "edited", "performed")
_COLON_RE = re.compile(r"^\s*([^:：\n]{0,40})[:：]")
PROMO_RE = re.compile(
    r"(未经[^\n]{0,6}(许可|授权)|不得翻唱|不得翻录|禁止翻唱|版权所有|版权方|"
    r"音乐版权|保留所有权利|All Rights Reserved|发行[:：]|出品[:：]|推广[:：]?\s|"
    r"营销[:：]|企划[:：]|宣发|词曲版权|独家发行|网易云音乐|QQ音乐|酷狗音乐|酷我音乐)", re.I)


def is_credit(line):
    m = _COLON_RE.match(line)
    return bool(m and any(k in m.group(1).lower() for k in CREDIT_WORDS))


def _artist_tokens(artist):
    n = norm(artist)
    toks = [n] if len(n) >= 2 else []
    han = "".join(re.findall(r"[一-鿿]+", n))
    lat = "".join(re.findall(r"[a-z]+", n))
    toks += [t for t in (han, lat) if len(t) >= 2]
    return toks


def lrc_to_text(lrc, artist, title):
    na, nt = norm(artist), norm(title)
    atoks = _artist_tokens(artist)
    out = []
    for raw in lrc.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.lstrip("﻿")
        if META_RE.match(line):
            continue
        line = TS_RE.sub("", line).strip()
        if not line or is_credit(line) or PROMO_RE.search(line):
            continue
        if len(out) < 2 and any(t in norm(line) for t in atoks):
            continue
        if len(out) < 3 and na and nt and na in norm(line) and nt in norm(line):
            continue
        out.append(line)
    return "\n".join(l for i, l in enumerate(out) if i == 0 or l != out[i - 1]).strip()


def nlines(text):
    return len({l.strip() for l in text.split("\n") if l.strip()})


def han_ratio(text):
    ch = [c for c in text if not c.isspace()]
    return sum(1 for c in ch if "一" <= c <= "鿿") / len(ch) if ch else 0.0


# ---------------- resolution ----------------

def resolve_text(artist, title):
    """Best confident lyrics for the song as cleaned plain text, or None."""
    ct = core_title(title)
    ct_s = normalize.to_simplified(ct)
    primary = re.split(r"[,&/]", artist)[0].strip()
    aliases = ARTIST_ALIASES.get(primary.lower(), [])
    zh = aliases[0] if aliases else primary

    matches, seen = [], set()
    for q in (f"{ct_s} {zh}".strip(), f"{ct_s} {primary}".strip(), ct_s):
        for c in kg_search(q):
            name, singer = c.get("songname", ""), c.get("singername", "")
            if c["hash"] in seen or not title_ok(ct, name) or not singer_ok(artist, singer):
                continue
            if VARIANT_RE.search(name) and not VARIANT_RE.search(ct):
                continue
            seen.add(c["hash"])
            matches.append(c)
        if len(matches) >= 3:
            break
        time.sleep(SLEEP)

    kg = None
    for c in matches[:3]:
        lrc = kg_lyric(c["hash"])
        if not lrc:
            continue
        text = lrc_to_text(lrc, c["singername"], c["songname"])
        n = nlines(text)
        if kg is None or n > kg[0]:
            kg = (n, text)

    ne = None
    for q in (f"{ct_s} {zh}".strip(), ct_s):
        for c in ne_search(q):
            name = c.get("name", "")
            singers = "、".join(a.get("name", "") for a in (c.get("artists") or []))
            if not title_ok(ct, name) or not singer_ok(artist, singers):
                continue
            if len(c.get("artists") or []) > 1 and not re.search(r"[,&]|feat", artist, re.I):
                continue
            lrc = ne_lyric(c["id"])
            if lrc:
                ne = (nlines(lrc_to_text(lrc, singers, name)),
                      lrc_to_text(lrc, singers, name))
                break
        if ne:
            break
        time.sleep(SLEEP)

    # Prefer Kugou; take the clearly fuller source when they disagree materially
    best = None
    if kg and ne:
        best = ne[1] if ne[0] > kg[0] * 1.25 else kg[1]
    elif kg:
        best = kg[1]
    elif ne:
        best = ne[1]
    if not best or nlines(best) < 4 or han_ratio(best) < 0.3:
        return None
    return best
