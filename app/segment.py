"""pkuseg segmentation with an HSK + idiom user dictionary (longest match)."""
import re
import threading

import spacy_pkuseg as pkuseg

from . import hskdata

_lock = threading.Lock()
_seg = None

HAN_RUN = re.compile(r"[一-鿿㐀-䶿]+|[A-Za-z0-9]+")


def _segmenter():
    global _seg
    with _lock:
        if _seg is None:
            _seg = pkuseg.pkuseg(user_dict=hskdata.user_dict_path())
    return _seg


def segment_line(line):
    """Segment one normalized line into tokens. Non-Han runs (latin/digits)
    pass through as single tokens."""
    seg = _segmenter()
    tokens = []
    for run in HAN_RUN.findall(line):
        if run[0].isascii():
            tokens.append(run)
            continue
        # merge idioms the model may have split: greedy longest match against
        # the idiom set over the model's token stream. pkuseg's thread safety
        # is not guaranteed and FastAPI runs sync endpoints in a threadpool,
        # so serialize cut() — it's CPU-bound anyway on this 2-core box.
        with _lock:
            raw = seg.cut(run)
        tokens.extend(_merge_idioms(raw))
    return tokens


def _merge_idioms(tokens):
    idioms = hskdata.idiom_set()
    out = []
    i = 0
    n = len(tokens)
    while i < n:
        merged = None
        # try to combine up to 4 consecutive tokens into a known idiom
        for j in range(min(n, i + 4), i + 1, -1):
            cand = "".join(tokens[i:j])
            if 3 <= len(cand) <= 8 and cand in idioms:
                merged = (cand, j)
                break
        if merged:
            out.append(merged[0])
            i = merged[1]
        else:
            out.append(tokens[i])
            i += 1
    return out
