"""Pure parsing in tools/discover_avalanche.py.

Only the offline functions are covered: name splitting and embed extraction.
Everything that touches Substack, YouTube or NetEase is left to the live run.
"""
import html
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))

import discover_avalanche as da                            # noqa: E402


@pytest.mark.parametrize("raw, primary, must_contain", [
    # Bracketed second spelling -- the Han form wins, both stay searchable.
    ("Hualun（花伦）", "花伦", ["Hualun", "花伦"]),
    ("波卡利甜 Pocari Sweet", "波卡利甜", ["Pocari Sweet", "波卡利甜"]),
    # Mixed-script billing splits on the script boundary.
    ("马木尔 Mamer", "马木尔", ["Mamer", "马木尔"]),
    # Collaborations seed the lead act; the partner stays as an alias.
    ("竇唯 & 朝簡", "竇唯", ["朝簡"]),
    ("Howie Lee feat. 老丹", "Howie Lee", ["老丹"]),
    ("FM3 and Various Artists", "FM3", ["FM3"]),
])
def test_split_name(raw, primary, must_contain):
    got, aliases = da.split_name(raw)
    assert got == primary
    for want in must_contain:
        assert want in aliases


@pytest.mark.parametrize("raw", ["Carsick Cars", "Dear Eloise", "Sleeping Dogs",
                                 "Ts Bayandalai", "Muscle Snog"])
def test_latin_band_names_are_never_split_on_whitespace(raw):
    """The bug this guards: 'Carsick Cars' -> alias 'Cars' hands search_artist a
    one-word query that matches an unrelated band."""
    primary, aliases = da.split_name(raw)
    assert primary == raw
    assert aliases == [raw]


def test_split_name_rejects_unusable_billing():
    assert da.split_name("&")[0] is None
    assert da.split_name("")[0] is None


def test_embeds_reads_substack_data_attrs():
    attrs = {"url": "https://seippelabel.bandcamp.com/album/as-time-goes-by",
             "title": "《时光所至》(As Time Goes By), by 胡格吉乐图",
             "author": "Seippelabel", "is_album": True}
    body = ('<div data-attrs="%s" data-component-name="BandcampToDOM">'
            % html.escape(json.dumps(attrs, ensure_ascii=False), quote=True))
    got = da.embeds(body, "BandcampToDOM")
    assert len(got) == 1
    release, artist = got[0]["title"].rsplit(", by ", 1)
    assert artist == "胡格吉乐图"
    assert release == "《时光所至》(As Time Goes By)"
    assert da.embeds(body, "Youtube2ToDOM") == []


def test_embeds_survives_malformed_json():
    body = '<div data-attrs="{not json}" data-component-name="BandcampToDOM">'
    assert da.embeds(body, "BandcampToDOM") == []
