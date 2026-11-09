"""候选打分：每个被选中的 POI 都要能回答"为什么是它"。

reasons 列表会一路带到 UI 与导出报告里，是本项目的可解释性抓手——
评委/用户不需要相信模型，他们能直接看到打分依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..knowledge.poi_codes import INTEREST_TO_CATEGORIES, MEAL_CATEGORIES, NIGHT_CATEGORIES
from ..models.enums import Category, Interest, Severity
from ..models.poi import POIRecord
from ..models.request import TripRequest
from ..models.weather import DayForecast, WeatherRisk

_AVOID_PENALTY = -1000.0


@dataclass
class ScoreContext:
    request: TripRequest
    forecast: DayForecast | None = None
    risk: WeatherRisk | None = None
    remaining_cents: int = 0
    slot: str = "day"  # day / meal / night
    used_names: set[str] = field(default_factory=set)

    @property
    def prefer_indoor(self) -> bool:
        return bool(self.risk and self.risk.prefer_indoor)


@dataclass
class Scored:
    poi: POIRecord
    value: float
    reasons: list[str]

    def as_dict(self) -> dict:
        return {"id": self.poi.id, "name": self.poi.name, "score": round(self.value, 2), "reasons": self.reasons}


def score(poi: POIRecord, ctx: ScoreContext) -> Scored:
    req = ctx.request
    value = 0.0
    reasons: list[str] = []

    # 1. 口碑（评分优先，缺评分时退回 popularity）
    if poi.rating:
        gain = (poi.rating - 3.5) * 12
        if poi.rating >= 4.5:
            reasons.append(f"评分 {poi.rating}")
        value += gain
    value += poi.popularity * 10

    # 2. 兴趣匹配（含 must_visit 强提升）
    blob = " ".join([poi.name, poi.why, poi.best_time, *poi.tags, poi.category.label]).lower()
    for it in req.interests:
        if _interest_hits(it, poi, blob):
            value += 18
            reasons.append(f"命中偏好「{it.label}」")
    for kw in req.must_visit:
        if kw.lower() in blob or kw.lower() in poi.name.lower():
            value += 60
            reasons.append(f"你点名要去「{kw}」")
    for kw in req.avoid:
        if kw and kw.lower() in blob:
            value += _AVOID_PENALTY
            reasons.append(f"你说过不想去「{kw}」")

    # 3. 时段/类目契合
    if ctx.slot == "night":
        if poi.category in NIGHT_CATEGORIES:
            value += 22
            reasons.append("属于夜间娱乐")
        else:
            value -= 25
    elif ctx.slot == "meal":
        if poi.category in MEAL_CATEGORIES:
            value += 22
            reasons.append("适合用餐时段")
        else:
            value -= 30
    else:
        if poi.category in NIGHT_CATEGORIES:
            value -= 20
        if poi.category in (Category.SIGHT, Category.HISTORIC, Category.MUSEUM, Category.PARK, Category.AMUSEMENT):
            value += 8

    # 4. 天气契合
    if ctx.prefer_indoor:
        if poi.indoor:
            value += 25
            reasons.append("当天天气恶劣，室内项目更稳")
        else:
            value -= 22
    elif ctx.forecast and not ctx.forecast.is_wet and ctx.slot == "day":
        if not poi.indoor and poi.category in (Category.SIGHT, Category.PARK):
            value += 10
            reasons.append("当天天气好，户外体验更佳")
    if ctx.risk and ctx.risk.severity.rank >= Severity.WARN.rank and poi.category == Category.AMUSEMENT:
        value -= 30  # 露天乐园在预警日性价比极低

    # 5. 人群适配
    p = req.party
    if p.kids:
        if poi.kid_friendly:
            value += 18
            reasons.append("亲子友好")
        elif poi.kid_friendly is False:
            value -= 25
    if p.elders:
        if poi.stay_minutes > 210:
            value -= 12
            reasons.append("单次游玩偏长，长辈同行建议拆分")
        if poi.accessible:
            value += 12
    if p.pets:
        if poi.pet_friendly:
            value += 18
            reasons.append("可携宠")
        elif poi.pet_friendly is False:
            value -= 30
    if p.accessible and poi.accessible is False:
        value -= 40

    # 6. 价格与预算
    price = poi.price_cents * max(1, p.head_count)
    if req.budget_cents and ctx.remaining_cents:
        ratio = price / max(1, ctx.remaining_cents)
        if ratio > 0.35:
            value -= 25
            reasons.append(f"单项占剩余预算 {ratio:.0%}，偏贵")
        elif ratio < 0.12:
            value += 10
            if Interest.BUDGET in req.interests:
                reasons.append("性价比高")
    if req.has_interest(Interest.BUDGET) and price and price < 15000:
        value += 12
        reasons.append("花费低")

    # 7. 时间约束
    if poi.open_hours_raw and poi.open_from_minute is None:
        reasons.append("营业时间未结构化，出行前请复核")
    if poi.need_reservation:
        value -= 6
        lead = f"，需提前 {poi.reservation_lead_days} 天" if poi.reservation_lead_days else ""
        reasons.append(f"需预约{lead}")
    if poi.best_time:
        blob_hit = poi.best_time in ("夜", "夜晚", "晚上", "傍晚", "日落", "黄昏")
        if blob_hit and ctx.slot != "night":
            value -= 15
            reasons.append(f"最佳时段是{poi.best_time}，当前排在白天")

    # 7.5 停业兜底：数据源没过滤干净时，打分层再拦一次
    if poi.closed:
        value += _AVOID_PENALTY
        reasons.append("该地点当前暂停营业，已排除")

    # 8. 去重
    if poi.name in ctx.used_names:
        value += _AVOID_PENALTY
        reasons.append("已在行程中出现，不重复安排")

    return Scored(poi=poi, value=value, reasons=reasons or ["默认热度排序"])


def _interest_hits(it: Interest, poi: POIRecord, blob: str) -> bool:
    if poi.category in INTEREST_TO_CATEGORIES.get(it, ()):
        return True
    if it is Interest.PHOTO and (poi.photo_spots or "打卡" in blob or "网红" in blob):
        return True
    if it is Interest.FOOD and poi.category in MEAL_CATEGORIES:
        return True
    if it is Interest.SHOWS and poi.category in (Category.SHOW, Category.CINEMA, Category.MUSEUM):
        return True
    if it is Interest.NIGHTLIFE and poi.category in NIGHT_CATEGORIES:
        return True
    from ..models.enums import aliases_of

    return any(alias in blob for alias in aliases_of(Interest, it) if len(alias) >= 2)
