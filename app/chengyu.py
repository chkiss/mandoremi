"""What a learner needs to see about a chengyu, assembled from CC-CEDICT.

Every reference that teaches chengyu well shows the same four things: the
characters, the reading, what the characters literally say, and what the phrase
actually means. The gap between the third and the fourth is the entire reason
chengyu are hard — 一无所有 is "one - not - that which - have", and no amount of
staring at those four glosses gets you to "flat broke". Showing only the meaning
hides why the phrase looks the way it does; showing only the characters is what
a paper dictionary already failed to do for the reader.

CC-CEDICT has a whole-word entry for 88% of the chengyu occurrences in the
corpus, and per-character readings for the remaining 12%. It supplies an
explicit "lit." rendering for only about one chengyu in six, so the literal
column is usually assembled here, character by character, using the reading the
chengyu itself uses — 觉 is "to feel" as jue2 and "a nap" as jiao4, and
不知不觉 wants the first.
"""
import re

from . import dictionary, hskdata

_CHAR_GLOSS_MAX = 18
_MEANING_MAX = 95

# Senses that are true but useless in a four-part literal gloss. A breakdown
# reading "石 abbr. for Shijiazhuang + 水 Shui ethnic group" is noise wearing the
# costume of a translation.
_JUNK = re.compile(
    r"^(surname |abbr\. for|\(bound form\)|used in |variant of |old variant|"
    r"see |sixty-fourth |one of the |the |a Chinese |Chinese surname|"
    r"used for its phonetic|used in |phonetic|[A-Za-z]+ surname)", re.I)
_PROPER = re.compile(r"^[A-Z][a-z]+(?: [A-Z][a-z]+)*$")
_PARENS = re.compile(r"\s*\([^)]*\)\s*")

# "lit." and "fig." appear both bare and parenthesised in CC-CEDICT.
_LIT = re.compile(r"^\(?lit\.\)?\s*", re.I)
_FIG = re.compile(r"^\(?fig\.\)?\s*", re.I)
_INLINE_FIG = re.compile(r";?\s*\(?fig\.\)?\s+", re.I)


# Classical function words and bound morphemes. CC-CEDICT orders these entries
# by their modern standalone sense, which is the wrong one inside a chengyu:
# 所 leads with "actually" but in 一无所有 it is the nominalising 所, and 莫
# leads with the surname. Everything here is a grammatical word or a character
# whose chengyu sense is fixed — not a judgement call about a content word.
CLASSICAL = {
    "所": "that which", "其": "its", "之": "of", "以": "by means of",
    "而": "and yet", "乃": "thereupon", "者": "the one who", "于": "at",
    "何": "what", "莫": "none", "无": "without", "亦": "also",
    "夫": "that", "焉": "therein", "矣": "(perfective)", "哉": "(exclamatory)",
    "当": "ought", "然": "so", "自": "self", "为": "to be", "与": "with",
    "斯": "this", "兹": "this", "尔": "you", "彼": "that", "此": "this",
    "翼": "wing", "唯": "only", "惟": "only", "既": "already", "犹": "still",
    "奈": "how", "岂": "how could", "勿": "do not", "毋": "do not",
}


def _clean(sense):
    s = re.sub(r"\((?:idiom|saying|idiom\.)\)", " ", sense)
    s = " ".join(s.split())
    # Removing "(idiom)" from mid-sense leaves a space before the semicolon
    # that used to follow it: "not having anything at all ; utterly lacking".
    s = re.sub(r"\s+([;,])", r"\1", s)
    return s.strip(" ;,")


def _shorten(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip(" ,;") + "…"


def _char_gloss(ch, reading):
    """The best short English for one character, under the reading the chengyu
    gives it. Falls back to the character's senses at large when that reading
    has no entry (rare: a chengyu preserving a pronunciation CC-CEDICT drops)."""
    if ch in CLASSICAL:
        return CLASSICAL[ch]
    pool = dictionary.senses_for_reading(ch, reading) if reading else None
    if not pool:
        pool = dictionary.senses(ch)
    if not pool:
        return None
    usable = [s for s in pool
              if not _JUNK.match(s) and not _PROPER.match(s.strip())]
    for sense in (usable or pool):
        g = _clean(sense.split(";")[0])
        # "(bound form) idea", "(of a period of time) long", "(prefix) can" —
        # the qualifier is for a dictionary reader, not for a four-part gloss.
        g = " ".join(_PARENS.sub(" ", g).split())
        # Leading "to " on every verb makes the row read as a list of
        # infinitives; the reader already knows these are glosses.
        if g.startswith("to "):
            g = g[3:]
        if g:
            return _shorten(g, _CHAR_GLOSS_MAX)
    return None


def literal_from_chars(word, reading=None):
    """不知不觉 -> 不 not + 知 know + 不 not + 觉 feel.

    `reading` is the whole word's numbered pinyin, split across the characters
    so each one is glossed under the pronunciation it actually has here.
    """
    syls = (reading or "").split()
    if len(syls) != len(word):
        syls = [None] * len(word)
    parts = []
    for ch, syl in zip(word, syls):
        g = _char_gloss(ch, syl)
        if not g:
            return None
        parts.append(f"{ch} {g}")
    return " + ".join(parts)


def entry(word):
    """Everything the chengyu table renders for one idiom.

    Keys: word, pinyin, pinyin_exact (False when assembled per character),
    literal, literal_exact (True when CC-CEDICT supplied a "lit." rendering),
    meaning, hsk (level code or None). Any value but `word` may be None — the
    table renders what exists rather than dropping a row over one blank cell.
    """
    raw_py = dictionary.pinyin_raw(word)
    py_exact = raw_py is not None
    if not raw_py:
        raw_py = dictionary.pinyin_chars_raw(word)

    raw = dictionary.senses(word)
    lit = [s for s in raw if _LIT.match(s)]
    fig = [s for s in raw if _FIG.match(s)]
    rest = [s for s in raw if s not in lit and s not in fig]

    literal = literal_exact = None
    if lit:
        # A single sense often carries both halves: "draw legs on a snake
        # (idiom); fig. to ruin the effect by adding sth superfluous".
        halves = _INLINE_FIG.split(_LIT.sub("", lit[0]), 1)
        literal = _clean(halves[0])
        tail = halves[1] if len(halves) > 1 else ""
        literal_exact = True
        if tail and not fig:
            fig = [tail]
            rest = [s for s in rest if s not in lit]
    if not literal:
        literal = literal_from_chars(word, raw_py)
        literal_exact = False

    # "fig." senses are the actual meaning when present; otherwise the ordinary
    # senses are. Keeping the lit. sense out of this column is the whole point
    # of having two columns.
    picked = [_FIG.sub("", s) for s in fig] or rest or raw
    meaning = "; ".join(x for x in (_clean(s) for s in picked[:2]) if x) or None
    if meaning:
        meaning = _shorten(meaning, _MEANING_MAX)

    return {
        "word": word,
        "pinyin": dictionary.to_tone_marks(raw_py) if raw_py else None,
        "pinyin_exact": py_exact,
        "literal": literal,
        "literal_exact": bool(literal_exact),
        "meaning": meaning,
        "hsk": hskdata.hsk_dict().get(word),
    }
