"""行程装配引擎。

刻意不让大模型自由发挥排程，而是「确定性装配 + 事后校验」：
1. 每天先选一个锚点景点，其余按 得分 − 通勤代价 贪心补齐 → 天然少折返；
2. 用餐只在午/晚窗口，夜间娱乐只在夜场窗口；
3. 恶劣天气日把户外锚点自动换成室内（博物馆/演出/商场），并记录换的理由；
4. 预算按「分」累加，装不下就丢最低分项，而不是硬塞爆预算；
5. 每个 Slot 携带 reasons/evidence，报告里能回答"为什么是它"。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from ..config import Settings
from ..knowledge.cities import CityInfo, resolve_city
from ..knowledge.poi_codes import MEAL_CATEGORIES, NIGHT_CATEGORIES
from ..models.common import GeoPoint
from ..models.enums import Category, Interest, Severity, SlotType
from ..models.poi import POIRecord, RouteLeg
from ..models.plan import DayPlan, IntercityLeg, Slot, TripPlan
from ..models.request import TripRequest
from ..models.weather import CityWeather, DayForecast, WeatherRisk
from ..providers.base import Chain
from .scoring import ScoreContext, Scored, score
from .weather_risk import comfort_index

_SLOT_TYPE: dict[Category, SlotType] = {
    Category.FOOD: SlotType.MEAL,
    Category.CAFE: SlotType.MEAL,
    Category.BAR: SlotType.NIGHTLIFE,
    Category.KTV: SlotType.NIGHTLIFE,
    Category.CLUB: SlotType.NIGHTLIFE,
    Category.ESCAPE: SlotType.NIGHTLIFE,
    Category.SHOW: SlotType.SHOW,
    Category.CINEMA: SlotType.SHOW,
    Category.HOTEL: SlotType.CHECKIN,
}

# 一天里"值不值得单独占一天"的权重
_POOL_BIAS: dict[Category, float] = {
    Category.AMUSEMENT: 1.35,
    Category.SPA: 1.15,
    Category.SIGHT: 1.10,
    Category.MUSEUM: 1.05,
}

_PACE_STAY = {"relaxed": 1.25, "normal": 1.0, "packed": 0.8}
_PACE_LIMIT = {"relaxed": 2, "normal": 3, "packed": 4}
_PACE_BUFFER = {"relaxed": 25, "normal": 15, "packed": 8}


def slot_type(cat: Category) -> SlotType:
    return _SLOT_TYPE.get(cat, SlotType.SIGHT)


class DayAssembler:
    """把候选池按时间轴装配成一天。"""

    def __init__(
        self,
        index: int,
        day_date: date,
        city: CityInfo,
        request: TripRequest,
        settings: Settings,
        chain: Chain,
        forecast: DayForecast | None,
        risk: WeatherRisk | None,
        lodging: POIRecord | None,
        pool: list[POIRecord],
        used_names: set[str],
        budget_left: int,
        cursor: int,
        routes: list[dict] | None = None,
    ) -> None:
        self.index = index
        self.date = day_date
        self.city = city
        self.request = request
        self.settings = settings
        self.chain = chain
        self.forecast = forecast
        self.risk = risk
        self.lodging = lodging
        self.pool = pool
        self.used_names = used_names
        self.budget_left = budget_left
        self.cursor = cursor
        self.slots: list[Slot] = []
        self.prev_point = lodging.point if lodging and lodging.point else None
        self.routes = routes if routes is not None else []
        # 预警日只保留至多 1 项户外：完全禁户外会让整天空洞，全放开又违反安全策略
        self.outdoor_used = 0

    # ---------- 对外 ----------
    def run(self) -> DayPlan:
        day_pool = [
            p for p in self.pool
            if p.name not in self.used_names
            and p.category not in MEAL_CATEGORIES
            and p.category not in NIGHT_CATEGORIES
            and p.category is not Category.HOTEL
        ]
        if not day_pool:
            exhausted = any(
                p.category not in MEAL_CATEGORIES
                and p.category not in NIGHT_CATEGORIES
                and p.category is not Category.HOTEL
                for p in self.pool
            )
            note = (
                "候选景点已全部排入前几日；本日改为自由活动，可用「即时问答」按商圈与评分现场补推荐"
                if exhausted
                else f"「{self.city.name}」暂无结构化景点数据：配置 AMAP_KEY 可实时检索，"
                f"或在 data/fixtures/{self.city.key}.json 补充本地知识库"
            )
            self._append_free(note)
        else:
            self._fill_daytime(day_pool)
        # 即使白天没排满，吃饭和夜场也要继续尝试：空着整天比"少一个景点"更伤体验
        self._fill_meals()
        self._fill_night()
        self._append_checkin()
        if not self.slots:
            # 候选被人群约束/营业时间/剩余额度全部排除时，宁可给出一天可执行的"自由活动"
            # 并说明原因，也不要交出一张空白日让用户以为程序坏了。
            self.cursor = min(self.cursor, self.settings.day_end_minute - 120)
            self._append_free(
                f"本日候选被约束排除（携宠/儿童禁入、营业时间不匹配或剩余额度不足）。"
                f"建议：放宽人群限制、换住宿商圈，或用「即时问答」按商圈现找。"
            )
        day = DayPlan(
            index=self.index,
            date=self.date,
            city=self.city.name,
            theme=self._theme(),
            weather=self.forecast,
            risk=self.risk,
            slots=[_resequence(s, i) for i, s in enumerate(self.slots, start=1)],
            lodging=self.lodging,
            notes=self._day_notes(),
        )
        day.recompute_totals()
        return day

    # ---------- 白天 ----------
    def _fill_daytime(self, day_pool: list[POIRecord]) -> None:
        limit = self._day_limit()
        ranked = sorted(
            (self._scored(p, "day") for p in day_pool),
            key=lambda s: -(s.value * _POOL_BIAS.get(s.poi.category, 1.0)),
        )
        anchor = self._pick(ranked, allow_outdoor=self._allow_outdoor())
        if anchor:
            self.add(anchor, slot_type(anchor.poi.category))
        placed = 0
        guard = 0
        while placed < limit and guard < 40:
            guard += 1
            rest = [p for p in day_pool if p.name not in self.used_names]
            if not rest:
                break
            scored = sorted((self._scored_proximate(p, "day") for p in rest), key=lambda s: -s.value)
            nxt = self._pick(scored, allow_outdoor=self._allow_outdoor())
            if nxt is None:
                break
            if self.add(nxt, slot_type(nxt.poi.category)) is None:
                # 时间窗装不下：整天放弃它，否则会在同一个候选上死循环
                self.used_names.add(nxt.poi.name)
                continue
            placed += 1

    # ---------- 用餐 ----------
    def _fill_meals(self) -> None:
        meals = [p for p in self.pool if p.category in MEAL_CATEGORIES and p.name not in self.used_names]
        if not meals:
            return
        for hour in (12, 18):
            if len([s for s in self.slots if s.type is SlotType.MEAL]) >= self.settings.max_meals_per_day:
                break
            if self.cursor > hour * 60 + 120:
                continue
            self.cursor = max(self.cursor, hour * 60)
            pick = self._pick(sorted((self._scored(p, "meal") for p in meals if p.name not in self.used_names), key=lambda s: -s.value))
            if pick:
                self.add(pick, SlotType.MEAL)

    # ---------- 夜间娱乐（本项目主战场）----------
    def _fill_night(self) -> None:
        req = self.request
        if not (req.nightlife or req.has_interest(Interest.NIGHTLIFE, Interest.SHOWS)):
            return
        night = [p for p in self.pool if p.category in NIGHT_CATEGORIES and p.name not in self.used_names]
        if not night:
            return
        scored = sorted((self._scored(p, "night") for p in night), key=lambda s: -s.value)
        pick = self._pick(scored)
        if pick is None:
            return
        last_end = self.slots[-1].end_minute if self.slots else 18 * 60
        self.cursor = min(max(self.cursor, last_end + 45, 19 * 60), 21 * 60 + 30)
        slot = self.add(pick, slot_type(pick.poi.category))
        if slot and self.lodging and self.lodging.point:
            back = self._route(self.lodging, slot.title, register=False)
            slot.notes.append(
                f"散场后回酒店「{self.lodging.name}」约 {back.minutes} 分钟（{back.summary or back.mode}）"
            )
            if back.minutes and self.cursor > 23 * 60:
                slot.notes.append("结束时间已接近午夜，请确认末班交通或改用出租车/代驾")

    def _append_checkin(self) -> None:
        if not (self.lodging and self.index == 1):
            return
        if any(s.type is SlotType.CHECKIN for s in self.slots):
            return
        start = min(max(self.cursor, 21 * 60), 23 * 60)
        self.slots.append(
            Slot(
                seq=len(self.slots) + 1,
                type=SlotType.CHECKIN,
                title=f"入住 {self.lodging.name}",
                start_minute=start,
                end_minute=24 * 60,
                poi=self.lodging,
                cost_cents=0,  # 住宿按整程晚数计在「预算」分项里，避免逐日与总计两处重复计入
                indoor=True,
                notes=[self.lodging.why or "住宿锚点：夜间返程与次日出发的基准位置"],
            )
        )

    def _append_free(self, note: str) -> None:
        end = self.settings.day_end_minute
        if self.cursor >= end:
            return  # 时间轴已被用餐/夜场占到 21:30 之后，塞一个 start>=end 的空洞会直接违反"时段有效"
        self.slots.append(
            Slot(
                seq=len(self.slots) + 1,
                type=SlotType.FREE,
                title="自由活动",
                start_minute=self.cursor,
                end_minute=end,
                notes=[note],
                flexible=True,
            )
        )

    # ---------- 工具 ----------
    def add(self, scored: Scored, kind: SlotType) -> Slot | None:
        poi = scored.poi
        leg: RouteLeg | None = None
        base = self.cursor
        prev_title = self.slots[-1].title if self.slots else (
            f"住宿地 {self.lodging.name}" if self.lodging else self.city.name
        )
        if self.slots and (not poi.point or self.prev_point):
            leg = self._route(poi, prev_title)
            base += leg.minutes
        minutes = self._fit_open_hours(poi, base, self._stay_for(poi))
        if minutes is None:
            return None
        limit = self.settings.night_end_minute if kind in (SlotType.NIGHTLIFE, SlotType.SHOW) else self.settings.day_end_minute
        if base + minutes > limit:
            return None
        # 只有真排上了才推进时间轴：以前先 cursor += 再判断失败，
        # 会让被拒绝的候选偷偷吃掉时间，后续餐段全部错位甚至出现 start>=end。
        cost = poi.price_cents * max(1, self.request.party.head_count)
        slot = Slot(
            seq=len(self.slots) + 1,
            type=kind,
            title=poi.name,
            start_minute=base,
            end_minute=base + minutes,
            poi=poi,
            cost_cents=cost,
            transit_before=leg,
            indoor=poi.indoor,
            notes=list(scored.reasons),
            reservation_hint=_reservation_hint(poi),
            evidence_ids=list(poi.evidence_ids),
            flexible=kind in (SlotType.SIGHT, SlotType.NIGHTLIFE),
        )
        self.slots.append(slot)
        self.cursor = slot.end_minute + self._buffer(kind)
        self.prev_point = poi.point or self.prev_point
        self.used_names.add(poi.name)
        self.budget_left = max(0, self.budget_left - cost)
        if kind is SlotType.SIGHT and not poi.indoor:
            self.outdoor_used += 1
        return slot

    def _route(self, poi: POIRecord, prev_title: str = "", register: bool = True) -> RouteLeg:
        """装配阶段只用估算，真实通勤留到最后并发换回。

        为什么：装配是顺序决策，当场问高德就是 16 段串行 HTTP（实测 18 秒），
        而这段等待完全可以从"串行相加"变成"并发取最大值"。
        估算值决定"这天排不排得下"，真实值只负责告诉校验器与导出文件"这段多久"。

        register=False 是给"散场回酒店"这种要立刻写出分钟数的场景用的：
        只有一段，当场问真实路径比留给用户一个估算值更负责。
        """
        target = poi.point
        mode = "transit"
        if self.prev_point and target:
            d = self.prev_point.distance_m_to(target)
            mode = "walk" if d <= 1200 else "transit"
        if not register:
            if self.prev_point and target:
                res = self.chain.run("transit", "route", self.city, self.prev_point, target, mode)
                if isinstance(res.value, RouteLeg):
                    leg = res.value
                    leg.summary = f"从「{prev_title}」→「{poi.name}」{leg.summary or ''}".strip()
                    return leg
            leg = RouteLeg.estimate(self.prev_point, target, mode)
            leg.summary = f"从「{prev_title}」→「{poi.name}」{leg.summary or ''}".strip()
            return leg
        leg = RouteLeg.estimate(self.prev_point, target, mode)
        if self.prev_point and target:
            leg.summary = f"从「{prev_title}」→「{poi.name}」{leg.summary or ''}".strip()
            self.routes.append(
                {
                    "leg": leg,
                    "origin": GeoPoint(self.prev_point.lat, self.prev_point.lng),
                    "dest": GeoPoint(target.lat, target.lng),
                    "mode": mode,
                    "prefix": f"从「{prev_title}」→「{poi.name}」",
                }
            )
        elif not poi.point:
            leg.summary = f"从「{prev_title}」出发，「{poi.name}」坐标缺失，按默认预留"
        return leg

    def _scored(self, poi: POIRecord, slot: str) -> Scored:
        ctx = ScoreContext(
            request=self.request,
            forecast=self.forecast,
            risk=self.risk,
            remaining_cents=self.budget_left,
            slot=slot,
            used_names=self.used_names,
        )
        return score(poi, ctx)

    def _scored_proximate(self, poi: POIRecord, slot: str) -> Scored:
        s = self._scored(poi, slot)
        if self.prev_point and poi.point:
            minutes = poi.point.distance_m_to(self.prev_point) / 1000 / 12 * 60
            s.value -= minutes * 0.7
            if minutes >= 45:
                s.reasons.append(f"距上一站约 {minutes:.0f} 分钟车程，扣分")
        return s

    def _pick(self, ranked: list[Scored], allow_outdoor: bool = True) -> Scored | None:
        """预算过滤 + 人群硬排除 +（恶劣天气时）室内优先两遍扫描。"""
        party = self.request.party
        indoor_first = bool(self.risk and self.risk.prefer_indoor)
        passes = ([s for s in ranked if s.poi.indoor], ranked) if indoor_first else (ranked,)
        for pool in passes:
            for s in pool:
                if s.value <= -100:
                    continue
                if not allow_outdoor and not s.poi.indoor:
                    continue
                if self._excluded_for_party(s.poi):
                    continue
                price = s.poi.price_cents * max(1, party.head_count)
                if self.request.budget_cents and price > self.budget_left:
                    continue
                return s
        return None

    def _excluded_for_party(self, poi: POIRecord) -> bool:
        """与同行人群硬冲突的场所直接不排，而不是排完再让校验器判失败。"""
        p = self.request.party
        if p.kids and poi.category in (Category.BAR, Category.CLUB, Category.KTV):
            return True
        if p.pets and poi.pet_friendly is False:
            return True
        if p.accessible and poi.accessible is False:
            return True
        return False

    def _allow_outdoor(self) -> bool:
        return not (self.risk and self.risk.prefer_indoor and self.outdoor_used >= 1)

    def _fit_open_hours(self, poi: POIRecord, start: int, requested: int) -> int | None:
        """把停留时长收进营业时间窗口；窗口对不上就返回 None（换候选而不是硬塞）。"""
        lo, hi = poi.open_from_minute, poi.open_to_minute
        if lo is None or hi is None:
            return requested
        if hi <= lo:  # 跨零点营业（酒吧/夜店/24h 健身）
            if start >= lo:
                return max(requested, 60)
            if start < hi:
                return min(requested, max(60, hi - start))
            return None
        if start < lo:
            return None
        room = hi - start
        if room < 45:
            return None
        return min(requested, room)

    def _stay_for(self, poi: POIRecord) -> int:
        raw = poi.stay_minutes or 90
        if poi.category == Category.AMUSEMENT:
            raw = max(raw, 240)
        if poi.category == Category.KTV:
            raw = max(raw, 120)
        return int(round(raw * _PACE_STAY.get(self.request.pace, 1.0)))

    def _day_limit(self) -> int:
        base = _PACE_LIMIT.get(self.request.pace, 3)
        if self.request.party.needs_care:
            base -= 1
        return max(1, min(base, self.settings.max_sights_per_day + 1))

    def _buffer(self, kind: SlotType) -> int:
        if kind is SlotType.MEAL:
            return 15
        base = _PACE_BUFFER.get(self.request.pace, 15)
        return base + 12 if self.request.party.needs_care else base

    def _theme(self) -> str:
        cats = [s.poi.category for s in self.slots if s.poi]
        if not cats:
            return "自由活动"
        counts: dict[Category, int] = {}
        for c in cats:
            counts[c] = counts.get(c, 0) + 1
        top = max(counts, key=lambda c: counts[c])
        label = {
            Category.SIGHT: "经典景点",
            Category.HISTORIC: "人文古迹",
            Category.MUSEUM: "博物展览",
            Category.PARK: "城市公园",
            Category.AMUSEMENT: "主题乐园",
            Category.SPORTS: "运动休闲",
            Category.SHOPPING: "逛街购物",
            Category.SPA: "温泉洗浴",
        }.get(top, "综合游览")
        if any(c in NIGHT_CATEGORIES for c in cats):
            label += " + 夜生活"
        if self.risk and self.risk.prefer_indoor:
            reason = "低温大风" if (self.forecast and (self.forecast.is_cold or (self.forecast.wind_max_kmh or 0) >= 40)) else (
                "降雨" if (self.forecast and self.forecast.is_wet) else "恶劣天气"
            )
            label = f"室内优先（{reason}） · " + label
        return label

    def _day_notes(self) -> list[str]:
        notes: list[str] = []
        if self.risk and self.risk.severity.rank >= Severity.WATCH.rank:
            notes.append(f"天气提示[{self.risk.severity.label}]：" + "；".join(self.risk.advice))
        for s in self.slots:
            if s.reservation_hint:
                notes.append(f"{s.title}：{s.reservation_hint}")
        return notes


class ItineraryPlanner:
    def __init__(
        self,
        city: CityInfo,
        request: TripRequest,
        weather: CityWeather,
        chain: Chain,
        settings: Settings,
    ) -> None:
        self.city = city
        self.request = request
        self.weather = weather
        self.chain = chain
        self.settings = settings
        self.routes_resolved = 0

    def _resolve_routes(self, routes: list[dict]) -> int:
        """并发把估算通勤换成高德真实路径，就地更新 Slot 已经引用的那个 RouteLeg。

        16 段串行 HTTP ≈ 18 秒，6 路并发 ≈ 3 秒；估算值负责排程，
        真实值负责校验与导出——两边各取所长，而不是让游客等一个"更准的排序"。
        """
        if not routes:
            return 0

        def fetch(item: dict):
            return self.chain.run("transit", "route", self.city, item["origin"], item["dest"], item["mode"])

        got = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            for item, res in zip(routes, pool.map(fetch, routes)):
                real = res.value if isinstance(res.value, RouteLeg) else None
                if not real or not real.minutes:
                    continue
                leg = item["leg"]
                leg.minutes = real.minutes
                leg.distance_m = real.distance_m
                leg.cost_cents = real.cost_cents
                leg.mode = real.mode
                leg.source = real.source
                leg.summary = f"{item['prefix']}{real.summary or ''}".strip()
                got += 1
        return got

    def plan(
        self,
        pool: dict[Category, list[POIRecord]],
        lodging: POIRecord | None = None,
        trip_id: str = "",
        evidence_ids: list[str] | None = None,
    ) -> TripPlan:
        flat = _dedupe([p for rows in pool.values() for p in rows])
        window = self._window()
        used: set[str] = set()
        # 住宿必须先预留：它按整程晚数计在 budget_breakdown 里，
        # 若不从可花额度中扣掉，逐日装配会"每步都没超支、合计却爆预算"。
        budget_left = self.request.budget_cents
        if lodging and lodging.price_cents and budget_left:
            nights = max(1, len(window) - 1)
            reserve = lodging.price_cents * self.request.party.room_count * nights
            budget_left = max(0, budget_left - reserve)

        # 恶劣天气日先挑：室内候选（博物馆/剧场/商场）通常比户外少，
        # 若按日期顺序分配，好天气日会先把它们耗尽，导致雨天只能排户外。
        priority = sorted(
            range(len(window)),
            key=lambda i: -self._risk_rank(window[i]),
        )
        built: dict[int, DayPlan] = {}
        routes: list[dict] = []
        for pos in priority:
            d = window[pos]
            assembler = DayAssembler(
                index=pos + 1,
                day_date=d,
                city=self.city,
                request=self.request,
                settings=self.settings,
                chain=self.chain,
                forecast=self.weather.on_day(d),
                risk=self.weather.risk_on(d),
                lodging=lodging,
                pool=flat,
                used_names=used,
                budget_left=budget_left,
                cursor=self._day_start(pos + 1),
                routes=routes,
            )
            day = assembler.run()
            budget_left = max(0, budget_left - day.total_cost_cents)
            built[pos] = day
        days = [built[i] for i in range(len(window))]
        self.routes_resolved = self._resolve_routes(routes)

        plan = TripPlan(
            trip_id=trip_id or f"{self.city.key}-{date.today().isoformat().replace('-', '')}",
            request=self.request,
            days=days,
            intercity=self._intercity(window),
            evidence=evidence_ids or [],
            budget_breakdown=_breakdown(days, lodging, self.request),
            packing=_packing(self.request, days),
        )
        return plan

    def _window(self) -> list[date]:
        req = self.request
        start = req.start_date or self._suggest_start()
        return [start + timedelta(days=i) for i in range(max(1, req.days))]

    def _suggest_start(self) -> date:
        """用户没给日期时，从 15 天窗口里挑一个「无高等级风险 + 舒适指数最高」的出发日。"""
        best, best_score = date.today(), -1
        for f in self.weather.forecast:
            if f.date < date.today() + timedelta(days=2):
                continue  # 后天以后才订得到
            r = self.weather.risk_on(f.date)
            if r and r.severity.rank >= Severity.WARN.rank:
                continue
            s = comfort_index(f)
            if s > best_score:
                best, best_score = f.date, s
        self.request.start_date = best
        return best

    def _risk_rank(self, d: date) -> int:
        r = self.weather.risk_on(d)
        return r.severity.rank if r else 0

    def _day_start(self, idx: int) -> int:
        base = self.settings.day_start_minute
        if idx == 1 and self.request.origin_city:
            base = max(base, 780)  # 首日含跨城交通，按 13:00 起算
        elif idx == 1:
            base = max(base, 540)
        if self.request.pace == "relaxed":
            base += 45
        if self.request.party.elders:
            base += 30
        return base

    def _intercity(self, window: list[date]) -> list[IntercityLeg]:
        req = self.request
        if not req.origin_city:
            return []
        origin = resolve_city(req.origin_city)
        if not origin or origin.key == self.city.key:
            return []
        dep = window[0]
        back = window[-1] + timedelta(days=1)
        note = "时刻与票价请以 12306/航旅纵横 实时为准（无个人可用的开放接口，本条为占位建议）"
        return [
            IntercityLeg(mode="train", from_city=origin.name, to_city=self.city.name, dep_time=f"{dep:%m-%d} 上午", note=note, source="fixture"),
            IntercityLeg(mode="train", from_city=self.city.name, to_city=origin.name, dep_time=f"{back:%m-%d}", note=note, source="fixture"),
        ]


def _dedupe(rows: list[POIRecord]) -> list[POIRecord]:
    seen: set[str] = set()
    out: list[POIRecord] = []
    for p in rows:
        key = (p.external_id or p.name).strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _resequence(slot: Slot, i: int) -> Slot:
    slot.seq = i
    return slot


def _reservation_hint(poi: POIRecord) -> str:
    if not poi.need_reservation:
        return ""
    bits = ["需预约"]
    if poi.reservation_lead_days:
        bits.append(f"提前 {poi.reservation_lead_days} 天")
    if poi.reservation_channel:
        bits.append(f"渠道：{poi.reservation_channel}")
    if poi.reservation_cutoff:
        bits.append(f"{poi.reservation_cutoff} 停止入场")
    return " · ".join(bits)


def _breakdown(days: list[DayPlan], lodging: POIRecord | None, req: TripRequest) -> dict[str, int]:
    groups = {"门票娱乐": 0, "餐饮": 0, "住宿": 0, "交通": 0}
    for d in days:
        for s in d.slots:
            if s.type is SlotType.MEAL:
                groups["餐饮"] += s.cost_cents
            elif s.type is SlotType.CHECKIN:
                groups["住宿"] += s.cost_cents
            else:
                groups["门票娱乐"] += s.cost_cents
            if s.transit_before:
                groups["交通"] += s.transit_before.cost_cents
    if lodging and lodging.price_cents:
        nights = max(1, len(days) - 1)
        groups[f"住宿（{req.party.room_count} 间 × {nights} 晚）"] = (
            lodging.price_cents * req.party.room_count * nights
        )
    return {k: v for k, v in groups.items() if v}


def _packing(req: TripRequest, days: list[DayPlan]) -> list[str]:
    """从天气与人群直接推导出行清单——比让模型即兴列更像"智能体"。"""
    items: list[str] = []
    tmins = [d.weather.tmin_c for d in days if d.weather and d.weather.tmin_c is not None]
    tmaxs = [d.weather.tmax_c for d in days if d.weather and d.weather.tmax_c is not None]
    wet = any(d.weather and d.weather.precip_mm >= 5 for d in days)
    windy = any(d.weather and (d.weather.wind_max_kmh or 0) >= 40 for d in days)
    uvs = [d.weather.uv_index for d in days if d.weather and d.weather.uv_index]
    drys = [d.weather.humidity_max for d in days if d.weather and d.weather.humidity_max is not None]
    if wet:
        items.append("折叠伞/雨衣（行程中有降水日）")
    if uvs and max(uvs) >= 7:
        items.append("SPF50 防晒霜 + 墨镜（最晒那天紫外线指数已达强级）")
    if drys and min(drys) <= 35:
        items.append("润唇膏 + 保湿面霜（有几天全天湿度都很低）")
    if tmaxs and max(tmaxs) >= 33:
        items += ["防晒帽+防晒霜", "便携小风扇/补水盐丸"]
    if tmins and min(tmins) <= 5:
        items.append("保暖外套（清晨与夜场气温低）")
    if tmins and max(tmins) <= 0:
        items += ["防滑靴/暖宝宝", "手机保暖袋（低温掉电快）"]
    if windy:
        items.append("防风外套，避免戴易落帽子（索道/露天项目可能停运）")
    polluted = any(
        d.risk and any(x.name.startswith(("空气污染", "空气质量偏差")) for x in d.risk.factors)
        for d in days
    )
    if polluted:
        items.append("KN95 口罩（行程中有空气质量偏差的日子，敏感人群另备哮喘药）")
    if req.party.kids:
        items += ["儿童常用药、备用衣物", "推车或腰凳（日均步程较高时）"]
    if req.party.elders:
        items += ["长辈病历/常用药", "折叠凳（排队歇脚）"]
    if req.party.pets:
        items += ["宠物牵引绳、免疫证明（部分景区要求出示）"]
    if req.nightlife or req.has_interest(Interest.NIGHTLIFE):
        items.append("身份证（酒吧/夜店/KTV 需实名核验，人证不一致不得接待）")
    items += ["充电宝与数据线", "少量现金（老城区小摊）"]
    return items
