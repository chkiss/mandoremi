"""HSK 3.0 vocabulary, idiom lexicon, and normalization ruleset."""
import json
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# Level codes used throughout the app (and in stored analyses):
# 1-6 = HSK 1-6, 7 = HSK 7-9, 8 = beyond HSK (Chinese, not in lists),
# 9 = unknown (non-Chinese), 0 = filler (excluded from stats)
LEVEL_BEYOND = 8
LEVEL_UNKNOWN = 9
LEVEL_FILLER = 0

LEVEL_FILES = [("1", 1), ("2", 2), ("3", 3), ("4", 4), ("5", 5), ("6", 6), ("7-9", 7)]

_cache = {}


def _load_lines(path):
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def hsk_dict():
    """word -> level (lowest level wins)."""
    if "hsk" not in _cache:
        d = {}
        for suffix, level in LEVEL_FILES:
            for w in _load_lines(os.path.join(DATA_DIR, f"hsk-{suffix}.txt")):
                if w not in d:
                    d[w] = level
        _cache["hsk"] = d
    return _cache["hsk"]


def idiom_set():
    if "idioms" not in _cache:
        s = set()
        for ln in _load_lines(os.path.join(DATA_DIR, "chengyu.txt")):
            w = ln.split()[0].strip()
            if w:
                s.add(w)
        _cache["idioms"] = s
    return _cache["idioms"]


def fillers():
    if "fillers" not in _cache:
        _cache["fillers"] = set(_load_lines(os.path.join(DATA_DIR, "fillers.txt")))
    return _cache["fillers"]


def normalize_rules():
    if "rules" not in _cache:
        with open(os.path.join(DATA_DIR, "normalize_rules.json"), encoding="utf-8") as f:
            _cache["rules"] = json.load(f)
    return _cache["rules"]


def config():
    if "config" not in _cache:
        with open(os.path.join(DATA_DIR, "config.json"), encoding="utf-8") as f:
            _cache["config"] = json.load(f)
    return _cache["config"]


def grammar_patterns():
    if "grammar" not in _cache:
        with open(os.path.join(DATA_DIR, "grammar_patterns.json"), encoding="utf-8") as f:
            _cache["grammar"] = json.load(f)
    return _cache["grammar"]


def user_dict_path():
    """Lexicon file biasing the segmenter toward HSK words + idioms (longest match)."""
    path = os.path.join(DATA_DIR, "user_dict.txt")
    if not os.path.exists(path):
        words = set(hsk_dict()) | idiom_set()
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(words)))
    return path


def normalize_token(token):
    """Longest-match vocabulary normalization: 爱的→爱, 想着→想, etc.
    Returns the dictionary form to use for HSK lookup."""
    d = hsk_dict()
    rules = normalize_rules()
    if token in rules.get("overrides", {}):
        return rules["overrides"][token]
    if token in d or token in idiom_set():
        return token
    for suf in rules.get("strip_suffixes", []):
        if len(token) > len(suf) and token.endswith(suf):
            stripped = token[: -len(suf)]
            if stripped in d:
                return stripped
    # longest dictionary prefix of length >= 2 (e.g. 朋友们 -> 朋友)
    for end in range(len(token) - 1, 1, -1):
        if token[:end] in d:
            return token[:end]
    return token


def classify(token):
    """Return (normalized_token, level_code)."""
    if token in fillers():
        return token, LEVEL_FILLER
    if not any("一" <= ch <= "鿿" for ch in token):
        return token, LEVEL_UNKNOWN
    norm = normalize_token(token)
    level = hsk_dict().get(norm)
    if level is None and norm not in idiom_set() and token not in idiom_set():
        level = _decompose_level(norm)
    if level is None:
        return norm, LEVEL_BEYOND
    return norm, level


_decompose_cache = {}


def _decompose_level(word):
    """The segmenter emits compounds that no HSK list carries as one word
    (不是, 这就是, 没说…). If the token splits entirely into HSK words, its
    difficulty is that of its hardest part — not "beyond HSK". Returns the
    minimal achievable max-part level, or None if it doesn't fully decompose.
    Idioms are excluded by the caller: their meaning isn't compositional."""
    if word in _decompose_cache:
        return _decompose_cache[word]
    d = hsk_dict()
    n = len(word)
    result = None
    if 2 <= n <= 6 and all("一" <= ch <= "鿿" for ch in word):
        # DP over split points, minimizing the hardest part
        best = [None] * (n + 1)
        best[0] = 0
        for i in range(1, n + 1):
            for j in range(max(0, i - 4), i):
                if best[j] is None:
                    continue
                lvl = d.get(word[j:i])
                if lvl is None:
                    continue
                cand = max(best[j], lvl)
                if best[i] is None or cand < best[i]:
                    best[i] = cand
        result = best[n]
    if len(_decompose_cache) > 50000:
        _decompose_cache.clear()
    _decompose_cache[word] = result
    return result
