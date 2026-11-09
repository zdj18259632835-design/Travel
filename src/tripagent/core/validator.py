"""行程约束校验：把"这版方案能不能直接执行"变成一张可展示的记分卡。

约束三分法取自 TravelPlanner / ChinaTravel 的公开定义：
  env         —— 数据与环境事实（城市是否识别、坐标是否覆盖）
  commonsense —— 常识（不折返、不重叠、时段合理）
  hard        —— 用户硬约束（预算、人群禁忌、预约窗口、天气安全）
这一层是本项目最"可辩护"的部分：评委问"凭什么说你排得合理"，答案是逐条通过率。
"""

from __future__ import annotations

from datetime import date, timedelta

from ..config import Settings
from ..models.enums import Category, ConstraintKind, Severity, SlotType
from ..models.plan import Slot, TripPlan
from ..models.validation import ConstraintCheck, ValidationReport
from ..providers.base import Chain

_NIGHT_CATS = (Category.BAR, Category.CLUB, Category.KTV, Category.ESCAPE)


def validate(plan: TripPlan, chain: Chain | None = None, settings: Settings | None = None) -> ValidationReport:
    settings = settings or Settings()
    report = ValidationReport()
    _check_env(plan, report)
    _check_commonsense(plan, report, settings)
    _check_hard(plan, report, settings)
    return report


def _add(report: ValidationReport, cid: str, kind: ConstraintKind, name: str, passed: bool, detail: str = "", hint: str = "", weight: float = 1.0, related: list[str] | None = None) -> None:
    report.add(
        ConstraintCheck(
            id=cid,
            kind=kind,
            name=name,
            passed=passed,
            detail=detail,
            fix_hint=hint,
            weight=weight,
            related=related or [],
        )
    )


def _all_slots(plan: TripPlan) -> list[tuple[int, Slot]]:
    return [(d.index, s) for d in plan.days for s in d.slots]


# ---------------------------------------------------------------- env
def _check_env(plan: TripPlan, r: ValidationReport) -> None:
    req = plan.request
    _add(r, "city_resolved", ConstraintKind.ENV, "目的地已识别", bool(req.destination), "" if req.destination else f"没能从「{req.raw_text}」里认出城市", "补全城市名，或在 data/fixtures/cities_extra.json 里新增城市坐标", weight=2.0)

    slots = [s for d in plan.days for s in d.slots if s.poi]
    geo = [s for s in slots if s.poi and s.poi.point]
    ratio = len(geo) / len(slots) if slots else 0
    _add(
        r,
        "geo_coverage",
        ConstraintKind.ENV,
        "坐标覆盖率",
        ratio >= 0.7,
        f"{len(geo)}/{len(slots)} 个安排有经纬度" if slots else "本次没有安排任何实体地点",
        "配置 AMAP_KEY 或为该城市补充带坐标的 fixture，才能画路线图" if ratio < 0.7 else "",
    )

    srcs = {s.poi.source for s in slots if s.poi}
    _add(
        r,
        "poi_source",
        ConstraintKind.ENV,
        "地点数据来源可信",
        "amap" in srcs or "fixture" in srcs,
        f"来源：{'、'.join(sorted(srcs)) or '无'}",
        "" if srcs else "无数据源命中，建议扩充 fixture",
    )

    days_with_weather = [d for d in plan.days if d.weather]
    _add(
        r,
        "weather_coverage",
        ConstraintKind.ENV,
        "行程日期均有预报",
        len(days_with_weather) == len(plan.days),
        f"{len(days_with_weather)}/{len(plan.days)} 天拿到预报" if plan.days else "无行程日",
        "出发日期超出 15 天预报窗口，越靠后越不准，建议临近再复核" if len(days_with_weather) != len(plan.days) else "",
    )


