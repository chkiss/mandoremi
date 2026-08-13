"""Crawler and link-preview surface.

This app's audience is reached by pasting links into Reddit, Discord and HN, so
a missing og: tag is a real acquisition cost, not a nicety.

The /sitemap.xml and /robots.txt routes are wired by app/public_pages.py, which
is intentionally gitignored (local-only) and absent on a fresh clone. Skip the
two tests that depend on it so `pytest` is green out of the box; they still run
where the file is present (the live/dev checkout).
"""
import importlib.util
import re

import pytest

HAS_PUBLIC_PAGES = importlib.util.find_spec("app.public_pages") is not None
SKIP_PUBLIC = pytest.mark.skipif(
    not HAS_PUBLIC_PAGES,
    reason="app/public_pages.py is gitignored/local-only; routes absent on a clean clone")


@SKIP_PUBLIC
def test_robots_allows_public_pages_and_blocks_the_api(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    body = r.text
    assert "Disallow: /api/" in body
    assert "Allow: /about" in body
    assert "Sitemap: http" in body
    # the preview card must outrank the /static/ disallow, or link cards break
    assert body.index("Allow: /static/card.png") < body.index("Disallow: /static/")


@SKIP_PUBLIC
def test_sitemap_lists_only_public_pages(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    locs = re.findall(r"<loc>(.*?)</loc>", r.text)
    # The static public pages, plus one entry per artist in the seed corpus
    # (none in a fresh test database).
    assert {u.rsplit("/", 1)[-1] or "/" for u in locs} >= {"/", "about", "artists"}
    assert all(u.startswith("http") for u in locs)
    assert not any("/api" in u for u in locs)
    # Nothing behind a session may be advertised to crawlers.
    assert not any(re.search(r"/(api|settings|playlists?)\b", u) for u in locs)


def test_index_has_link_preview_tags(client):
    html = client.get("/").text
    for tag in ('property="og:title"', 'property="og:description"',
                'property="og:image"', 'property="og:url"',
                'name="twitter:card"', 'rel="canonical"',
                'name="description"'):
        assert tag in html, f"index.html lost {tag}"


def test_about_has_link_preview_tags(client):
    html = client.get("/about").text
    for tag in ('property="og:title"', 'property="og:image"', 'rel="canonical"'):
        assert tag in html, f"about.html lost {tag}"


def test_card_image_is_served(client):
    r = client.get("/static/card.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert len(r.content) > 5000, "card.png looks like a placeholder"
