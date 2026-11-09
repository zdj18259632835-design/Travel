"""意图解析回归测试：这些用例就是二次开发时的"改文案不跑偏"保险。"""

from __future__ import annotations

from datetime import date

from tripagent.core import intent

TODAY = date(2026, 11, 5)  # 周四


def test_parse_days_and_city():
    req = intent.parse("成都 3天 11月20日出发 爱吃火锅")
    assert req.destination == "成都" and req.city_key == "chengdu"
    assert req.days == 3
    assert req.start_date == date(2026, 11, 20)
    assert intent.Interest.FOOD in req.interests


def test_cn_numbers_and_multi_night():
    assert intent.parse_days("五天四晚") == 5
    assert intent.parse_days("10 天") == 10
    assert intent.parse_days("两周") is None


def test_date_in_text_is_not_mistaken_for_days():
    req = intent.parse("杭州 2026年12月25日 待2天")
    assert req.days == 2, "「25日」不能被当成 25 天"
    assert req.start_date == date(2026, 12, 25)


def test_relative_dates():
    assert intent.parse_date("下周六出发", TODAY) == date(2026, 11, 14)
    assert intent.parse_date("本周五", TODAY) == date(2026, 11, 6)
    assert intent.parse_date("明天", TODAY) == date(2026, 11, 6)
    assert intent.parse_date("国庆", TODAY) == date(2027, 10, 1)  # 已过则顺延一年


def test_per_head_budget_scales_with_party():
    req = intent.parse("重庆 3天 人均预算1500 三个朋友一起")
    assert req.party.head_count == 3
    assert req.budget_cents == 1500 * 100 * 3
    req2 = intent.parse("重庆 3天 预算3000元")
    assert req2.budget_cents == 3000 * 100


def test_party_and_care_flags():
    req = intent.parse("西安 4天 带爸妈 还有一只狗")
    assert req.party.elders >= 2 and req.party.pets == 1
    req2 = intent.parse("南京 2天 亲子遛娃 坐轮椅的爷爷")
    assert req2.party.kids == 1 and req2.party.accessible


def test_nightlife_and_pace():
    assert intent.parse("长沙 2天 想蹦迪").nightlife is True
    assert intent.parse("长沙 2天 不要夜生活 早点回酒店").nightlife is False
    assert intent.parse("成都 3天 特种兵式").pace == "packed"
    assert intent.parse("成都 3天 度假躺平").pace == "relaxed"


def test_must_visit_and_avoid():
    req = intent.parse("成都 3天 一定要去武侯祠，不想去宽窄巷子")
    assert "武侯祠" in req.must_visit
    assert "宽窄巷子" in req.avoid


def test_detect_query_category_and_prefs():
    city, cat, prefs = intent.detect_query("成都有哪些评分高、性价比好的清吧？")
    assert city == "成都"
    assert cat is intent.Category.BAR
    assert prefs.get("high_rating") and prefs.get("cheap")
    _, cat2, _ = intent.detect_query("哈尔滨适合亲子的室内玩乐")
    assert cat2 in (intent.Category.AMUSEMENT, intent.Category.MUSEUM, None)


def test_city_candidates_keeps_zhou_suffix_cities():
    """动态地理编码的输入候选：绝不能把「潮州」削成「潮」。"""
    assert "潮州" in intent.city_candidates("潮州 2天 11月20日出发 预算1500")
    assert "广州" in intent.city_candidates("广州 3天 想去珠江新城")
    assert "苏州" in intent.city_candidates("苏州 2天 园林")
    assert "北京" in intent.city_candidates("北京市 3天")          # 剥掉「市」但保留主体
    assert not any(w in intent.city_candidates("帮我安排一下预算和推荐") for w in ("帮我", "预算", "推荐"))


def test_origin_city():
    assert intent.parse("从上海出发去成都 4天").origin_city == "上海"
