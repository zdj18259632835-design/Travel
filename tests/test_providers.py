"""Provider 层测试：降级链、解析器、缓存。不打网络。"""

from __future__ import annotations

from datetime import date

import pytest

from tripagent.config import get_settings
from tripagent.models.enums import Category, Interest, Severity
from tripagent.models.poi import POIQuery, POIRecord, RouteLeg
from tripagent.models.request import TripRequest
from tripagent.models.weather import AlarmRecord, DayForecast
from tripagent.providers import Chain
from tripagent.providers.base import Cache, Provider
from tripagent.providers.culture import parse_rows
from tripagent.providers.poi import _amap_to_poi
from tripagent.providers.weather import parse_alarm, select_alarms

from conftest import make_poi  # tests 目录在 sys.path 内

CMA_ROWS = [
    {"id": "51010841600000_20261105", "title": "四川省成都市气象台发布大风黄色预警信号",
     "description": "成都市气象台2026年11月5日发布大风黄色预警信号：预计24小时内平均风力6级以上",
     "effective": "2026/11/05 10:00", "url": "/publish/a.html"},
    {"id": "46000041600000_20261105", "title": "海南省气象台发布海上雷雨大风黄色预警信号",
     "description": "本岛西部海面可能出现10级以上阵风", "effective": "2026/11/05 09:00"},
    {"id": "51010041600000_20261105", "title": "四川省成都市青羊区气象台发布暴雨红色预警信号",
     "description": "3小时降雨量将达100毫米以上", "effective": "2026/11/05 11:00"},
]


def test_chain_degrades_to_fixture_in_offline_mode(city):
    chain = Chain(get_settings())
    res = chain.run("forecast", "fetch_forecast", city, 16, model=DayForecast)
    assert res.source == "fixture", "离线模式必须命中本地兜底而不是报错"
    assert len(res.value) == 16
    assert all(isinstance(f, DayForecast) for f in res.value)
    assert all(a.capability == "forecast" and a.ok for a in res.attempts)


def test_alarm_filter_keeps_only_target_city_scope(city):
    rows = select_alarms(CMA_ROWS, city, "cma_alarm")
    assert rows, "成都的两条预警必须留下"
    assert all("海南" not in a.title for a in rows), "别省预警不能出现在成都结论里"
    red = next(a for a in rows if a.level == "红")
    assert red.alarm_type == "暴雨" and red.severity is Severity.DANGER
    assert rows[0].alarm_type  # 类型必须解析出来，不能只留标题


def test_parse_alarm_extracts_type_and_level():
    assert parse_alarm("成都市气象台发布大风黄色预警信号") == ("大风", "黄")
    assert parse_alarm("海南省气象台发布海上雷雨大风黄色预警信号") == ("海上雷雨大风", "黄")
    assert parse_alarm("贵州省毕节市纳雍县气象台发布大雾黄色预警信号") == ("大雾", "黄")
    t, lv = parse_alarm("某某发布寒潮蓝色预警信号")
    assert (t, lv) == ("寒潮", "蓝")


def test_select_alarms_returns_empty_for_unrelated_city():
    haerbin = type("C", (), {"key": "harbin", "name": "哈尔滨", "adcode": "230100", "lat": 45.8, "lng": 126.5})()
    assert select_alarms(CMA_ROWS, haerbin, "cma_alarm") == []


def test_amap_payload_maps_to_poi_record(city):
    row = {
        "id": "B0FFF9Z1OP", "name": "蜀大侠火锅(春熙店)", "address": "春熙路街道88号",
        "location": "104.081,30.657", "type": "餐饮服务;中餐厅;四川菜(川菜)", "typecode": "050102",
        "adname": "锦江区", "business": {"rating": "4.7", "cost": "128", "opentime": "10:00-02:00", "tag": "麻辣牛肉"},
    }
    rec = _amap_to_poi(row, city, Category.FOOD)
    assert rec and rec.name.startswith("蜀大侠")
    assert rec.rating == 4.7
    assert rec.avg_cost_cents == 12800, "人均必须从元换成分"
    assert rec.lat and rec.lng and rec.lat > 30 and rec.lng > 103
    assert rec.open_from_minute == 600 and rec.open_to_minute == 120, "跨零点营业时间"
    assert rec.source == "amap"


def test_amap_error_payload_is_not_silently_empty(city):
    chain = Chain(get_settings())
    # 没有 key 时 provider 直接标记不可用，不该发出请求
    names = [n for n, _ in chain.providers_for("poi")]
    assert "amap" not in names, "离线模式不该尝试高德"
    inst = chain._instances.get("poi:amap")
    assert inst is None


