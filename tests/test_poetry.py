"""诗词彩蛋：本地公版联句库的匹配规则、数据完整性与界面呈现。

这个功能最容易出的两种事故，都被这里盯着：
  1. 张冠李戴（把别人的句子记给某位诗人）——用户看不出来，所以我们只收人工核对过的条目，
     并且用测试要求每条都必须带作者与出处；
  2. 一天里同一句出现三遍——天气建议已经被这条反馈打过一次脸。
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tripagent.core.poetry import attach, entry_score, relevant, slot_text  # noqa: E402
from tripagent.models.enums import Category, SlotType  # noqa: E402
from tripagent.models.poi import POIRecord  # noqa: E402
from tripagent.models.plan import DayPlan, Slot, TripPlan  # noqa: E402
from tripagent.models.request import TripRequest  # noqa: E402

LIB = json.loads((ROOT / "data" / "fixtures" / "poetry.json").read_text(encoding="utf-8"))


def _poi(name: str, tags=()) -> POIRecord:
    return POIRecord(
        id=f"fx:{name}", name=name, category=Category.SIGHT, city="长沙",
        lat=28.2, lng=112.9, source="fixture", tags=list(tags),
    )


def _slot(seq: int, title: str, poi_name: str = "", tags=(), type_: SlotType = SlotType.SIGHT) -> Slot:
    return Slot(
        seq=seq, type=type_, title=title,
        poi=_poi(poi_name, tags) if poi_name else None,
        start_minute=600 + seq * 120, end_minute=720 + seq * 120,
    )


def _plan(days: list[list[Slot]]) -> TripPlan:
    req = TripRequest(destination="长沙", days=len(days), start_date=date(2026, 11, 20))
    return TripPlan(
        request=req,
        days=[DayPlan(index=i + 1, date=req.start_date + timedelta(days=i), slots=s) for i, s in enumerate(days)],
    )


CS = [
    {"match": ["爱晚", "岳麓"], "lines": ["停车坐爱枫林晚，霜叶红于二月花"],
     "author": "杜牧", "from": "山行", "note": "爱晚亭的亭名，就取自这一句。"},
    {"match": [], "lines": ["正是江南好风景，落花时节又逢君"],
     "author": "杜甫", "from": "江南逢李龟年", "note": "写于潭州。"},
]


def test_keyword_hit_beats_city_wide_line():
    """"爱晚亭"必须拿到《山行》，不能被城市通用句抢走。"""
    plan = _plan([[_slot(1, "爱晚亭", "爱晚亭"), _slot(2, "太平街散步", "太平街")]])
    assert entry_score(CS[0], "爱晚亭") == 3 and entry_score(CS[1], "爱晚亭") == 1
    assert attach(plan, CS) == 2
    a, b = plan.days[0].slots
    assert a.extras["poem"]["from"] == "山行"
    assert b.extras["poem"]["from"] == "江南逢李龟年"


def test_no_line_repeats_inside_one_trip():
    """用户原话："五天五句一模一样，等于没说"。库用完就留空，绝不重复。"""
    slots = [
        _slot(1, "爱晚亭", "爱晚亭"),
        _slot(2, "岳麓山", "岳麓山"),          # 也命中《山行》，但那句已经用过了 → 落到兜底句
        _slot(3, "某商场", "某商场"),          # 库里再没有相关句子 → 留空
        _slot(4, "某咖啡馆", "某咖啡馆"),
    ]
    plan = _plan([slots])
    assert attach(plan, CS) == 2, "两句库最多挂两句，第四段宁可空着"
    lines = [s.extras["poem"]["lines"][0] for s in plan.all_slots if s.extras.get("poem")]
    assert len(lines) == len(set(lines)), f"同一句出现了两遍：{lines}"
    assert sum(1 for s in plan.all_slots if not s.extras.get("poem")) == 2


def test_transport_and_hotel_slots_are_left_alone():
    """正在坐高铁、正在酒店睡觉的时候冒出一句"晓看红湿处"是很可笑的。"""
    plan = _plan([[_slot(1, "到达", "长沙南站", type_=SlotType.TRANSPORT),
                   _slot(2, "入住", "", type_=SlotType.CHECKIN),
                   _slot(3, "休息缓冲", "", type_=SlotType.REST)]])
    assert attach(plan, CS) == 0
    assert all(not s.extras.get("poem") for s in plan.all_slots)


def test_tags_also_drive_the_match():
    """川剧不在 POI 名里，但在标签里——匹配文本必须包含标签。"""
    entries = [{"match": ["川剧"], "lines": ["此曲只应天上有"], "author": "杜甫", "from": "赠花卿", "note": ""}]
    s = _slot(1, "夜间娱乐", "某会所", tags=["川剧", "变脸"])
    assert "川剧" in slot_text(s)
    plan = _plan([[s]])
    assert attach(plan, entries) == 1


def test_empty_library_means_no_verses_not_invented_ones():
    plan = _plan([[_slot(1, "冰雪大世界", "冰雪大世界")]])
    assert attach(plan, []) == 0
    assert not plan.all_slots[0].extras.get("poem")


# ---------------------------------------------------------------- 数据完整性
def test_every_entry_is_attributable():
    """没有作者或出处的句子，界面上一看就像模型编的——一律不收。"""
    for city, rows in LIB["cities"].items():
        assert rows, f"{city} 是空表"
        generic = [r for r in rows if not (r.get("match") or [])]
        assert len(generic) <= 1, f"{city} 的城市兜底句只能有一句，现在 {len(generic)} 句"
        for r in rows:
            assert r.get("lines") and all(str(x).strip() for x in r["lines"]), f"{city} 有句子为空"
            assert r.get("author"), f"{city}/{r.get('lines')} 没有作者"
            assert r.get("from"), f"{city}/{r.get('lines')} 没有出处"
            assert r.get("note"), f"{city}/{r.get('lines')} 缺「为什么是这句」的说明"
            assert isinstance(r.get("match"), list), f"{city}/{r.get('lines')} 的 match 不是列表"


def test_only_public_domain_authors():
    """近现代作品版权不清（作者去世后 50 年才进入公版），一律不进库。"""
    modern = {"毛泽东", "郭沫若", "老舍", "郁达夫", "徐志摩", "林徽因", "汪国真", "余秋雨"}
    bad = {(c, r.get("author")) for c, rows in LIB["cities"].items() for r in rows
           if str(r.get("author")).split("（")[0] in modern}
    assert not bad, f"疑似仍在版权期：{bad}"


def test_alias_targets_exist():
    for src, target in (LIB.get("aliases") or {}).items():
        assert target in LIB["cities"], f"别名 {src} 指向了不存在的城市 {target}"
        assert src not in LIB["cities"], f"{src} 既是城市又是别名，查表会有歧义"


def test_library_lookup_tolerates_admin_suffixes_and_county_names():
    """用户输"奉节"拿到的是"奉节县"；常熟、庐山是县级市，句子挂在上级城市库里。"""
    from tripagent.providers.poetry import entries_for

    assert len(entries_for(LIB, "奉节县")) == 4          # 朝辞白帝彩云间 那一路
    assert len(entries_for(LIB, "常熟市")) == 3          # 借苏州的库
    assert len(entries_for(LIB, "庐山市")) == 3
    assert entries_for(LIB, "哈尔滨市") == []             # 没有可靠条目 → 空，而不是凑
    assert entries_for(LIB, "") == []


def test_poetry_capability_has_a_chain(settings):
    assert settings.chain("poetry") == ["fixture", "none"] or "fixture" in settings.chain("poetry")


def test_offline_service_attaches_verses(settings):
    """离线全流程也要能挂上句：这是本地库最大的好处（不联网、不烧 token）。"""
    from tripagent.agents.service import TripService

    out = TripService(settings).plan_trip("成都 2天 带一个小孩 11月20日出发")
    poems = [s.extras["poem"] for s in (out.plan.all_slots if out.plan else []) if s.extras.get("poem")]
    assert poems, "成都有库，离线模式也该挂上句"
    firsts = [p["lines"][0] for p in poems]
    assert len(firsts) == len(set(firsts)), f"整趟行程里出现了重复句子：{firsts}"


def test_reason_rewrite_does_not_erase_the_poem():
    """回归：推荐语那一步曾经整块覆盖 slot.extras，把已经挂好的诗句抹掉了。"""
    from tripagent.agents import reasons
    from tripagent.providers.base import Attempt, ChainResult

    class FakeChain:
        def run(self, capability, method, *a, **kw):
            return ChainResult(
                value="爱晚亭|这个时段斜光穿过枫林，两个人坐着聊天，不用赶下一站",
                source="fake",
                attempts=[Attempt(capability, "fake", True, hits=1)],
            )

    entries = [{"match": ["爱晚"], "lines": ["停车坐爱枫林晚"], "author": "杜牧", "from": "山行", "note": "x"}]
    plan = _plan([[_slot(1, "爱晚亭", "爱晚亭")]])
    assert attach(plan, entries) == 1
    reasons.personalize(plan, "情侣约会", "看夜景", FakeChain())
    s = plan.all_slots[0]
    assert s.extras.get("audience_reason"), "推荐语没写上，测试没走到被回归的那一行"
    assert s.extras.get("poem"), "推荐语把 extras 整块覆盖了，诗句被抹掉"


def test_relevant_orders_specific_before_generic():
    text = "爱晚亭 岳麓山"
    assert relevant(CS, text) == [0, 1]
    assert relevant(CS, "完全无关的地方") == [1], "只剩城市兜底句"
    assert relevant(CS, "") == [], "空文本不配拥有句子"
