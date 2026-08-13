import pytest

from app import playlist_import as pi


def test_dispatch_rejects_unknown_url():
    with pytest.raises(pi.PlaylistImportError):
        pi.fetch("https://example.com/playlist/123")


def test_parse_netease():
    data = {"result": {"name": "飙升榜", "tracks": [
        {"name": "樱花草", "artists": [{"name": "Sweety"}]},
        {"name": "窝囊废", "artists": [{"name": "刘凤瑶"}]},
    ]}}
    out = pi.parse_netease(data)
    assert out["name"] == "飙升榜"
    assert out["tracks"][0] == {"artist": "Sweety", "title": "樱花草"}
    assert out["capped"] is False
    with pytest.raises(pi.PlaylistImportError):
        pi.parse_netease({"result": {"tracks": []}})


def _yt_item(title):
    return {"lockupViewModel": {"metadata": {"lockupMetadataViewModel": {
        "title": {"content": title}}}}}


def test_parse_youtube():
    data = {
        "contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {
            "content": {"sectionListRenderer": {"contents": [{"itemSectionRenderer": {
                "contents": [_yt_item("周杰伦 - 七里香 (Official Music Video)"),
                             _yt_item("屋顶")]}}]}}}}]}},
        "metadata": {"playlistMetadataRenderer": {"title": "C-pop"}},
    }
    out = pi.parse_youtube(data)
    assert out["name"] == "C-pop"
    assert out["tracks"][0] == {"artist": "周杰伦", "title": "七里香"}  # noise stripped, split
    assert out["tracks"][1] == {"artist": "", "title": "屋顶"}
    assert out["capped"] is False
    with pytest.raises(pi.PlaylistImportError):
        pi.parse_youtube({})


def test_parse_apple():
    page = ('<meta property="og:title" content="Today&#8217;s Hits">'
            '<script type="application/json" id="serialized-server-data">'
            '[{"data": {"sections": [{"items": [{"title": "hate that i made you '
            'love me", "artistName": "Ariana Grande"}]}]}}]</script>')
    out = pi.parse_apple(page)
    assert out["name"] == "Today’s Hits"
    assert out["tracks"] == [{"artist": "Ariana Grande",
                              "title": "hate that i made you love me"}]
    with pytest.raises(pi.PlaylistImportError):
        pi.parse_apple("<html>no data</html>")
