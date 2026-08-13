from app import analyze


def _keys(text):
    return {g["key"] for g in analyze.analyze(text)["grammar"]}


def test_tier1_correlative_pairs():
    assert "xianzai" in _keys("我们先吃饭 然后看电影")
    assert "zhiyao" in _keys("只要你说 我就来")
    assert "zhiyou" in _keys("只有你 我才快乐")
    assert "chule" in _keys("除了你以外 我什么都不要")
    assert "bushijiushi" in _keys("不是你哭 就是我哭")
    assert "yuqi" in _keys("与其等待 不如离开")


def test_tier2_regex_patterns():
    assert "yijiu" in _keys("我一看到你就笑")
    assert "anota" in _keys("你爱不爱我")
    assert "anota" in _keys("你有没有想过我")
    assert "potential" in _keys("我听不懂你的话")
    assert "potential" in _keys("夜里睡不着")
    assert "vwan" in _keys("说完这句话")
    assert "directional" in _keys("我们唱起来")
    assert "imminent" in _keys("天快亮了")
    assert "finalpart" in _keys("你还爱我吗")
    assert "bi" in _keys("我比你更爱他")
    assert "dehua" in _keys("你走的话 我也走")


def test_reduplication():
    assert "redup" in _keys("慢慢地走")
    # lexical AA words are not grammatical reduplication
    assert "redup" not in _keys("谢谢妈妈")


def test_tier3_separable_verbs():
    assert "separable" in _keys("只想见你一面")       # 见面 split
    assert "separable" in _keys("睡了一觉醒来")       # 睡觉 split
    assert "separable" in _keys("你伤透了我的心")     # 伤心 split
    assert "separable" in _keys("帮个忙好吗")         # 帮忙 split
    assert "separable" not in _keys("我们见面吧")     # contiguous = not split
    assert "separable" not in _keys("我看见画面")     # 看见 + 画面, lexical
    assert "separable" not in _keys("站在我面前")     # 面前 is a word


def test_tier3_other_patterns():
    assert "youdian" in _keys("我有点想你")
    assert "yidianr" in _keys("请你走慢一点")
    assert "yidianr" not in _keys("我有一点想你")     # counted as 有点, not adj+一点
    assert "yidianr" not in _keys("尝了一点甜头")     # 一点+noun = quantity, not comparative
    assert "yidianbu" in _keys("我一点都不难过")
    assert "separable" not in _keys("伤悲凄美心碎")   # 伤悲 opens a compound, not 伤心 split
    assert "meiyouname" in _keys("没有人像你这么好")
    assert "nandao" in _keys("难道你不懂")
    assert "buru" in _keys("不如忘了他")


def test_false_positive_guards():
    assert "potential" not in _keys("我不了解你")   # 不了 in 不了解
    assert "potential" not in _keys("别不着急")     # 不着 in 不着急
    assert "potential" not in _keys("相爱着不懂悲伤")  # 着 is not a verb here
    assert "vcuo" not in _keys("你唱得不错")        # 不错 = "not bad"
    assert "vcuo" not in _keys("我们又一次错过")    # 错过 = to miss
    assert "vcuo" not in _keys("都是我的错")        # 错 as noun
    assert "vwan" not in _keys("完全没问题")        # 完全 is a word
    assert "directional" not in _keys("回到过去")   # 过去 = the past (noun)
    assert "vcuo" in _keys("我爱错了人")            # real V错 still detected