def test_cache_roundtrip_preserves_models(city):
    """缓存必须能把 pydantic 模型原样读回，否则第二次运行会拿到 dict 而崩在引擎里。"""
    settings = get_settings()
    calls = {"n": 0}

    class _Demo(Provider):
        capability = "demo_cap"

        def fetch(self, c):
            calls["n"] += 1
            return [DayForecast(date=date.today(), text="晴", tmax_c=20.0, tmin_c=10.0, precip_mm=0.0, source="demo")]

    inst = _Demo(settings, Cache(settings))
    import uuid

    key = ("demo_roundtrip", city.key, uuid.uuid4().hex)  # 每次唯一，避免命中上一轮留下的缓存
    ck = inst.cache.key(*key)
    try:
        r1 = inst.cached_call(key, lambda: inst.fetch(city), DayForecast)
        r2 = inst.cached_call(key, lambda: inst.fetch(city), DayForecast)
        assert calls["n"] == 1, "第二次必须命中磁盘缓存"
        assert isinstance(r1[0], DayForecast) and isinstance(r2[0], DayForecast)
        assert r2[0].text == "晴" and r2[0].tmax_c == 20.0
        assert r2[0].date == date.today()
    finally:
        inst.cache.path(ck).unlink(missing_ok=True)


def test_estimated_transit_returns_leg(city):
    chain = Chain(get_settings())
    res = chain.run("transit", "route", city, None, None, "walk")
    assert isinstance(res.value, RouteLeg)
    assert res.value.minutes >= 0
    assert res.source in ("estimated", "fallback")


def test_fixture_poi_search_filters_by_category_and_keyword(city):
    chain = Chain(get_settings())
    res = chain.run("poi", "search", city, POIQuery(city=city.name, category=Category.BAR, limit=10), model=POIRecord)
    if not res.value:
        return  # 该城市还没建 fixture，不算失败（配置 AMAP_KEY 后走真实数据）
    assert all(p.category is Category.BAR for p in res.value)
    assert all(p.lat and p.lng for p in res.value), "fixture 必须有坐标，否则通勤与路线图失效"
    assert all(p.source == "fixture" for p in res.value)


def test_pick_lodging_survives_hotels_without_price(city):
    """高德 business.cost 经常缺失；曾经因为"改推最便宜"筛成空列表而 IndexError。"""
    from tripagent.agents.service import TripService
    from tripagent.providers.base import ChainResult

    svc = TripService(get_settings())
    no_price = [
        POIRecord(id="a", name="无价酒店A", category=Category.HOTEL, lat=30.57, lng=104.06, rating=4.7),
        POIRecord(id="b", name="无价酒店B", category=Category.HOTEL, lat=30.58, lng=104.07, rating=4.5),
    ]
    svc.chain = type("_C", (), {
        "run": lambda self, *a, **k: ChainResult(value=list(no_price), source="amap", attempts=[]),
        "log": [], "settings": svc.settings,
    })()
    req = TripRequest(destination="成都", city_key="chengdu", days=3, budget_cents=200000)
    picked = svc.pick_lodging(city, req, nights=2)
    assert picked is not None, "没有价格数据不该导致崩溃或返回空"
    assert picked.id == "a", "无价格时按评分优先"
    assert "没有可用价格" in svc.lodging_warning, "必须如实告知预算未覆盖住宿"


def test_extract_json_tolerates_real_llm_defects():
    """联网模式下 qwen 的输出形态不稳定，这几种都必须能吃下（零网络复现）。"""
    import pytest

    from tripagent.providers.llm import ProviderError, extract_json

    good = '[{"title":"a"},{"title":"b"}]'
    cases = {
        "裸数组": good,
        "带解释文字": f"以下是结果：\n{good}\n希望有帮助。",
        "markdown 围栏": f"```json\n{good}\n```",
        "尾逗号": '[{"title":"a",},{"title":"b",},]',
        "包一层对象": '{"notes":[{"title":"a"}]}',
        "被截断": '[{"title":"a"},{"title":"b"},{"snip',
    }
    for label, text in cases.items():
        value = extract_json(text)
        assert isinstance(value, (list, dict)), label
    with pytest.raises(ProviderError):
        extract_json("完全没有结构化内容")


def test_enum_alias_lookup_from_chinese_label():
    assert Category("酒吧酒馆") is Category.BAR
    assert Category("酒吧") is Category.BAR          # 口语别名
    assert Interest("夜生活") is Interest.NIGHTLIFE
    assert Severity("警告") is Severity.WARN


