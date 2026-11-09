"""规则引擎测试：天气风险、行程装配、约束校验。全部确定性、不联网。"""

from __future__ import annotations

from datetime import date, timedelta

from tripagent.config import get_settings
from tripagent.core import validator, weather_risk
from tripagent.core.planner import ItineraryPlanner
from tripagent.core.scoring import ScoreContext, score
from tripagent.models.enums import Category, Interest, Severity
from tripagent.models.request import Party, TripRequest
from tripagent.models.weather import AlarmRecord, CityWeather, DayForecast
from tripagent.providers import Chain

from conftest import make_weather  # tests 目录在 sys.path 内


def _chain():
    return Chain(get_settings())


# ---------------------------------------------------------------- 天气风险
def test_heavy_rain_day_is_indoor_biased():
    w = make_weather(date.today(), 5, rain_day=2)
    out = weather_risk.assess(w, [f.date for f in w.forecast])
    risky = w.risk_on([f.date for f in w.forecast][2])
    assert risky is not None and risky.prefer_indoor
    assert risky.severity.rank >= Severity.WARN.rank
    assert out["overall"].rank >= Severity.WARN.rank


def test_red_alarm_blocks_travel_and_suggests_new_window():
    w = make_weather(date.today() + timedelta(days=1), 5)
    target = w.forecast[1].date
    w.alarms = [
        AlarmRecord(
            title="成都市气象台发布暴雨红色预警信号",
            alarm_type="暴雨",
            level="红",
            severity=Severity.DANGER,
            area="成都",
            text=f"成都市气象台{target:%Y年%m月%d日}发布暴雨红色预警信号：12小时降雨量将达100毫米以上",
            source="fixture",
        )
    ]
    out = weather_risk.assess(w, [f.date for f in w.forecast])
    assert out["blocking_advice"], "红色预警必须给出「不建议出行」结论"
    assert "不建议" in out["blocking_advice"]
    assert out["best_dates"], "同时必须给出可改期的日期"


def test_comfort_index_ordering():
    w = make_weather(date.today(), 3, rain_day=1, hot=False)
    c = [weather_risk.comfort_index(f) for f in w.forecast]
    assert c[1] < c[0], "雨天舒适指数必须更低"
    hot = make_weather(date.today(), 2, hot=True)
    assert weather_risk.comfort_index(hot.forecast[0]) < 60, "38℃+雷暴不该是高舒适日"


def test_advice_is_actionable():
    w = make_weather(date.today(), 2, rain_day=0)
    r = weather_risk.assess_day(w.forecast[0], [], party=None)
    assert any("室内" in a or "雨具" in a for a in r.advice)


# ---------------------------------------------------------------- 打分
def test_scoring_prefers_indoor_on_bad_weather_and_flags_avoid():
    from tripagent.models.request import Party, TripRequest

    w = make_weather(date.today(), 2, rain_day=0)
    risk = weather_risk.assess_day(w.forecast[0], [], party=None)
    req = TripRequest(
        destination="成都",
        city_key="chengdu",
        days=2,
        start_date=w.forecast[0].date,
        interests=[Interest.FOOD],
        party=Party(adults=2),
        avoid=["远端"],
    )
    pool = _synthetic_pool()
    ctx = ScoreContext(request=req, forecast=w.forecast[0], risk=risk, remaining_cents=10**9)
    assert score(pool["E馆"], ctx).value > score(pool["A景点"], ctx).value, "恶劣天气日室内项应胜出"
    assert score(pool["D远端景点"], ctx).value < -100, "命中 avoid 的词必须被剔除"


def _synthetic_pool():
    from conftest import make_poi

    rows = [
        make_poi(1, "A景点", Category.SIGHT, 30.570, 104.060, ticket_cents=5000),
        make_poi(3, "C博物馆", Category.SIGHT, 30.580, 104.070, indoor=True, ticket_cents=0),
        make_poi(5, "E馆", Category.MUSEUM, 30.572, 104.062, indoor=True),
        make_poi(4, "D远端景点", Category.SIGHT, 30.700, 104.300, ticket_cents=80000, tags=["远端"]),
    ]
    return {p.name: p for p in rows}


# ---------------------------------------------------------------- 装配
def test_plan_has_slots_for_every_day(poi_pool, city, request_basic):
    chain = _chain()
    w = make_weather(request_basic.start_date, request_basic.days + 3)
    weather_risk.assess(w, [f.date for f in w.forecast][: request_basic.days])
    planner = ItineraryPlanner(city, request_basic, w, chain, get_settings())
    plan = planner.plan(poi_pool, lodging=None)
    assert len(plan.days) == request_basic.days
    assert all(len(d.slots) >= 1 for d in plan.days)
    titles = [s.title for d in plan.days for s in d.slots]
    assert len(titles) == len(set(titles)), "同一地点不得在多天重复"
    assert any(s.type.label in ("夜间娱乐", "观看演出") for s in plan.all_slots), "夜生活诉求应被安排"


