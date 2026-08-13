"""CC-CEDICT glosses (simplified word -> short English), attached at serve
time only — stored analyses stay gloss-free."""
import gzip
import os
import re

PATH = os.path.join(os.path.dirname(__file__), "..", "data", "cedict.u8.gz")

_LINE = re.compile(r"^\S+ (\S+) \[([^\]]*)\] /(.*)/$")
_SKIP_PREFIXES = ("variant of ", "old variant of ", "used in ", "see ", "CL:")
_MAX_SENSES = 3
_MAX_LEN = 110

_glosses = {}
_pinyin = {}
_senses = {}
# word -> [(pinyin, [senses]), ...] in file order. Kept because a character's
# meaning depends on which reading a word uses: 觉 is "to feel" as jue2 and
# "a nap" as jiao4, and a literal breakdown that picks the wrong one is worse
# than no breakdown at all.
_entries = {}


def load():
    if _glosses:
        return
    raw = {}
    with gzip.open(PATH, "rt", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            m = _LINE.match(line.strip())
            if not m:
                continue
            simp, py, defs = m.groups()
            senses = [s for s in defs.split("/")
                      if s and not s.startswith(_SKIP_PREFIXES)]
            if senses:
                raw.setdefault(simp, []).extend(senses)
                _entries.setdefault(simp, []).append((py, senses))
    for simp, senses in raw.items():
        # A word can have several entries (pronunciations); surname-only
        # senses shouldn't crowd out the everyday meaning.
        senses.sort(key=lambda s: s.startswith("surname "))
        text = "; ".join(senses[:_MAX_SENSES])
        if len(text) > _MAX_LEN:
            text = text[:_MAX_LEN - 1].rstrip() + "…"
        _glosses[simp] = text
    _senses.update(raw)
    for simp, ents in _entries.items():
        # An uppercase reading is a surname or a place name. For 山 that entry
        # comes first in the file, which would make 千山万水 read "Shān".
        best = min(ents, key=lambda e: (e[0][:1].isupper(), ents.index(e)))
        _pinyin[simp] = best[0]


def gloss(word):
    return _glosses.get(word)


def senses(word):
    """Every CC-CEDICT sense, unjoined and untruncated. gloss() is the display
    form; this is for callers that need to sort senses themselves (the chengyu
    table splits 'lit.' from 'fig.')."""
    return _senses.get(word) or []


def senses_for_reading(word, reading):
    """The senses belonging to one specific pronunciation, e.g. 觉/jue2.

    `reading` is numbered CC-CEDICT pinyin, matched case-insensitively because
    a word's own entry lowercases a reading that the character's entry
    capitalises. Lowercase entries come first: 顾/gu4 is "to look after" and
    顾/Gu4 is "surname Gu", and only one of those belongs in a gloss. Returns
    None when no entry has that reading, so a caller can tell "we picked the
    right reading" apart from "we guessed".
    """
    want = (reading or "").lower().replace("u:", "v")
    hits = [(py, ss) for py, ss in _entries.get(word, ())
            if py.lower().replace("u:", "v") == want]
    if not hits:
        return None
    hits.sort(key=lambda e: e[0][:1].isupper())
    return [s for _py, ss in hits for s in ss]


def _compound_gloss(word):
    """Per-character fallback for segmenter compounds CC-CEDICT doesn't list
    as words (多难 -> 多 (many) + 难 (difficult))."""
    if not (2 <= len(word) <= 4) or not all("一" <= ch <= "鿿" for ch in word):
        return None
    parts = []
    for ch in word:
        g = _glosses.get(ch)
        if not g:
            return None
        first = g.split("; ")[0]
        if len(first) > 24:
            first = first[:23].rstrip() + "…"
        parts.append(f"{ch} ({first})")
    return " + ".join(parts)


def annotate(analysis):
    """Add an English gloss ("g") to each vocab entry that CC-CEDICT knows,
    falling back to a per-character breakdown for unlisted compounds.
    Call on the outgoing response only, after any personalize() overlay."""
    if analysis:
        for word, v in analysis["vocab"].items():
            g = _glosses.get(word) or _compound_gloss(word)
            if g:
                v["g"] = g
    return analysis


# --- pinyin -----------------------------------------------------------------
# CC-CEDICT writes tones as trailing digits (bu4 zhi1 bu4 jue2). Readers expect
# tone marks, and a chengyu table full of digits is a table people skip.

_VOWELS = "aoeiuvü"
_MARKS = {
    "a": "āáǎà", "o": "ōóǒò", "e": "ēéěè",
    "i": "īíǐì", "u": "ūúǔù", "ü": "ǖǘǚǜ",
}
_SYL = re.compile(r"^([a-zA-ZüÜ:]+)([1-5])$")


def _mark(syl, tone):
    """Place the tone mark where Hanyu Pinyin orthography puts it: on a or e
    if present, on the o of 'ou', otherwise on the last vowel."""
    syl = syl.replace("u:", "ü").replace("U:", "Ü").replace("v", "ü")
    if tone == 5:
        return syl
    low = syl.lower()
    idx = -1
    for want in ("a", "e"):
        if want in low:
            idx = low.index(want)
            break
    else:
        if "ou" in low:
            idx = low.index("ou")
        else:
            for i in range(len(low) - 1, -1, -1):
                if low[i] in _VOWELS:
                    idx = i
                    break
    if idx < 0:
        return syl
    marked = _MARKS[low[idx]][tone - 1]
    if syl[idx].isupper():
        marked = marked.upper()
    return syl[:idx] + marked + syl[idx + 1:]


def to_tone_marks(numbered):
    """'bu4 zhi1 bu4 jue2' -> 'bù zhī bù jué'. Anything that isn't a numbered
    syllable (punctuation, xx, latin) passes through untouched."""
    out = []
    for part in (numbered or "").split():
        m = _SYL.match(part)
        out.append(_mark(m.group(1), int(m.group(2))) if m else part)
    return " ".join(out)


def pinyin_raw(word):
    """Numbered CC-CEDICT pinyin, or None."""
    return _pinyin.get(word)


def pinyin(word):
    """Tone-marked reading for a whole word, or None if CC-CEDICT lacks it."""
    raw = _pinyin.get(word)
    return to_tone_marks(raw) if raw else None


def pinyin_chars_raw(word):
    """Numbered reading assembled character by character — the fallback for
    words with no entry of their own (日日夜夜, 清清楚楚). Correct for
    reduplication and for anything without a 多音字, a guess for the rest, so
    callers should prefer pinyin(). None if any character is unknown."""
    parts = []
    for ch in word:
        raw = _pinyin.get(ch)
        if not raw:
            return None
        parts.append(raw.split()[0])
    return " ".join(parts)
