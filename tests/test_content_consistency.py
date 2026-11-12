"""文案-排期-图片三者的互相一致性：本轮用户实测暴露的问题都在这里钉住。

用户原话浓缩成三条：
① 中午写"避开正午暴晒"，下面排露天野餐；
② 下午写"适合洞穴、博物馆"，配图是阳光强烈的开阔园区；
③ 文案里"摩天轮""红树林与海鸟"这些景物，条目里根本没有——是模型编的。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tripagent.agents import reasons  # noqa: E402
from tripagent.core.light import bias, light_level  # noqa: E402
from tripagent.core.schedule_note import schedule_note  # noqa: E402
from tripagent.models.enums import Category, SlotType  # noqa: E402
from tripagent.models.poi import POIRecord  # noqa: E402
from tripagent.models.weather import DayForecast  # noqa: E402

NOON = 12 * 60


def _f(**kw) -> DayForecast:
    base = dict(date=date(2026, 11, 20), text="晴", tmin_c=23.0, tmax_c=31.0,
                precip_mm=0.0, uv_index=9.0, sunset="17:40", source="open_meteo")
    base.update(kw)
    return DayForecast(**base)


class _Slot:
    def __init__(self, name, indoor, start=NOON, type_=SlotType.SIGHT):
        self.poi = POIRecord(id=name, name=name, category=Category.PARK, city="三亚",
                             lat=18.2, lng=109.5, indoor=indoor, source="fixture")
        self.title = name
        self.indoor = indoor
        self.start_minute = start
        self.type = type_

    @property
    def band(self) -> str:
        from tripagent.models.common import time_band

        return time_band(self.start_minute)


# ---------------------------------------------------------------- 光照规则
def test_hard_light_demotes_outdoor_and_promotes_indoor():
    f = _f()
    assert light_level(NOON, f) == "硬光"
    d_out, r_out = bias(False, NOON, f)
    d_in, r_in = bias(True, NOON, f)
    assert d_out == -25.0 and "紫外线" in r_out
    assert d_in == 20.0 and "室内" in r_in
    # 理由必须能给用户看：它带着当日事实，不是"因为规则如此"
    assert "9" in r_out


def test_soft_light_before_sunset_favors_outdoor():
    f = _f(uv_index=3.0, tmax_c=24.0)
    assert light_level(17 * 60 + 10, f) == "柔光"
    assert bias(False, 17 * 60 + 10, f)[0] == 15.0


def test_after_sunset_outdoor_is_demoted():
    f = _f(uv_index=0.0, tmax_c=22.0)
    d, why = bias(False, 19 * 60, f)
    assert d == -20.0 and "日落" in why


def test_rainy_day_does_not_claim_hard_light():
    f = _f(text="中雨", uv_index=2.0, tmax_c=26.0)
    assert light_level(NOON, f) == "阴雨"
    assert bias(False, NOON, f)[0] == -15.0


def test_no_forecast_means_no_fake_confidence():
    assert light_level(NOON, None) == "未知"
    assert bias(False, NOON, None) == (0.0, "")


# ---------------------------------------------------------------- 时段说明
def test_band_note_stops_claiming_sun_avoidance_when_outdoor_is_placed():
    """"避开正午暴晒"与实际排了露天同时出现，就是用户截图里那条自相矛盾。"""
    slots = [_Slot("三亚市丰兴隆党群主题公园", False)]
    txt = schedule_note("中午", slots, _f(), "避开正午暴晒与饭点人流，适合安排吃饭或回酒店歇脚")
    assert "避开正午暴晒与饭点人流，适合安排吃饭" not in txt
    assert "紫外线" in txt and "遮阳" in txt
    assert "三亚市丰兴隆党群主题公园" in txt      # 说明必须关于这一档实际排了什么
    assert "对调" in txt                          # 给出可执行的缓解办法，而不是空话


def test_band_note_keeps_generic_when_it_is_consistent():
    slots = [_Slot("海南省博物馆", True)]
    txt = schedule_note("中午", slots, _f(), "避开正午暴晒与饭点人流，适合安排吃饭或回酒店歇脚")
    assert "避开正午暴晒" in txt
    assert "室内 1 处" in txt


def test_band_note_without_forecast_does_not_invent_weather():
    slots = [_Slot("步行街", False)]
    txt = schedule_note("中午", slots, None, "避开正午暴晒")
    assert "℃" not in txt and "紫外线" not in txt


# ---------------------------------------------------------------- 推荐语
def test_prompt_now_supplies_the_material_and_bans_invention():
    """上一版要求"写具体可感的点"，却不给景物原料，模型只能编。"""
    p = reasons.REASON_PROMPT
    assert "只允许使用条目里给出的事实" in p
    assert "摩天轮" in p and "木栈道" in p          # 反例必须写进提示词才有效
    assert "100-160 字" in p
    assert "第一句点明这是什么活动" in p


def test_row_lines_carry_type_band_indoor_and_weather():
    from tripagent.agents.service import TripService
    from tripagent.config import reload_settings

    out = TripService(reload_settings()).plan_trip("成都 2天 11月20日出发 情侣")
    rows = reasons._row_lines(out.plan)
    assert rows
    for r in rows:
        parts = r.split("|")
        assert len(parts) >= 7, f"喂给模型的行缺字段：{r}"
        assert parts[2] in ("清晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "深夜")
        assert parts[3] in ("室内", "户外")


def test_fallback_reason_reaches_100_chars_even_for_sparse_poi():
    """数据再少也要够 100 字——补的是排期事实，不是形容词。"""
    poi = POIRecord(id="s", name="某社区公园", category=Category.PARK, city="三亚",
                    lat=18.2, lng=109.5, source="fixture")
    s = _Slot("某社区公园", False, start=15 * 60)
    s.poi = poi
    txt = reasons.fallback_reason(poi, "长辈同行", s, "晴24~30℃、紫外线 8")
    assert len(txt) >= 100, (len(txt), txt)
    assert reasons.acceptable(txt)[0], reasons.acceptable(txt)
    for banned in reasons._EMPTY_WORDS:
        assert banned not in txt


def test_acceptable_rejects_short_and_empty_talk():
    """长度与空话是两条独立的驳回理由，都要能挡住模型的凑字数写法。"""
    long_empty = ("这个地方很有特色，值得一去，不容错过，"
                  "是很多人都会喜欢的安排，去了不会有遗憾，也能拍出好看的照片留念。") * 2
    ok, why = reasons.acceptable(long_empty)
    assert not ok and "空话" in why
    ok, why = reasons.acceptable("短句。")
    assert not ok and "太短" in why
    ok, why = reasons.acceptable("好" * 400)
    assert not ok and "太长" in why
    real = ("这是一处特色餐饮，安排在中午。本地人常去的海鲜大排档，按斤计价，"
            "当日晴23~29℃、紫外线偏强，室内受天气影响小，正好避开正午暴晒；"
            "人均约 ¥350，需提前预约，先问清单价再下单，加工费另算，别只看总价。")
    assert len(real) >= 100, len(real)
    ok, why = reasons.acceptable(real)
    assert ok, why


def test_fallback_reason_is_factual_and_long_enough():
    """没有大模型时也必须给足 100 字以上，而且只用已核实字段。"""
    poi = POIRecord(
        id="x", name="林姐香味海鲜", category=Category.FOOD, city="三亚", lat=18.2, lng=109.5,
        rating=4.6, avg_cost_cents=35000, why="本地人常去的海鲜大排档，按斤计价",
        pitfalls=["先问清单价再下单", "加工费另算"], best_time="18:00-20:00",
        open_hours_raw="10:00-22:00", need_reservation=True, indoor=True, source="fixture",
    )
    s = _Slot("林姐香味海鲜", True, type_=SlotType.MEAL)
    s.poi = poi
    txt = reasons.fallback_reason(poi, "情侣约会", s, "晴23~29℃、紫外线 9")
    assert len(txt) >= 100, (len(txt), txt)
    for banned in reasons._EMPTY_WORDS:
        assert banned not in txt
    assert "室内" in txt and "¥350" in txt and "需预约" in txt


# ---------------------------------------------------------------- 预报覆盖
def test_qweather_yields_when_it_cannot_cover_the_window(monkeypatch, settings):
    """和风只给 7 天却排在 11/20 的行程之前 → 必须让位给 16 天的源。

    这是"中午该避暑却排了野餐"的最底层原因：行程日期一天预报都没有，
    光照规则与文案都拿不到事实，只能沉默。
    """
    from tripagent.knowledge.cities import get_city
    from tripagent.providers.base import Cache, ProviderError
    from tripagent.providers.weather import QWeather

    s = settings.model_copy(update={"qweather_key": "K", "qweather_host": "h.example"})
    prov = QWeather(s, Cache(s))
    monkeypatch.setattr(prov, "_location_id", lambda city: "101270101")

    def fake_get_json(url, params=None, headers=None, timeout=None):
        if "geo" in url:
            return {"location": [{"id": "101270101"}]}
        day0 = date(2026, 11, 12)
        return {"daily": [{"fxDate": (day0 + timedelta(days=i)).isoformat(),
                           "textDay": "晴", "tempMax": "29", "tempMin": "22",
                           "uvIndex": "7"} for i in range(7)]}

    monkeypatch.setattr(prov, "get_json", fake_get_json)
    with pytest.raises(ProviderError, match="覆盖不了"):
        prov.fetch_forecast(get_city("sanya"), 13)


# ---------------------------------------------------------------- 无图不进候选
def test_prepare_photos_drops_candidates_without_qualified_images(monkeypatch, settings):
    """入池就等于"有合格图"：质检也在装配前做完，不留"装配后变成 0 图"的口子。"""
    from tripagent.agents.service import TripService
    from tripagent.knowledge.cities import get_city
    from tripagent.models.enums import Category
    from tripagent.providers.base import ChainResult

    svc = TripService(settings)
    pool = {
        Category.SHOW: [
            POIRecord(id="a", name="三亚千古情景区", category=Category.SHOW, city="三亚",
                      lat=18.3, lng=109.6, photos=["https://x/blur.jpg"], source="qwen_search"),
            POIRecord(id="b", name="有图的剧场", category=Category.SHOW, city="三亚",
                      lat=18.3, lng=109.6, photos=["https://x/good.jpg"], source="fixture"),
        ],
        Category.FOOD: [POIRecord(id="c", name="没图的餐馆", category=Category.FOOD,
                                  city="三亚", lat=18.3, lng=109.6)],
    }
    scores = {
        "https://x/good.jpg": [1600, 1200, 900.0, 0.05, 0.4, 0.2],
        "https://x/blur.jpg": [1600, 1200, 1.0, 0.05, 0.4, 0.2],   # 方差过低 → 淘汰
    }
    monkeypatch.setattr(svc.chain, "run",
                        lambda *a, **k: ChainResult(value=scores, source="fake"))
    out = type("O", (), {"trace": []})()
    dropped = svc.prepare_photos(get_city("sanya"), pool, out)
    assert dropped == 1, "唯一那张图是糊图，等于没有合格图"
    assert [p.name for p in pool[Category.SHOW]] == ["有图的剧场"]
    assert pool[Category.FOOD], "餐饮不强制有图，否则会出现没饭吃的行程"
    assert any("预筛" in t for t in out.trace)
