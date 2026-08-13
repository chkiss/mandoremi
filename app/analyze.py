"""Full analysis pipeline: lyrics text -> stats/token/grammar JSON."""
import hashlib
import math

from . import grammar, hskdata, normalize, segment

LEVELS = [1, 2, 3, 4, 5, 6, 7]        # 7 == HSK 7-9
LEARNER_LEVELS = [0] + LEVELS          # 0 == pre-HSK1 (knows nothing yet)


def lyrics_hash(text):
    joined = "\n".join(normalize.clean_lines(text))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def analyze(text):
    lines = normalize.clean_lines(text)
    token_lines = [segment.segment_line(ln) for ln in lines]

    # classify every token; vocab keyed by normalized form
    display_lines = []   # [[{t, n, lvl, i(diom)}]] for the lyric viewer
    for tokens in token_lines:
        row = []
        for tok in tokens:
            norm, lvl = hskdata.classify(tok)
            is_idiom = tok in hskdata.idiom_set() or norm in hskdata.idiom_set()
            row.append({"t": tok, "n": norm, "lvl": lvl, "i": int(is_idiom)})
        display_lines.append(row)
    vocab, total_tokens = _vocab_from_lines(display_lines)

    stats = _stats(vocab, total_tokens)
    gram = grammar.detect(token_lines, lines)
    idioms = sorted(
        [{"word": n, "count": v["count"], "lvl": v["lvl"]} for n, v in vocab.items() if v["idiom"]],
        key=lambda x: -x["count"],
    )
    return {
        "version": hskdata.config()["analysis_version"],
        "lines": display_lines,
        "vocab": vocab,
        "stats": stats,
        "grammar": gram,
        "idioms": idioms,
    }


def _vocab_from_lines(lines):
    """Build the vocab bag (norm -> {lvl, count, idiom, forms}) from display
    lines. Fillers are excluded from counts."""
    vocab = {}
    total = 0
    for row in lines:
        for t in row:
            if t["lvl"] == hskdata.LEVEL_FILLER:
                continue
            total += 1
            v = vocab.setdefault(t["n"], {"lvl": t["lvl"], "count": 0, "idiom": t["i"], "forms": []})
            v["count"] += 1
            if t["t"] != t["n"] and t["t"] not in v["forms"]:
                v["forms"].append(t["t"])
    return vocab, total


def _merge_known_tokens(lines, known):
    """Personal known words are often outside every dictionary, so the
    segmenter splits them (滄海 -> 沧/海). Re-join runs of up to 4 adjacent
    Chinese tokens that exactly form a known word."""
    for li, row in enumerate(lines):
        out = []
        i = 0
        while i < len(row):
            merged_end = None
            for j in range(min(len(row), i + 4), i + 1, -1):
                cand = "".join(t["t"] for t in row[i:j])
                if cand in known and all(1 <= t["lvl"] <= hskdata.LEVEL_BEYOND for t in row[i:j]):
                    merged_end = j
                    break
            if merged_end:
                cand = "".join(t["t"] for t in row[i:merged_end])
                out.append({"t": cand, "n": cand,
                            "lvl": hskdata.hsk_dict().get(cand, hskdata.LEVEL_BEYOND),
                            "i": int(cand in hskdata.idiom_set())})
                i = merged_end
            else:
                out.append(row[i])
                i += 1
        lines[li] = out


def strip_text(analysis):
    """Text-free variant for auto-fetched songs: keeps the stats, the vocab
    bag, grammar/idiom counts — data about the song — and drops everything
    that reconstructs the lyric text (lines, grammar example lines)."""
    a = {k: analysis[k] for k in ("version", "vocab", "stats", "idioms")}
    a["grammar"] = [{k: g[k] for k in ("key", "name", "level", "count")}
                    for g in analysis["grammar"]]
    a["ghost"] = 1
    return a


def _flag_vocab(vocab, known):
    """Mark known words and probable-known substrings (没 when list has 没有)."""
    parts = {kw[i:j] for kw in known if len(kw) <= 8
             for i in range(len(kw)) for j in range(i + 1, len(kw) + 1)} - known
    for norm, v in vocab.items():
        v["known"] = 1 if norm in known else 0
        if not v["known"] and norm in parts:
            v["p"] = 1


