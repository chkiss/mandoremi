"""Lyrics text normalization: traditional->simplified, strip timestamps,
section labels, punctuation; preserve line breaks for display."""
import re

from opencc import OpenCC

_cc = OpenCC("t2s")

TIMESTAMP_RE = re.compile(r"[\[\(（【]?\d{1,2}[:：]\d{2}(?:[.:．]\d{1,3})?[\]\)）】]?")
SECTION_RE = re.compile(
    r"^\s*[\[\(（【]?\s*"
    r"(verse|chorus|bridge|intro|outro|pre-?chorus|hook|refrain|rap"
    r"|主歌|副歌|前奏|间奏|尾奏|导歌|桥段|说唱|合唱|独唱"
    r"|作词|作曲|编曲|演唱|词|曲|监制|制作人?|混音|吉他|贝斯|鼓|和声|录音)"
    r"\s*[:：]?[^\n]*[\]\)）】]?\s*$",
    re.IGNORECASE,
)
# strip everything except Han chars, latin letters/digits (kept so we can mark
# them Unknown), and whitespace
PUNCT_RE = re.compile(r"[^一-鿿㐀-䶿A-Za-z0-9\s]")


# OpenCC leaves these alone but they're near-universal in Mandopop lyrics:
# 妳 (feminine you) -> 你; particle 著 -> 着 except in zhù words where 著 is
# also the simplified form.
ZHU_WORDS = ("著名", "著作", "显著", "昭著", "土著", "编著", "著称",
             "名著", "原著", "专著", "著述", "著书")
ZHU_PLACEHOLDER = ""


def to_simplified(text):
    text = _cc.convert(text).replace("妳", "你")
    for w in ZHU_WORDS:
        text = text.replace(w, w.replace("著", ZHU_PLACEHOLDER))
    text = text.replace("著", "着").replace(ZHU_PLACEHOLDER, "著")
    return text


def clean_lines(text):
    """Return display-ready simplified lines (junk lines removed)."""
    text = to_simplified(text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = []
    for raw in text.split("\n"):
        line = TIMESTAMP_RE.sub(" ", raw)
        if SECTION_RE.match(line):
            continue
        line = PUNCT_RE.sub(" ", line)
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return lines
