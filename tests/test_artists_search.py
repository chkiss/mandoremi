"""Candidate ranking in app.artists.search.

The same rules as tools/discover_deep.pick_best, in the copy that runs behind
playlist import. Kept as its own suite because the two implementations have
drifted before: the tools copy was fixed first and this one still resolved
水树 to the Japanese singer 水樹奈々.

The NetEase call is stubbed, so these are offline.
"""
import json
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import artists                                    # noqa: E402


def art(name, id, musicSize, alias=None, transNames=None):
    return {"name": name, "id": id, "musicSize": musicSize,
            "albumSize": 0, "alias": alias or [], "transNames": transNames}


@pytest.fixture
def netease(monkeypatch):
    """Stub urlopen so search() sees exactly the candidate list under test."""
    def install(candidates):
        payload = json.dumps({"result": {"artists": candidates}}).encode()

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(artists.urllib.request, "urlopen",
                            lambda *a, **k: Resp(payload))
    return install


def test_exact_name_beats_an_alias_match_with_a_bigger_catalog(netease):
    netease([art("野孩子", 13416, 129),
             art("羽·泉", 13418, 375, ["野孩子"])])
    assert artists.search("野孩子")[:2] == (13416, "野孩子")


def test_translated_name_prefix_does_not_win(netease):
    netease([art("水树", 35122573, 105, ["Tassi"]),
             art("水樹奈々", 17028, 1022, transNames=["水树奈奈"])])
    assert artists.search("水树")[:2] == (35122573, "水树")


def test_stub_does_not_shadow_the_real_artist(netease):
    netease([art("No Party For Cao Dong", 1, 0),
             art("草东没有派对", 1161122, 20, ["No Party For Cao Dong"])])
    got = artists.search("草東沒有派對 (No Party For Cao Dong)")
    assert got[:2] == (1161122, "草东没有派对")


def test_all_stubs_is_no_match(netease):
    netease([art("水树", 1, 0)])
    assert artists.search("水树") is None


@pytest.mark.parametrize("netease_name, ok", [
    ("小老虎J-Fever", True),      # latin stage-name suffix: same act
    ("法兹乐队 FAZI", True),       # generic band word
    ("李志洲", False),            # a different person
    ("张梦奇", False),
])
def test_han_prefix_rule(netease, netease_name, ok):
    query = {"小老虎J-Fever": "小老虎", "法兹乐队 FAZI": "法兹",
             "李志洲": "李志", "张梦奇": "张梦"}[netease_name]
    netease([art(netease_name, 99, 100)])
    assert (artists.search(query) is not None) is ok


def test_alias_match_resolves_when_nothing_matches_by_name(netease):
    netease([art("野孩子", 13416, 129, ["Wild Children"])])
    got = artists.search("Wild Children")
    assert got[:2] == (13416, "野孩子")
    # `confidence` describes how the STRING matched, not which field it came
    # from, so an alias matching verbatim is still "exact".
    assert got[2] == "exact"