# ---------------------------------------------------------------- commonsense
def _check_commonsense(plan: TripPlan, r: ValidationReport, st: Settings) -> None:
    overlap: list[str] = []
    overtime: list[str] = []
    dup: list[str] = []
    backtrack: list[str] = []
    off_hours: list[str] = []
    idle: list[str] = []
    odd_meal: list[str] = []
    long_walk: list[str] = []

    seen: set[str] = set()
    for d in plan.days:
        for s in d.slots:
            if s.type is SlotType.FREE:
                continue
            if s.start_minute >= s.end_minute:
                overlap.append(f"Day{d.index} {s.title}")
            if s.type in (SlotType.CHECKIN, SlotType.FREE, SlotType.REST):
                continue  # 住宿与自由活动本来就延伸到 24:00，不该算超时
            limit = st.night_end_minute if s.type in (SlotType.NIGHTLIFE, SlotType.SHOW) else st.day_end_minute
            if s.end_minute > limit:
                overtime.append(f"Day{d.index} {s.title} 至 {s.end_minute // 60:02d}:{s.end_minute % 60:02d}")
            key = s.poi.external_id or s.title if s.poi else s.title
            if s.type not in (SlotType.CHECKIN,) and key in seen:
                dup.append(f"Day{d.index} {s.title}")
            seen.add(key)
            if s.poi and s.type not in (SlotType.CHECKIN, SlotType.MEAL):
                ok = s.poi.open_covers(s.start_minute, s.duration_min)
                if ok is False:
                    off_hours.append(f"Day{d.index} {s.title}（营业 {s.poi.open_hours_raw}）")
            if s.type is SlotType.MEAL:
                if not (11 * 60 - 60 <= s.start_minute <= 14 * 60 + 60) and not (17 * 60 - 60 <= s.start_minute <= 21 * 60 + 60):
                    odd_meal.append(f"Day{d.index} {s.title} {s.time_text}")
            tb = s.transit_before
            if tb and tb.mode == "walk" and tb.minutes > st.max_walk_minutes_per_leg:
                long_walk.append(f"Day{d.index} 去「{s.title}」需步行 {tb.minutes} 分")

        real = [s for s in d.slots if s.type not in (SlotType.FREE, SlotType.CHECKIN)]
        if not real:
            why = next((s.notes[0] for s in d.slots if s.type is SlotType.FREE and s.notes), "")
            idle.append(f"Day{d.index}" + (f"：{why[:70]}" if why else "（无安排）"))
        # 折返检测：同一天里出现 "回到已访问坐标" 或通勤占比过高
        transit_min = sum(s.transit_before.minutes for s in d.slots if s.transit_before)
        active = max(1, sum(s.duration_min for s in d.slots))
        if transit_min and transit_min / active > 0.45:
            backtrack.append(f"Day{d.index} 通勤 {transit_min} 分 / 活动 {active} 分")

    _add(r, "no_time_overlap", ConstraintKind.COMMONSENSE, "时间段不重叠且顺序有效", not overlap, "；".join(overlap[:3]), "检查通勤估算与停留时长设置", weight=1.5)
    _add(r, "within_day_window", ConstraintKind.COMMONSENSE, "未超出活动时段（白天 21:30／夜场 24:00）", not overtime, "；".join(overtime[:3]), f"调低每日项目数，或把 {overtime[0].split()[1] if overtime else ''} 挪到次日" if overtime else "")
    _add(r, "no_duplicate_poi", ConstraintKind.COMMONSENSE, "同一地点不重复安排", not dup, "；".join(dup[:3]), "景点重复，需要替换候选池", weight=1.2)
    _add(r, "route_efficiency", ConstraintKind.COMMONSENSE, "路线不折返（通勤占比 <45%）", not backtrack, "；".join(backtrack[:3]), "同一天尽量按同一街区聚类，或改用住宿附近出发", weight=1.5)
    _add(r, "open_hours_respected", ConstraintKind.COMMONSENSE, "落在营业时间内", not off_hours, "；".join(off_hours[:3]), "调整时段或替换为全天开放景点", weight=1.5)
    _add(r, "every_day_has_plan", ConstraintKind.COMMONSENSE, "每天都有实际安排", not idle, "；".join(idle), "为空白日补充候选或调整天数")
    _add(r, "meal_time_reasonable", ConstraintKind.COMMONSENSE, "用餐时段合理", not odd_meal, "；".join(odd_meal[:3]), "把用餐挪到 11:30-14:30 / 17:00-21:00")
    _add(
        r,
        "walk_leg_within_limit",
        ConstraintKind.COMMONSENSE,
        f"单段步行 ≤{st.max_walk_minutes_per_leg} 分钟",
        not long_walk,
        "；".join(long_walk[:3]),
        "改选同街区候选，或把该段改成地铁/打车",
    )


