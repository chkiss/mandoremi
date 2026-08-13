#!/usr/bin/env python3
"""Merge CC-CEDICT's idiom-flagged entries into data/chengyu.txt.

WHY THIS EXISTS
---------------
The original list is a frequency-ranked dump of 8,519 chengyu. Audited against
CC-CEDICT, it was missing 1,720 four-character entries that CC-CEDICT itself
marks "(idiom)" — including ones that turn up in ordinary writing (一日三秋,
一心二用, 一刻千金). Every one of those is a chengyu the app was silently
failing to recognise, in the segmenter and on every page that counts them.

WHAT THIS CHANGES DOWNSTREAM (read before running)
--------------------------------------------------
The idiom set feeds `hskdata.user_dict_path()`, which biases pkuseg toward
longest-match. Adding entries therefore changes SEGMENTATION, not just idiom
counts: 一日三秋 currently comes out as two or four tokens and afterwards comes
out as one. So every stored analysis is stale the moment this runs. The
sequence is:

  1. run this tool
  2. delete data/user_dict.txt so it regenerates from the new set
  3. bump "analysis_version" in data/config.json
  4. re-seed (tools/seed_corpus.py), which re-fetches and REPLACES every row
  5. regenerate the /artists snapshot and re-read its prose

Skipping step 4 leaves the corpus half-analysed under two different notions of
what a chengyu is, which is worse than not merging at all.

SCOPE: four characters only
---------------------------
CC-CEDICT flags ~800 idioms longer than four characters. Those are proverbs and
sayings (歇后语 and full sentences), and collapsing a seven-character proverb
into one token distorts the vocabulary bag far more than it helps. The 4-char
form is what "chengyu" means to a learner and what the app's UI claims to show.

Usage:  ./.venv/bin/python tools/merge_chengyu.py [--apply]
Without --apply it prints what would change and writes nothing.
"""
import argparse
import gzip
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data")
CHENGYU = os.path.join(DATA, "chengyu.txt")
CEDICT = os.path.join(DATA, "cedict.u8.gz")

_LINE = re.compile(r"^\S+ (\S+) \[([^\]]*)\] /(.*)/$")
_HAN = re.compile(r"^[一-鿿]{4}$")


def cedict_idioms():
    """Four-character simplified entries CC-CEDICT marks as idioms."""
    out = {}
    with gzip.open(CEDICT, "rt", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            m = _LINE.match(line.strip())
            if not m:
                continue
            simp, _py, defs = m.groups()
            if not _HAN.match(simp):
                continue
            if "(idiom" in defs or "idiom)" in defs:
                out.setdefault(simp, defs)
    return out


def existing():
    """word -> original line, preserving the file's frequency column."""
    out = {}
    with open(CHENGYU, encoding="utf-8") as f:
        for ln in f:
            ln = ln.rstrip("\n")
            if ln.strip():
                out.setdefault(ln.split()[0].strip(), ln)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    have = existing()
    ced = cedict_idioms()
    new = sorted(w for w in ced if w not in have)

    print(f"existing {len(have)}  cedict 4-char idioms {len(ced)}  "
          f"new {len(new)}  merged {len(have) + len(new)}")
    print("\nsample of what would be added:")
    for w in new[:15]:
        print(f"  {w}  {ced[w][:70]}")

    if not args.apply:
        print("\n(dry run — pass --apply to write)")
        return

    # Appended, not re-sorted: the head of this file is frequency-ranked and
    # a diff that only appends is a diff a human can actually review. The new
    # entries carry frequency 0 because CC-CEDICT has no frequency data — and
    # nothing reads column 2, only hskdata.idiom_set(), which takes column 1.
    with open(CHENGYU, "a", encoding="utf-8") as f:
        for w in new:
            f.write(f"{w} \t 0\n")
    print(f"\nappended {len(new)} entries to {CHENGYU}")

    ud = os.path.join(DATA, "user_dict.txt")
    if os.path.exists(ud):
        os.remove(ud)
        print(f"removed {ud} — it regenerates from the new set on next load")
    print("\nNEXT: bump analysis_version in data/config.json, then re-seed. "
          "Until the re-seed finishes the corpus is mixed-version.")


if __name__ == "__main__":
    main()
