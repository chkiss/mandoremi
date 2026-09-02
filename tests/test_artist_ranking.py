"""Candidate ranking in discover_deep.pick_best.

Payloads below are trimmed from real NetEase /search/get responses. They are
the cases that silently produced WRONG artists in the corpus: catalog size was
outranking the quality of the match, so an act that merely listed the query as
one of its aliases could beat the artist actually named that.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))

import discover_deep as dd                                 # noqa: E402


def art(name, id, musicSize, alias=None, transNames=None):
    return {"name": name, "id": id, "musicSize": musicSize,
            "albumSize": 0, "alias": alias or [], "transNames": transNames}


# 羽·泉 list "野孩子" (one of their songs) as an alias and carry 3x the catalog.
YEHAIZI = [art("野孩子", 13416, 129), art("杨千嬅", 9621, 1294, ["Miriam Yeung"]),
           art("羽·泉", 13418, 375, ["野孩子"]), art("野孩子", 30360756, 2)]
# 水樹奈々 is a Japanese singer whose translated name prefixes ours.
SHUISHU = [art("水树", 35122573, 105, ["Tassi"]),
           art("水樹奈々", 17028, 1022, transNames=["水树奈奈"]),
           art("水树", 12345, 83)]
# 仓鼠ミツキ carries the alias 鼠鼠, a prefix of 鼠鼠鼠.
SHUSHUSHU = [art("鼠鼠鼠", 94644093, 17),
             art("仓鼠ミツキ", 33428946, 168, ["鼠鼠", "仓鼠", "maki"])]
# 李志洲 is a different person; the real 李志 pages are near-empty.
LIZHI = [art("李志", 121462061, 1), art("李志", 3688, 2),
         art("李志辉", 12057, 290), art("李志洲", 4063, 56)]


@pytest.mark.parametrize("query, candidates, want_name", [
    ("野孩子", YEHAIZI, "野孩子"),
    ("水树", SHUISHU, "水树"),
    ("鼠鼠鼠", SHUSHUSHU, "鼠鼠鼠"),
])
def test_exact_name_outranks_a_bigger_catalog(query, candidates, want_name):
    got = dd.pick_best(query, (), candidates)
    assert got is not None
    assert got[0] == want_name


def test_largest_catalog_still_wins_within_a_tier():
    """The stub-page rule this tie-break exists for must survive: among equally
    good name matches, prefer the page that actually holds songs."""
    got = dd.pick_best("水树", (), [art("水树", 1, 0), art("水树", 2, 105)])
    assert got == ("水树", 2)


def test_all_matches_empty_is_no_match():
    assert dd.pick_best("水树", (), [art("水树", 1, 0)]) is None


def test_an_empty_stub_must_not_shadow_the_real_artist():
    """The regression tiering introduced: a 0-song page matching the name
    exactly took tier 0 and shut out the real artist, who matched one tier
    lower. '草東沒有派對 (No Party For Cao Dong)' resolved to nothing this way."""
    stub = art("No Party For Cao Dong", 1, 0)
    real = art("草东没有派对", 1161122, 20, ["No Party For Cao Dong"])
    assert dd.pick_best("草東沒有派對 (No Party For Cao Dong)", (),
                        [stub, real]) == ("草东没有派对", 1161122)


def test_han_suffix_that_is_more_name_is_rejected():
    """李志 + 洲 is a different person. Nothing else matches, so: no match."""
    assert dd.pick_best("李志", (), [c for c in LIZHI if c["musicSize"] > 2]) is None


@pytest.mark.parametrize("query, netease, ok", [
    ("小老虎", "小老虎J-Fever", True),       # latin stage-name suffix
    ("马木尔", "马木尔Mamer", True),
    ("法兹", "法兹乐队 FAZI", True),          # generic band word
    ("李高特", "李高特三重奏", True),
    ("张梦", "张梦奇", False),
    ("郑好", "郑好儿", False),
    ("颜羽", "颜羽汐_yuxi", False),
])
def test_prefix_rule(query, netease, ok):
    got = dd.pick_best(query, (), [art(netease, 99, 100)])
    assert (got is not None) is ok


def test_alias_match_still_resolves_when_no_name_matches():
    """Tier 1 is a real match, just a worse one than tier 0."""
    got = dd.pick_best("Wild Children", (), [art("野孩子", 13416, 129,
                                                 ["Wild Children"])])
    assert got == ("野孩子", 13416)
