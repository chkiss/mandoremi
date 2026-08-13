#!/usr/bin/env python3
"""Find real English artist names on Wikidata, for artists NetEase has none for.

Why Wikidata rather than romanisation: 万能青年旅店 is not "Wan Neng Qing Nian
Lü Dian", it is **Omnipotent Youth Society** — the name the band uses in
English. Likewise 二手玫瑰 is Second Hand Rose, 海朋森 is Hiperson, 原子邦妮 is
Astro Bunny. Transliteration cannot know any of that; a public database of
artist names can. It is programmatic, free, and needs no key.

Fuzzy name search across a general knowledge base is dangerous, so three
guards, each of which caught a real mis-match while this was being written:

  * MATCH THE LABEL, NEVER AN ALIAS. Searching 小老虎 returns the Indian actor
    N. T. Rama Rao Jr., because 小老虎 is one of his Chinese nicknames. His
    label is 小N·T·拉馬·饒, so a label-only match rejects him. An alias match
    would not have: he is also credited as a playback singer, so even an
    occupation check passes.
  * IT MUST BE A MUSICIAN OR A GROUP. Searching 刺猬 returns the animal
    (P31 = taxon); 法老 returns the Egyptian title. Both are rejected on type.
  * IT MUST HAVE AN ENGLISH LABEL. 银临 has none, so she is left for the
    curated file rather than given something invented.

Anything rejected is simply left alone: the artist keeps their Chinese name, or
whatever data/artist_names.json already says. Existing curated entries always
win — this never overwrites a name a human chose.

  ./.venv/bin/python tools/enrich_names_wikidata.py [--dry-run] [--limit N]
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db  # noqa: E402

API = "https://www.wikidata.org/w/api.php"
UA = "Mandoremi/1.0 (https://mandoremi.com; artist name lookup)"
NAMES_FILE = os.path.join(os.path.dirname(__file__), "..", "data",
                          "artist_names.json")

# P31 (instance of) values that mean "a band/duo/ensemble".
GROUP_TYPES = {"Q215380", "Q2088357", "Q5741069", "Q9212979", "Q56816954"}
# P106 (occupation) values that mean "makes music", for P31=human (Q5).
MUSIC_JOBS = {"Q177220", "Q753110", "Q2252262", "Q639669", "Q36834",
              "Q488205", "Q855091", "Q158852"}
HUMAN = "Q5"


def _get(params, timeout=25):
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _claim_ids(entity, prop):
    out = set()
    for c in (entity.get("claims") or {}).get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value", {})
        if isinstance(v, dict) and v.get("id"):
            out.add(v["id"])
    return out


def _traditional(text):
    """Simplified -> traditional, for the Wikidata lookup only.

    We store artist names simplified, but Taiwanese and Hong Kong acts are
    labelled in traditional on Wikidata: 好乐团 finds nothing, while 好樂團 is
    Q67934438 with the English label "GoodBand". Since a large share of the
    corpus is Taiwanese, searching only the simplified form silently loses
    their real English names.
    """
    try:
        from opencc import OpenCC
        return OpenCC("s2t").convert(text)
    except Exception:                               # noqa: BLE001
        return text


def resolve(name):
    """(english, qid) for a Chinese artist name, or (None, reason)."""
    # Try the name as we store it, then its traditional form.
    forms = [name]
    trad = _traditional(name)
    if trad != name:
        forms.append(trad)
    for form in forms:
        out = _resolve_one(form)
        if out[0]:
            return out
    return out


def _resolve_one(name):
    d = _get({"action": "wbsearchentities", "search": name, "language": "zh",
              "uselang": "zh", "format": "json", "limit": 5, "type": "item"})
    ids = [h["id"] for h in (d.get("search") or [])]
    if not ids:
        return None, "no search hit"
    ent = _get({"action": "wbgetentities", "ids": "|".join(ids),
                "format": "json", "props": "labels|claims",
                "languages": "en|zh|zh-hans|zh-hant"})
    for qid in ids:
        e = (ent.get("entities") or {}).get(qid) or {}
        labels = {v["value"] for v in (e.get("labels") or {}).values()}
        if name not in labels:
            continue                      # label only — see the module docstring
        types = _claim_ids(e, "P31")
        jobs = _claim_ids(e, "P106")
        if not (types & GROUP_TYPES or (HUMAN in types and jobs & MUSIC_JOBS)):
            return None, "not a musician or group"
        en = (e.get("labels") or {}).get("en", {}).get("value")
        if not en:
            return None, "no English label"
        if en == name:
            return None, "English label is just the Chinese name"
        return en, qid
    return None, "no exact label match"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pause", type=float, default=0.4)
    args = ap.parse_args()

    db.init()
    conn = db.connect()
    have_en = {r["artist_id"] for r in conn.execute(
        "SELECT artist_id FROM artist_alias "
        "WHERE english IS NOT NULL AND length(english) > 0")}
    display = {r["artist_id"]: r["display"] for r in conn.execute(
        "SELECT artist_id, display FROM artist_alias")}
    counts = {}
    for r in conn.execute("SELECT artist_id, COUNT(*) n FROM seed_analysis "
                          "WHERE artist_id IS NOT NULL GROUP BY artist_id"):
        counts[r["artist_id"]] = r["n"]
    conn.close()

    curated = {}
    if os.path.exists(NAMES_FILE):
        with open(NAMES_FILE, encoding="utf-8") as f:
            curated = json.load(f)

    todo = [(aid, display[aid], n) for aid, n in
            sorted(counts.items(), key=lambda kv: -kv[1])
            if aid in display and aid not in have_en
            and display[aid] not in curated]
    print(f"{len(counts)} artists in the corpus; {len(todo)} with no English name")
    if args.limit:
        todo = todo[:args.limit]

    found = skipped = 0
    for aid, name, n in todo:
        try:
            en, why = resolve(name)
        except Exception as exc:                        # noqa: BLE001
            print(f"  !! {name}: {exc}")
            time.sleep(args.pause)
            continue
        if en:
            print(f"  ++ {name:<14} -> {en:<32} ({why}, {n} songs)")
            curated[name] = en
            found += 1
        else:
            print(f"  -- {name:<14}    {why}")
            skipped += 1
        time.sleep(args.pause)

    if not args.dry_run and found:
        with open(NAMES_FILE, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(curated.items())), f,
                      ensure_ascii=False, indent=2)
            f.write("\n")
    print(f"\nfound {found}, left alone {skipped}"
          + (" (dry run, nothing written)" if args.dry_run
             else f" -> {os.path.relpath(NAMES_FILE)}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
