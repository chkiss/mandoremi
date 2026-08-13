"""The frozen /artists article.

The page's numbers are pinned in data/leaderboard_snapshot.json rather than
recomputed, because the prose around them makes checkable claims (see
tools/snapshot_leaderboard.py). These tests guard the two ways that can go
wrong: a snapshot that no longer renders, and a snapshot whose links point at
artist pages the corpus no longer has.
"""
import json
import os

import pytest

from app import public


SNAP = os.path.join(public.DATA_DIR, "leaderboard_snapshot.json")
snapshot_exists = pytest.mark.skipif(
    not os.path.exists(SNAP), reason="no snapshot checked in")


@pytest.fixture
def snap():
    public._SNAPSHOT.update({"loaded": False, "data": None})
    yield public.snapshot()
    public._SNAPSHOT.update({"loaded": False, "data": None})


@snapshot_exists
def test_snapshot_has_every_field_the_article_renders(snap):
    f = snap["figures"]
    assert snap["corpus"]["songs"] > 0
    for key in ("easiest", "hardest", "learnable", "thin", "levels",
                "idioms", "gap_single", "gap_late", "mentions"):
        assert f[key], key
    assert len(snap["featured"]) >= 20
    # Every artist the prose names must have a label, or the sentence renders
    # a bare slug at a reader.
    for slug, label in f["mentions"].items():
        assert label and label != slug


@snapshot_exists
def test_frozen_article_renders_with_no_database(snap, monkeypatch):
    """The whole point of freezing: the page owes nothing to live data."""
    def boom(*a, **k):
        raise AssertionError("/artists must not read live data")

    monkeypatch.setattr(public, "data", boom)
    html = public.leaderboard_html("https://mandoremi.com", "t")
    assert "Which Chinese artists are easiest to learn from?" in html
    assert snap["figures"]["easiest"][0]["label"] in html
    # Not chengyu: that section moved out to the Core words article. What the
    # /artists page keeps is the vocabulary block and its bridge forward.
    assert "The vocabulary that songs use that HSK doesn" in html
    assert "More on this coming soon!" in html
    # The long table ships collapsed.
    assert "<details class=\"fulltable\">" in html
    for a in snap["featured"]:
        assert f'/artist/{a["slug"]}' in html


# Whether every artist the article links to still EXISTS is a question about
# the live corpus, which only the server has. That check lives in
# tools/check_leaderboard_drift.py, run where the database is.


@snapshot_exists
def test_snapshot_is_valid_json_and_utf8():
    with open(SNAP, encoding="utf-8") as f:
        json.load(f)


def test_missing_snapshot_falls_back_to_live(monkeypatch, tmp_path):
    monkeypatch.setattr(public, "SNAPSHOT_PATH", str(tmp_path / "nope.json"))
    monkeypatch.setattr(public, "_SNAPSHOT", {"loaded": False, "data": None})
    assert public.snapshot() is None