def test_budget_cap_is_respected(poi_pool, city, request_basic):
    chain = _chain()
    w = make_weather(request_basic.start_date, request_basic.days + 3)
    weather_risk.assess(w, [f.date for f in w.forecast][: request_basic.days])
    request_basic.budget_cents = 20000  # ¥200，几乎只够免费景点
    plan = ItineraryPlanner(city, request_basic, w, chain, get_settings()).plan(poi_pool)
    assert plan.total_cost_cents <= request_basic.budget_cents
    paid = [s for s in plan.all_slots if s.cost_cents > 0]
    assert all(s.cost_cents <= request_basic.budget_cents for s in paid)


def test_rainy_day_gets_indoor_swaps(poi_pool, city, request_basic):
    """预警日的契约：室内候选优先用尽，最多保留 1 项户外（再多就该被 weather_adapted 判失败）。"""
    chain = _chain()
    start = request_basic.start_date
    w = make_weather(start, request_basic.days, rain_day=1)
    window = [start + timedelta(days=i) for i in range(request_basic.days)]
    weather_risk.assess(w, window)
    plan = ItineraryPlanner(city, request_basic, w, chain, get_settings()).plan(poi_pool)
    day2 = plan.days[1]
    assert day2.risk and day2.risk.prefer_indoor

    sight_slots = [s for s in day2.slots if s.type.label == "游览"]
    outdoor = [s for s in sight_slots if not s.indoor]
    assert len(outdoor) <= 1, f"预警日户外项应 ≤1，实际 {[s.title for s in outdoor]}"
    assert any(s.indoor for s in sight_slots), "室内候选必须被优先安排"

    report = validator.validate(plan, chain, get_settings())
    weather_check = next(c for c in report.checks if c.id == "weather_adapted")
    assert weather_check.passed, "至多 1 项户外的雨天方案不应被判为未适配天气"


# ---------------------------------------------------------------- 校验
def test_scorecard_reports_three_dimensions(poi_pool, city, request_basic):
    chain = _chain()
    w = make_weather(request_basic.start_date, request_basic.days + 3)
    weather_risk.assess(w, [f.date for f in w.forecast][: request_basic.days])
    plan = ItineraryPlanner(city, request_basic, w, chain, get_settings()).plan(poi_pool)
    report = validator.validate(plan, chain, get_settings())
    card = report.scorecard()
    assert set(card) == {"env", "commonsense", "hard", "overall"}
    ids = {c.id for c in report.checks}
    assert {"budget_within", "no_duplicate_poi", "weather_adapted", "kid_safe_venues"} <= ids
    assert 0 <= report.pass_rate() <= 1


def test_kid_constraint_fails_when_bar_scheduled(poi_pool, city, request_basic):
    chain = _chain()
    w = make_weather(request_basic.start_date, request_basic.days)
    weather_risk.assess(w, [f.date for f in w.forecast][: request_basic.days])
    plan = ItineraryPlanner(city, request_basic, w, chain, get_settings()).plan(poi_pool)
    from tripagent.models.enums import SlotType
    from tripagent.models.plan import Slot
    from conftest import make_poi

    bar = make_poi(99, "违规酒吧", Category.BAR, 30.57, 104.06, requires_id=True)
    plan.days[0].slots.append(Slot(seq=9, type=SlotType.NIGHTLIFE, title=bar.name, start_minute=1260, end_minute=1380, poi=bar))
    request_basic.party.kids = 1
    report = validator.validate(plan, chain, get_settings())
    kid_check = next(c for c in report.checks if c.id == "kid_safe_venues")
    assert not kid_check.passed and kid_check.fix_hint


# ---------------------------------------------------------------- 天气结论的可读性
def _three_days() -> CityWeather:
    """三种明显不同的日子：湿冷大风 / 闷热强紫外线 / 小雨温和。"""
    start = date(2026, 11, 20)
    rows = [
        DayForecast(date=start, wmo_code=0, text="晴", tmax_c=2.0, tmin_c=-6.0,
                    wind_max_kmh=35.0, uv_index=3.0, humidity_max=40, precip_mm=0.0),
        DayForecast(date=start + timedelta(days=1), wmo_code=3, text="阴", tmax_c=31.0, tmin_c=24.0,
                    wind_max_kmh=8.0, uv_index=9.0, humidity_max=88, precip_mm=0.0),
        DayForecast(date=start + timedelta(days=2), wmo_code=61, text="小雨", tmax_c=18.0, tmin_c=15.0,
                    wind_max_kmh=12.0, uv_index=2.0, humidity_max=90, precip_mm=6.0, precip_prob=75),
    ]
    w = CityWeather(city="成都", city_key="chengdu", lat=30.57, lng=104.06, forecast=rows)
    weather_risk.assess(w, [r.date for r in rows], Party(adults=2))
    return w