# ---------------------------------------------------------------- hard
def _check_hard(plan: TripPlan, r: ValidationReport, st: Settings) -> None:
    req = plan.request

    if req.budget_cents:
        spent = plan.total_cost_cents
        _add(
            r,
            "budget_within",
            ConstraintKind.HARD,
            "不超预算",
            spent <= req.budget_cents,
            f"预计花费 ¥{spent / 100:.0f} / 预算 ¥{req.budget_yuan:.0f}",
            "去掉得分最低的付费项，或改选低价类目（公园/老街/夜市）" if spent > req.budget_cents else "",
            weight=3.0,
        )
    else:
        _add(r, "budget_within", ConstraintKind.HARD, "预算约束（未设置，跳过）", True, "用户未给预算，不做金额裁剪")

    kids = req.party.kids
    bad_kid = [s.title for _, s in _all_slots(plan) if kids and s.poi and s.poi.category in (Category.BAR, Category.CLUB, Category.KTV)]
    _add(r, "kid_safe_venues", ConstraintKind.HARD, "带娃不安排成人场所", not bad_kid, "；".join(bad_kid[:3]), "未成年人不得进入酒吧/夜店，替换为亲子乐园或演出", weight=2.0)

    pets = req.party.pets
    bad_pet = [s.title for _, s in _all_slots(plan) if pets and s.poi and s.poi.pet_friendly is False]
    _add(r, "pet_allowed", ConstraintKind.HARD, "可携宠约束", not bad_pet, "；".join(bad_pet[:3]), "该场所不接受宠物，替换为公园/户外", weight=1.5)

    acc = req.party.accessible
    bad_acc = [s.title for _, s in _all_slots(plan) if acc and s.poi and s.poi.accessible is False]
    _add(r, "accessible_only", ConstraintKind.HARD, "无障碍可达", not bad_acc, "；".join(bad_acc[:3]), "更换有电梯/坡道的场所", weight=1.5)

    danger_outdoor = []
    for d in plan.days:
        if not (d.risk and d.risk.severity.rank >= Severity.WARN.rank):
            continue
        outdoor = [s for s in d.slots if s.poi and not s.poi.indoor and s.type in (SlotType.SIGHT, SlotType.NIGHTLIFE, SlotType.SHOW)]
        if len(outdoor) >= 2:
            danger_outdoor.append(f"Day{d.index}（{d.risk.severity.label}，仍排 {len(outdoor)} 项户外）")
    _add(
        r,
        "weather_adapted",
        ConstraintKind.HARD,
        "恶劣天气日已转室内",
        not danger_outdoor,
        "；".join(danger_outdoor[:3]),
        "把当天户外项替换为博物馆/演出/商场，或与相邻日期整日对调",
        weight=2.5,
    )

    late_res: list[str] = []
    day_dates = {d.index: d.date for d in plan.days}
    for idx, s in _all_slots(plan):
        if not (s.poi and s.poi.need_reservation and s.poi.reservation_lead_days):
            continue
        # 预约截止要按"实际游览那天"倒推，而不是行程首日：
        # 用首日会让排在 Day3 的限流场馆看起来还来得及。
        visit = day_dates.get(idx) or req.start_date
        if visit is None:
            continue
        deadline = visit - timedelta(days=s.poi.reservation_lead_days)
        if deadline < date.today():
            late_res.append(f"{s.title} 需 {deadline:%m月%d日} 前预约（提前 {s.poi.reservation_lead_days} 天），已来不及")
    _add(r, "reservation_feasible", ConstraintKind.HARD, "预约窗口来得及", not late_res, "；".join(late_res[:3]), "改期或选择免预约景点", weight=1.5)

    nightlife_slots = [s for _, s in _all_slots(plan) if s.poi and s.poi.category in _NIGHT_CATS]
    if nightlife_slots:
        has_id = any("身份证" in p for p in plan.packing)
        _add(
            r,
            "nightlife_compliance",
            ConstraintKind.HARD,
            "夜间娱乐合规提醒",
            has_id,
            f"共 {len(nightlife_slots)} 处夜间场所（均需实名核验）",
            "在行前清单里加入身份证提醒",
            weight=1.0,
        )