def test_alarm_record_severity_mapping():
    a = AlarmRecord(title="t", level="橙", severity=Severity.WARN, alarm_type="台风")
    assert a.is_critical
    b = AlarmRecord(title="t", level="蓝")
    assert not b.is_critical


CULTURE_ROW = (
    "《蜀风雅韵》川剧晚会|演出|每晚19:40开演约90分钟，含变脸吐火，票150到320元|演出|260|"
    "亲子;情侣;长辈;朋友|19:30前到场|是|官方公众号|散场打车难，提前10分钟走|0.8"
)


def test_culture_row_maps_pitfalls_and_confidence_to_right_fields(city):
    """竖线格式一旦错位，界面上就会出现「避坑：0.8」这种把置信度当避坑点的鬼话。"""
    rows = parse_rows(CULTURE_ROW, city)
    assert len(rows) == 1
    rec = rows[0]
    assert rec.pitfalls == ["散场打车难，提前10分钟走"]
    assert rec.confidence == 0.8
    assert rec.need_reservation and rec.reservation_channel == "官方公众号"
    assert rec.best_time == "19:30前到场"
    assert rec.avg_cost_cents == 26000
    assert not rec.district  # 类目文本不是行政区，混进去界面就会显示「演出」


# ---------------------------------------------------------------- 实拍图
def test_name_match_rejects_lookalikes():
    from tripagent.providers.images import name_match

    assert name_match("喀纳斯景区雾凇体验", "喀纳斯景区")
    assert name_match("《蜀风雅韵》川剧晚会", "蜀风雅韵川剧晚会")
    assert not name_match("春熙路清吧", "太古里清吧")
    assert not name_match("", "任意")


def test_amap_photos_upgrades_http_and_dedupes():
    from tripagent.providers.poi import amap_photos

    row = {"photos": [{"url": "http://a.amap.com/1.jpg"}, {"url": "https://a.amap.com/1.jpg"},
                      {"url": "https://a.amap.com/2.jpg"}]}
    assert amap_photos(row) == ["https://a.amap.com/1.jpg", "https://a.amap.com/2.jpg"]
    assert amap_photos({}) == []


def test_sharpness_separates_clear_from_blurred():
    """拉普拉斯方差必须能把锐利图与糊图拉开数量级，否则筛图等于掷骰子。"""
    import io

    import numpy as np
    from PIL import Image, ImageFilter

    from tripagent.providers.images import sharpness

    a = np.zeros((400, 600), dtype=np.uint8)
    a[::17, :] = 255                     # 密集硬边：对焦好的图梯度强
    b = np.asarray(Image.fromarray(a).filter(ImageFilter.GaussianBlur(6)), dtype=np.uint8)
    buf = {}
    for name, arr in (("sharp", a), ("blur", b)):
        f = io.BytesIO()
        Image.fromarray(arr).save(f, format="PNG")
        buf[name] = f.getvalue()

    w1, h1, lap_sharp = sharpness(buf["sharp"])
    w2, h2, lap_blur = sharpness(buf["blur"])
    assert (w1, h1) == (600, 400), (w1, h1)
    assert lap_sharp > lap_blur * 3, (lap_sharp, lap_blur)


def test_filter_poi_photos_drops_blurry_and_small_but_not_unknown():
    from types import SimpleNamespace

    from tripagent.providers.images import filter_poi_photos

    scores = {
        "http://x/sharp.jpg": [1200, 900, 88.0],
        "http://x/blur.jpg": [1200, 900, 2.0],
        "http://x/small.jpg": [200, 150, 90.0],      # 分辨率不够，再锐也铺不满卡片
    }
    chain = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(value=scores, source="amap_search"))
    settings = SimpleNamespace(image_min_edge=420, image_min_lapvar=11.0)
    poi = SimpleNamespace(photos=["http://x/blur.jpg", "http://x/small.jpg",
                                  "http://x/sharp.jpg", "http://x/unknown.jpg"])

    dropped, touched = filter_poi_photos(chain, settings, [poi])

    assert poi.photos[0] == "http://x/sharp.jpg", poi.photos      # 最清晰的排最前
    assert "http://x/unknown.jpg" in poi.photos, poi.photos        # 没量到的不判死刑
    assert "http://x/blur.jpg" not in poi.photos
    assert "http://x/small.jpg" not in poi.photos
    assert (dropped, touched) == (2, 1), (dropped, touched)

    # 离线/取不到分数时必须原样放行，不能把界面弄成空白
    chain0 = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(value={}, source="none"))
    poi2 = SimpleNamespace(photos=["a", "b"])
    assert filter_poi_photos(chain0, settings, [poi2]) == (0, 0)   # 没打分 = 没淘汰
    assert poi2.photos == ["a", "b"]