def test_weather_advice_is_per_day_not_one_template():
    """用户反馈"五天五句一模一样，等于没说"：结论必须由当天要素决定。"""
    w = _three_days()
    verdicts = [weather_risk.day_verdict(f, w.risk_on(f.date)) for f in w.forecast]
    assert len({v["advice"] for v in verdicts}) == 3, verdicts
    assert len({v["tips"] for v in verdicts}) == 3, verdicts
    assert "冷" in verdicts[0]["advice"] or "暖" in verdicts[0]["advice"]
    assert "紫外线" in verdicts[1]["advice"] or "热" in verdicts[1]["advice"]
    assert "上午" in verdicts[2]["advice"]


def test_safety_tips_cover_cold_wind_uv_and_dry_air():
    """保暖/保湿/防晒这些"对身体"的话，正是用户点名要的内容。"""
    w = _three_days()
    t0 = " ".join(weather_risk.safety_tips(w.forecast[0]))
    t1 = " ".join(weather_risk.safety_tips(w.forecast[1]))
    assert "严寒" in t0 and "风大" in t0
    assert "紫外线" in t1 and "水" in t1
    for f in w.forecast:
        assert weather_risk.safety_tips(f), f"{f.date} 一条健康提示都没有"


def test_headline_no_longer_repeats_the_why():
    """旧写法是「多云 · -5~6℃ — 多云，-5~6℃」，同一句话讲两遍。"""
    w = _three_days()
    from tripagent.models.common import temp_span

    for f in w.forecast:
        v = weather_risk.day_verdict(f, w.risk_on(f.date))
        assert temp_span(f.tmin_c, f.tmax_c) not in v["why"], v
        assert "舒适指数" in v["why"], v


# ---------------------------------------------------------------- 预报窗口按需收缩
def test_forecast_window_is_trip_length_plus_buffer():
    """5 天行程不该拿 16 天预报：窗口 = 行程天数 + 2 天缓冲（再加出发前等待）。"""
    from tripagent.agents.service import TripService

    svc = TripService(get_settings())
    req = TripRequest(
        raw_text="成都 5天",
        destination="成都",
        city_key="chengdu",
        adcode="510100",
        days=5,
        start_date=date.today() + timedelta(days=1),
        party=Party(adults=2),
    )
    outcome = svc.plan_trip(req)
    line = next((t for t in outcome.trace if t.startswith("预报窗口")), "")
    assert "8 天" in line, line          # 1 天后出发 + 5 天行程 + 2 天缓冲
    assert "总计" in outcome.timings and "并发取数" in outcome.timings


def test_identical_days_are_reported_as_identical_not_fake_ranked():
    """用户要"别每天都一样"，但解法不能是编造区别。

    五天舒适指数都是 100 时写"这天体感最弱"是撒谎，所以极差 <6 一律不用排名话术。
    """
    start = date(2026, 11, 23)  # 周一
    rows = [
        DayForecast(date=start + timedelta(days=i), wmo_code=3, text="阴",
                    tmax_c=18.0, tmin_c=12.0, precip_mm=0.0, wind_max_kmh=10.0,
                    humidity_max=70, uv_index=2.0)
        for i in range(5)
    ]
    w = CityWeather(city="泉州", city_key="quanzhou", lat=24.9, lng=118.6, forecast=rows)
    weather_risk.assess(w, [r.date for r in rows], Party(adults=2))
    adv = [v["advice"] for v in weather_risk.window_verdicts(w, [r.date for r in rows])["outdoor"]]
    assert len(adv) == 5
    assert not any("最弱" in a or "第 1 好" in a for a in adv), adv
    assert all("其它日子差不多" in a for a in adv), adv


def test_days_that_really_differ_get_ranked_and_sunset_anchor():
    """真有差别时要敢说差别；日落时刻每天不同，是免费且真实的区分点。"""
    start = date(2026, 11, 23)
    rows = [
        DayForecast(date=start, wmo_code=0, text="晴", tmax_c=22.0, tmin_c=12.0,
                    wind_max_kmh=8.0, humidity_max=60, uv_index=4.0, sunset="17:21"),
        DayForecast(date=start + timedelta(days=1), wmo_code=95, text="雷暴", tmax_c=24.0, tmin_c=18.0,
                    precip_mm=28.0, precip_prob=85, wind_max_kmh=46.0, humidity_max=95, sunset="17:20"),
        DayForecast(date=start + timedelta(days=2), wmo_code=1, text="基本晴朗", tmax_c=20.0, tmin_c=11.0,
                    wind_max_kmh=10.0, humidity_max=58, uv_index=5.0, sunset="17:19"),
    ]
    w = CityWeather(city="大理", city_key="dali", lat=25.6, lng=100.2, forecast=rows)
    weather_risk.assess(w, [r.date for r in rows], Party(adults=2))
    groups = weather_risk.window_verdicts(w, [r.date for r in rows])
    outdoor = groups["outdoor"]
    assert outdoor, groups
    assert any("第 1 好" in v["advice"] for v in outdoor), [v["advice"] for v in outdoor]
    assert any("日落 17:" in v["advice"] for v in outdoor), [v["advice"] for v in outdoor]
    assert groups["indoor"] or groups["avoid"], "雷暴大雨日不该还留在「适合户外」里"
    assert all(v["date"] != "11/24" for v in outdoor), [v["date"] for v in outdoor]
