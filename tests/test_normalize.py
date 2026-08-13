from app import normalize


def test_timestamps_stripped():
    lines = normalize.clean_lines("[00:12.34]月亮代表我的心\n(1:02)你问我")
    assert lines == ["月亮代表我的心", "你问我"]


def test_section_and_credit_lines_removed():
    text = "[Chorus]\n作词: 某人\n混音：某某\n月亮代表我的心"
    assert normalize.clean_lines(text) == ["月亮代表我的心"]


def test_punctuation_stripped_latin_kept():
    assert normalize.clean_lines("我爱你，baby！…《你》") == ["我爱你 baby 你"]


def test_traditional_to_simplified():
    assert normalize.to_simplified("我愛妳") == "我爱你"


def test_zhu_particle_vs_zhu_words():
    # particle 著 -> 着, but 著 stays in zhù words like 著名
    assert normalize.to_simplified("執著地看著著名的原著") == "执着地看着著名的原著"


def test_crlf_and_blank_lines():
    assert normalize.clean_lines("你好\r\n\r\n  \n再见") == ["你好", "再见"]


def test_empty_input():
    assert normalize.clean_lines("") == []
    assert normalize.clean_lines("！！！…") == []
