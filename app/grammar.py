"""Detection of common HSK grammar patterns over segmented lines."""
import re

from . import hskdata

HAN = lambda s: any("一" <= ch <= "鿿" for ch in s)

_RX = {}  # compiled regex cache, keyed by pattern key


def detect(token_lines, text_lines):
    """token_lines: list of token lists; text_lines: matching display lines.
    Returns [{key, name, level, count, examples}] for detected patterns only."""
    results = []
    for pat in hskdata.grammar_patterns():
        count = 0
        examples = []
        for tokens, line in zip(token_lines, text_lines):
            hits = _match_line(pat, tokens, line)
            if hits:
                count += hits
                if len(examples) < 3:
                    examples.append(line)
        if count:
            results.append({
                "key": pat["key"], "name": pat["name"], "level": pat["level"],
                "count": count, "examples": examples,
            })
    results.sort(key=lambda r: (r["level"], -r["count"]))
    return results


def _match_line(pat, tokens, line):
    t = pat["type"]
    if t == "substring":
        return sum(line.count(p) for p in pat["parts"])
    if t == "regex":
        rx = _RX.get(pat["key"])
        if rx is None:
            rx = _RX[pat["key"]] = re.compile(pat["regex"])
        return sum(1 for _ in rx.finditer(line))
    if t == "separable":
        # 离合词 split by inserted material: 见面 -> 见你一面, 睡觉 -> 睡了一觉.
        # Dictionary lookups (HSK + CC-CEDICT) weed out lexical look-alikes:
        # skip when the verb char belongs to the word on its left (看见) or
        # opens a compound (伤悲), or the object char belongs to a word around
        # it (画面, 面前).
        from . import dictionary
        dictionary.load()
        d = hskdata.hsk_dict()
        is_word = lambda w: w in d or dictionary.gloss(w) is not None
        n = 0
        for v, o in pat["pairs"]:
            start = 0
            while True:
                i = line.find(v, start)
                if i < 0:
                    break
                start = i + 1
                # HSK dict only: CEDICT also lists rarities like 想见 that
                # collide with auxiliary + split (想 + 见你一面)
                if i > 0 and line[i - 1] + v in d:
                    continue
                window = line[i + 1: i + 6]
                j = window.find(o)
                if not 1 <= j <= 4:
                    continue
                gap = window[:j]
                if not all("一" <= ch <= "鿿" for ch in gap):
                    continue
                # v may open a compound (伤悲…心 is not 伤心 split) — but an
                # insertion marker later in the gap signals a real split with
                # a complement (伤透了…心, 握紧的手)
                if (gap[0] not in "了着过一个二两三起" and is_word(v + gap[0])
                        and not any(ch in "了着过一个二两三的得" for ch in gap[1:])):
                    continue
                # a gap ending in an insertion marker (睡了一觉, 帮个忙) is a
                # certain split; otherwise skip look-alikes like 情歌, 画面
                if gap[-1] not in "了着过一个二两三次场回顿番的得不好":
                    if is_word(gap[-1] + o):
                        continue
                    after = line[i + 1 + j + 1: i + 1 + j + 2]
                    if after and is_word(o + after):
                        continue
                n += 1
        return n
    if t == "reduplicated_token":
        # AA verb/adjective reduplication (看看, 慢慢); lexical AA words like
        # kinship terms are excluded via the pattern's list
        excl = set(pat.get("exclude", []))
        return sum(1 for tok in tokens
                   if len(tok) == 2 and tok[0] == tok[1] and tok not in excl)
    if t == "token":
        return sum(1 for tok in tokens if tok in pat["tokens"])
    if t == "token_before_han":
        n = 0
        for i, tok in enumerate(tokens):
            if tok in pat["tokens"] and i + 1 < len(tokens) and HAN(tokens[i + 1]):
                n += 1
        return n
    if t == "pair":
        if pat.get("exclude_substring") and pat["exclude_substring"] in line:
            return 0
        first = None
        for i, tok in enumerate(tokens):
            if tok in pat["a"]:
                first = i
                break
        if first is None:
            return 0
        if pat.get("b_final"):
            return 1 if tokens and tokens[-1] in pat["b"] and first < len(tokens) - 1 else 0
        for tok in tokens[first + 1:]:
            if tok in pat["b"]:
                return 1
        return 0
    return 0