def personalize(analysis, known):
    """Overlay a user's personal known-word set: re-merges split tokens, flags
    vocab entries, and recomputes per-level stats so known words count as known
    at every level. Mutates and returns the (deserialized) analysis; never
    touches stored rows."""
    if not known:
        return analysis
    if "lines" not in analysis:
        # text-free (ghost) analysis: no token re-merge possible, but the
        # vocab bag still personalizes fully
        _flag_vocab(analysis["vocab"], known)
        analysis["stats"] = _stats(analysis["vocab"],
                                   analysis["stats"]["total_tokens"], known)
        return analysis
    _merge_known_tokens(analysis["lines"], known)
    vocab, total_tokens = _vocab_from_lines(analysis["lines"])
    _flag_vocab(vocab, known)
    analysis["vocab"] = vocab
    analysis["stats"] = _stats(vocab, total_tokens, known)
    return analysis


def _stats(vocab, total_tokens, known=frozenset()):
    """All numeric stats, incl. per-learner-level coverage and learning value.
    Chinese tokens only (level 1-7 or beyond=8); non-Chinese 'unknown' tokens
    are reported in the distribution but excluded from coverage. Words in
    `known` count as known regardless of HSK level; the HSK distribution
    itself always reflects true levels."""
    counts_by_level = {lvl: 0 for lvl in LEVELS + [hskdata.LEVEL_BEYOND, hskdata.LEVEL_UNKNOWN]}
    unique_by_level = {lvl: 0 for lvl in LEVELS + [hskdata.LEVEL_BEYOND, hskdata.LEVEL_UNKNOWN]}
    for v in vocab.values():
        counts_by_level[v["lvl"]] += v["count"]
        unique_by_level[v["lvl"]] += 1

    chinese_tokens = sum(counts_by_level[l] for l in LEVELS) + counts_by_level[hskdata.LEVEL_BEYOND]
    chinese_unique = sum(unique_by_level[l] for l in LEVELS) + unique_by_level[hskdata.LEVEL_BEYOND]

    per_level = {}
    for learner in LEARNER_LEVELS:
        known_tokens = sum(
            v["count"] for norm, v in vocab.items()
            if v["lvl"] != hskdata.LEVEL_UNKNOWN
            and ((v["lvl"] in LEVELS and v["lvl"] <= learner) or norm in known))
        coverage = known_tokens / chinese_tokens if chinese_tokens else 0.0
        unk = [v for norm, v in vocab.items()
               if v["lvl"] > learner and v["lvl"] != hskdata.LEVEL_UNKNOWN and norm not in known]
        unique_unknown = len(unk)
        unknown_token_count = sum(v["count"] for v in unk)
        repeated_unknown = len([v for v in unk if v["count"] > 1])
        avg_reps = unknown_token_count / unique_unknown if unique_unknown else 0.0
        per_level[learner] = {
            "coverage": round(coverage, 4),
            "unique_unknown": unique_unknown,
            "repeated_unknown": repeated_unknown,
            "avg_reps_unknown": round(avg_reps, 2),
            "learning_value": round(learning_value(coverage, unique_unknown, chinese_unique, avg_reps), 1),
        }

    return {
        "total_tokens": total_tokens,
        "chinese_tokens": chinese_tokens,
        "unique_vocab": chinese_unique,
        "richness": round(chinese_unique / chinese_tokens, 4) if chinese_tokens else 0.0,
        "counts_by_level": counts_by_level,
        "unique_by_level": unique_by_level,
        "per_level": per_level,
    }


def learning_value(coverage, unique_unknown, unique_total, avg_reps):
    """Configurable 0-100 score: rewards repetition of unknowns, few unique
    unknowns, and moderate (not total) coverage."""
    cfg = hskdata.config()["learning_value"]
    rep = min(avg_reps, cfg["repetition_cap"]) / cfg["repetition_cap"]
    few = 1.0 - min(unique_unknown / unique_total, 1.0) if unique_total else 0.0
    cov = math.exp(-((coverage - cfg["ideal_coverage"]) ** 2) / (2 * cfg["coverage_sigma"] ** 2))
    score = (cfg["weight_repetition"] * rep
             + cfg["weight_few_unknown"] * few
             + cfg["weight_coverage"] * cov)
    return 100.0 * score