def test_amap_payload_carries_photos_into_poi_record(city):
    from tripagent.providers.poi import _amap_to_poi

    row = {
        "name": "甲景点", "id": "P1", "location": "104.06,30.57", "type": "风景名胜",
        "typecode": "110200", "adname": "青羊区",
        "photos": [{"title": "外观", "url": "https://aos-comment.amap.com/P1_1.jpg"}],
    }
    rec = _amap_to_poi(row, city, Category.SIGHT)
    assert rec is not None and rec.photos == ["https://aos-comment.amap.com/P1_1.jpg"]


def test_lodging_candidates_respect_budget_and_missing_prices():
    """并发预取住宿口碑时用的是同一个筛选函数，所以它必须与原来串行版一致。"""
    from datetime import date as _d

    from tripagent.core.lodging import pick_candidates
    from tripagent.models.request import Party, TripRequest

    req = TripRequest(raw_text="成都 3天", destination="成都", city_key="chengdu",
                      days=3, start_date=_d.today(), budget_cents=300000, party=Party(adults=4))
    cheap = make_poi(1, "便宜酒店", Category.HOTEL, 30.57, 104.06, avg_cost_cents=18000)
    pricey = make_poi(2, "贵酒店", Category.HOTEL, 30.57, 104.06, avg_cost_cents=120000)
    cands, cap, warn = pick_candidates([pricey, cheap], req, 2)
    assert cap > 0 and cheap in cands and pricey not in cands and warn == ""

    no_price = make_poi(3, "无价酒店", Category.HOTEL, 30.57, 104.06)
    cands2, cap2, warn2 = pick_candidates([no_price], req, 2)
    assert cands2 == [no_price] and "没有可用价格" in warn2


def test_reason_lines_accept_full_width_separator():
    """模型会抄样例里的全角 ｜，只认半角 | 就会把 16 条推荐语丢掉 10 条。"""
    from tripagent.agents.reasons import parse_reason_lines

    raw = "1. 天后宫|上午排队耗体力，建议阴天前往\n2. 洛阳桥｜傍晚退潮海风大，长辈备好外套"
    table = parse_reason_lines(raw)
    assert set(table) == {"天后宫", "洛阳桥"}, table


# ---------------------------------------------------------------- 模型额度护栏
def test_usage_ledger_counts_persists_and_blocks(tmp_path):
    from types import SimpleNamespace

    from tripagent.providers.usage import UsageLedger, usage_of

    p = tmp_path / "u.json"
    led = UsageLedger(p)
    assert led.tokens_today() == 0 and led.calls_today() == 0
    led.record(prompt=1000, completion=400, reasoning=0)
    assert led.tokens_today() == 1400 and led.calls_today() == 1
    assert not led.over_budget(2000)
    assert led.over_budget(1000)
    assert not led.over_budget(0), "0 必须表示不设限，否则本地开发会被莫名卡住"
    # 重新打开文件必须还在：创空间可能多实例，台账是唯一的公共账本
    assert UsageLedger(p).tokens_today() == 1400

    r = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=1200, completion_tokens=360,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=11000)))
    assert usage_of(r) == (1200, 360, 11000), "思考 token 不单独拆出来，账就少算一大截"
    assert usage_of({"usage": {"prompt_tokens": 5, "completion_tokens": 7}}) == (5, 7, 0)
    assert usage_of(None) == (0, 0, 0)


def test_llm_refuses_when_daily_token_budget_exhausted(tmp_path):
    from tripagent.config import Settings
    from tripagent.providers.base import Cache, ProviderError
    from tripagent.providers.llm import DashScope
    from tripagent.providers.usage import UsageLedger

    s = Settings(dashscope_api_key="sk-test", llm_daily_token_budget=1000,
                 data_dir=tmp_path, cache_dir=tmp_path / "c",
                 output_dir=tmp_path / "o", fixture_dir=tmp_path / "f")
    s.ensure_dirs()
    UsageLedger(s.usage_path).record(prompt=2000)
    with pytest.raises(ProviderError, match="额度"):
        DashScope(s, Cache(s)).chat([{"role": "user", "content": "hi"}])
