"""主编排：把 provider 链 + 规则引擎 + LLM 增强串成一次可观测的规划。

流水线（每一步都留痕，CLI/UI 都能看到"数据从哪来、谁失败了"）：
    understand → weather → assess_risk → gather_pois → gather_evidence
    → refine_pois → assemble → validate → narrate
LLM 只出现在 understand 补漏、evidence 归纳、narrate 润色三处，任何一处失败都不影响出方案。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..config import Settings, get_settings
from ..core import intent, validator, weather_risk
from ..core.planner import ItineraryPlanner
from ..knowledge.cities import CityInfo, resolve_city
from ..knowledge.poi_codes import NIGHT_CATEGORIES, categories_for
from ..models.common import GeoPoint
from ..models.enums import Category, Interest
from ..models.evidence import EvidenceRecord
from ..models.poi import POIQuery, POIRecord
from ..models.plan import TripPlan
from ..models.request import TripRequest
from ..models.weather import CityWeather
from ..providers.base import Chain
from ..providers.geo import resolve_any
from .advisor import AdvisorAnswer, EntertainmentAdvisor
from .gather import gather
from . import enrich


@dataclass
class PlanOutcome:
    request: TripRequest
    plan: TripPlan | None = None
    weather: CityWeather | None = None
    risk: dict[str, Any] = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    filled_by_llm: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)  # 各阶段耗时，优化前必须先能看见

    @property
    def blocked(self) -> bool:
        return bool(self.plan and self.plan.blocking_advice)


class TripService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.chain = Chain(self.settings)
        self.advisor = EntertainmentAdvisor(self.settings, self.chain)
        self.lodging_warning = ""
        self.lodging_ranking: list[dict] = []
        self.lodging_weights: list[tuple[str, float]] = []
        self.lodging_persona_used = ""
        self.last_photo_ms = 0
        self.web_evidence = True  # 关掉可省掉两路联网归纳，延迟砍半

    # ------------------------------------------------------------------
    def plan_trip(self, text: str | TripRequest) -> PlanOutcome:
        import time as _time

        req = text if isinstance(text, TripRequest) else intent.parse(str(text))
        outcome = PlanOutcome(request=req)
        outcome.trace.append(f"诉求：{req.describe() or req.raw_text}")

        # 分段计时：不做度量就没有"优化"，只有"猜"。
        # 每次改并发都会改变瓶颈在哪一段，看得见才敢继续动。
        t_start = _time.time()
        cursor = [t_start]

        def _lap(name: str) -> None:
            now = _time.time()
            outcome.timings[name] = round(now - cursor[0], 1)
            cursor[0] = now

        # 规则解析有缺口时才叫大模型补漏：可预测优先、离线可用、省钱
        if self.settings.dashscope_api_key and (
            not req.destination or not req.interests or req.start_date is None or not req.budget_cents
        ):
            outcome.filled_by_llm = enrich.llm_fill_request(req, self.chain)
            if outcome.filled_by_llm:
                outcome.trace.append("大模型补漏字段：" + "、".join(outcome.filled_by_llm))

        if not req.destination:
            city = resolve_city(req.raw_text or "")
            if city:
                req.destination, req.city_key, req.adcode = city.name, city.key, city.adcode
        if not req.destination:
            # 静态表没命中不等于"不是城市"：切出候选词，逐个问高德地理编码
            for cand in intent.city_candidates(req.raw_text or req.destination):
                probe = resolve_any(cand, self.chain)
                if probe:
                    city = probe
                    req.destination, req.city_key, req.adcode = probe.name, probe.key, probe.adcode
                    outcome.trace.append(f"动态识别城市：{cand} → {probe.name}（adcode {probe.adcode}）")
                    break
        if not req.destination:
            outcome.errors.append("没能识别目的地城市。请说清楚要去哪，例如「成都 3 天 11 月 20 日出发」。")
            return outcome
        # 先查内建表；查不到就用高德地理编码解析全国任意市/县/区，并把结果落盘复用
        city = resolve_any(req.destination, self.chain)
        if not city:
            outcome.errors.append(
                f"城市「{req.destination}」既不在内建坐标表里，高德地理编码也没解析出来。"
                f"请检查写法（如「泉州」而非「泉」），或手动加入 data/fixtures/cities_extra.json。"
            )
            return outcome
        req.city_key, req.adcode, req.destination = city.key, city.adcode, city.name

        # 1.5) 住宿人群与文旅受众：先从用户原话推断，推不出来留空由界面问
        from ..core.audience import Audience, detect_audience
        from ..core.lodging import LodgingPersona, persona_from_text

        persona, persona_reason = persona_from_text(req.raw_text or "")
        if persona:
            req.lodging_persona, req.lodging_reason = persona.value, persona_reason
        aud, aud_reason = detect_audience(req.raw_text or "", req.party)
        if aud and not req.audience:
            req.audience, req.audience_reason = aud.value, aud_reason

        # 1) 并发取数：天气/预警/POI/住宿候选/口碑/文旅项目互不依赖，一次全发出去
        # 预报窗口按"够用就好"算：行程天数 + 2 天缓冲，再加上出发前等待的那几天。
        # 5 天行程没必要拿 16 天预报——多出来的天数既慢又让用户自己挑。
        lead_days = max(0, (req.start_date - date.today()).days) if req.start_date else 0
        horizon = min(16, max(req.days + 2, lead_days + req.days + 2))
        if req.start_date is None:
            horizon = min(16, max(horizon, req.days + 9))  # 没定日期时多拿几天，才有的挑
        bundle = gather(self.chain, city, req, max(1, req.days - 1),
                        web_evidence=self.web_evidence, horizon=horizon)
        _lap("并发取数")
        outcome.trace.extend(bundle.trace())
        weather = CityWeather(
            city=city.name,
            city_key=city.key,
            lat=city.lat,
            lng=city.lng,
            forecast=bundle.forecast.value or [],
            alarms=bundle.alarms.value or [],
            air=bundle.air.value or [],
            notes={"forecast": bundle.forecast.source, "alarm": bundle.alarms.source,
                   "air": bundle.air.source},
        )
        outcome.weather = weather
        outcome.trace.append(f"预报窗口 {horizon} 天：行程 {req.days} 天 + 2 天缓冲（出发前 {lead_days} 天）")
        if not weather.forecast:
            outcome.errors.append("天气数据完全拿不到（网络异常？），已按「天气未知」继续排程。")

        # 2) 出发日推断 + 风险评估（两遍：先全窗口挑日期，再按真实行程窗口判定）
        horizon_dates = [f.date for f in weather.forecast]
        if req.start_date is None and horizon_dates:
            pre = weather_risk.assess(weather, horizon_dates, req.party)
            best = [date.fromisoformat(x) for x in pre.get("best_dates", []) if isinstance(x, str)]
            req.start_date = best[0] if best else min(horizon_dates) + timedelta(days=2)
            outcome.trace.append(f"未指定日期，按舒适指数选 {req.start_date:%m-%d} 出发")
        elif req.start_date is None:
            req.start_date = date.today() + timedelta(days=2)

        window = [req.start_date + timedelta(days=i) for i in range(max(1, req.days))]
        outcome.risk = weather_risk.assess(weather, window, req.party)

        # 3) 候选池：数据已在上面并发取回，这里只做汇总
        pool: dict[Category, list[POIRecord]] = {
            cat: res.value for cat, res in bundle.pois.items() if res.value
        }
        flat = [x for rows in pool.values() for x in rows]
        evidence = bundle.evidence.value or []
        if evidence and flat:
            touched = enrich.attach_evidence(flat, evidence)
            outcome.trace.append(f"口碑提纯：{touched}/{len(flat)} 个地点补齐了预约/时长/避坑字段")

        self.lodging_warning = ""
        # 3.5) 文旅娱乐项目并入候选池：演出/非遗/夜游/节庆/营地要真的能被排进行程，
        #      而不是只在文案里提一句
        cul_rows = bundle.culture.value or []
        for rec in cul_rows:
            pool[rec.category] = pool.get(rec.category, []) + [rec]

        lodging = self.pick_lodging(
            city, req, max(1, req.days - 1),
            rows=list(bundle.hotels.value or []),
            feats=bundle.lodging.value if isinstance(bundle.lodging.value, dict) else None,
        )
        if lodging:
            outcome.trace.append(f"住宿锚点：{lodging.name}（{lodging.display_price()}）")
            if self.lodging_warning:
                outcome.trace.append(self.lodging_warning)
        _lap("选住宿")

        # 4) 装配 + 校验 + 润色
        t_asm = _time.time()
        self.prepare_photos(city, pool, outcome)
        planner = ItineraryPlanner(city, req, weather, self.chain, self.settings)
        plan = planner.plan(pool, lodging=lodging, evidence_ids=[e.id for e in evidence][:40])
        outcome.timings["·装配"] = round(_time.time() - t_asm, 1)
        outcome.trace.append(f"真实通勤：{planner.routes_resolved}/{len(plan.all_slots)} 段并发回查")
        self.attach_photos(city, plan, outcome)
        self.drop_photoless_visual_slots(plan, outcome)
        self.crosscheck_pois(city, plan, outcome)
        self.attach_poems(city, plan, outcome)
        plan.blocking_advice = str(outcome.risk.get("blocking_advice") or "")
        t_val = _time.time()
        plan.scorecard = validator.validate(plan, self.chain, self.settings)
        outcome.timings["·校验"] = round(_time.time() - t_val, 1)
        _lap("装配·实拍图·校验")
        plan.extras["risk_overall"] = str(outcome.risk.get("overall"))
        plan.extras["comfort"] = outcome.risk.get("comfort")
        plan.extras["best_dates"] = outcome.risk.get("best_dates")
        plan.extras["weather_sources"] = {"forecast": weather.notes.get("forecast"), "alarm": weather.notes.get("alarm")}
        plan.sources_used = _dedupe_str(
            [
                f"预报:{weather.notes.get('forecast', 'none')}",
                f"预警:{weather.notes.get('alarm', 'none')}",
                f"地点:{_bundle_source(bundle.pois)}",
                f"口碑:{bundle.evidence.source}",
                f"通勤:{_first_trace_source(self.chain, 'transit')}",
            ]
            + ([f"大模型:{self.settings.llm_model}"] if self.settings.dashscope_api_key else [])
        )

        if self.lodging_warning:
            plan.warnings.append(self.lodging_warning)
        if not flat:
            plan.warnings.append(
                f"「{city.name}」没有命中任何地点数据：配置 AMAP_KEY 可实时检索，"
                f"或在 data/fixtures/{city.key}.json 补一份本地数据。"
            )
        from concurrent.futures import ThreadPoolExecutor

        from . import reasons

        aud = Audience(req.audience) if req.audience else None

        def _do_reasons() -> tuple[int, str]:
            if aud is None:
                return 0, "未指定人群，沿用数据依据"
            t = _time.time()
            # 每个线程各持一个 Chain：Chain.log 会被并发 append，共用实例会让降级轨迹串味
            got = reasons.personalize(plan, aud.label, aud.ask, Chain(self.settings))
            outcome.timings["·推荐语"] = round(_time.time() - t, 1)
            return got

        def _do_narrate() -> str:
            t = _time.time()
            if not self.settings.dashscope_api_key:
                plan.narrative = _template_narrative(plan)
                outcome.timings["·整体点评"] = 0.0
                return "未配置 key，使用模板文案"
            out = "已生成" if enrich.narrate(plan, Chain(self.settings)) else "失败，使用模板文案"
            outcome.timings["·整体点评"] = round(_time.time() - t, 1)
            return out

        # 推荐语与整体点评互不依赖（一个改逐条理由，一个写总评），串行等于白等两次 LLM
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_reason = pool.submit(_do_reasons)
            f_narrate = pool.submit(_do_narrate)
            _touched, note = f_reason.result()
            narrate_note = f_narrate.result()
        if aud:
            plan.extras["audience"] = aud.label
            outcome.trace.append(f"受众推荐语：{note}（依据：{req.audience_reason or '用户选择'}）")
        plan.extras["culture_count"] = len(cul_rows)
        outcome.trace.append(f"大模型点评：{narrate_note}")
        _lap("推荐语与点评")
        outcome.timings["总计"] = round(_time.time() - t_start, 1)
        outcome.trace.append("耗时：" + " ｜ ".join(f"{k} {v}s" for k, v in outcome.timings.items()))
        outcome.plan = plan
        return outcome

    # ------------------------------------------------------------------
    def prepare_photos(self, city: CityInfo, pool: dict, outcome: PlanOutcome) -> int:
        """装配前一次性把图备齐：补图 → 打分 → 淘汰不合格 → 剔除无图的视觉候选。

        为什么必须放在装配之前、而且只做一次：
        1. 用户要求"没有图就不要提供这个活动"。装配器看不到图就无法避开无图候选；
           放在装配后处理，要么留下空洞卡片，要么删完出现空白日。
        2. 上一版把质检留在装配后，同一批图被过两趟管线，实测"装配·实拍图·校验"涨到 27.8s；
           更糟的是预筛时看着有图、质检后变成 0 图的候选（三亚千古情）还是被排进了行程。
           合并成一次之后，"入池"就等于"有合格图"。

        返回被剔除的候选数。餐饮与购物不强制有图——否则会出现"排不出饭吃"的行程。
        """
        import time as _time

        from ..models.enums import Category
        from ..providers.images import screen_images

        visual = (Category.SIGHT, Category.MUSEUM, Category.PARK, Category.HISTORIC,
                  Category.SHOW, Category.AMUSEMENT, Category.ESCAPE, Category.SPORTS,
                  Category.CINEMA, Category.BAR, Category.KTV, Category.CLUB)
        rows = [p for cat, lst in pool.items() if cat in visual for p in lst]
        if not rows:
            return 0
        t0 = _time.time()

        # 1) 主检索没带图的（大模型归纳出来的文旅条目居多）：按名字回查一次
        missing = [p for p in rows if not p.photos]
        if missing:
            res = self.chain.run("images", "fetch_batch", city, [p.name for p in missing][:14], 6)
            table = res.value or {}
            for poi in missing:
                urls = table.get(poi.name) or []
                if urls:
                    poi.photos = urls

        # 2) 只探前 3 张判"有没有脸面进池子"；剩下 3 张留给装配后那批真被排上的地点去下。
        #    以前这里对 24 个候选的 6 张图全量打分，冷启动实测 26.7s，把整趟规划顶到 55s。
        rows.sort(key=lambda p: -(p.rating or 0))
        screened = rows[:24]
        emptied, touched = screen_images(self.chain, self.settings, screened)
        self.last_photo_ms = int((_time.time() - t0) * 1000)

        # 3) 一张合格图都没有的视觉候选，不进候选池
        drop_names = {p.name for p in screened if not p.photos}
        if drop_names:
            for cat in list(pool):
                if cat in visual:
                    pool[cat] = [p for p in pool[cat] if p.name not in drop_names]
            outcome.trace.append(
                f"实拍图预筛：{len(drop_names)} 个候选查不到合格公开图，已不进候选"
                f"（{'、'.join(sorted(drop_names))[:60]}）"
            )
        outcome.trace.append(
            f"实拍图：预筛 {touched} 个候选（每只探 3 张）淘汰 {emptied} 个无合格图候选，"
            f"耗时 {self.last_photo_ms / 1000:.1f}s"
        )
        return len(drop_names)

    # ------------------------------------------------------------------
    def drop_photoless_visual_slots(self, plan: TripPlan, outcome: PlanOutcome) -> int:
        """最后一道兜底：视觉类时段最终一张合格图都没有，就从行程里去掉。

        用户的原话是"数据库没有图片就不要提供这个活动"。
        预筛只覆盖装配前的候选池，装配后仍可能出现"图全被质检淘汰"的情况，
        所以这里再兜一次；餐饮/住宿/交通不在此列（没图也得吃饭睡觉）。

        两个刻意的保守处理：
        - 去掉之后当天一个可玩项都不剩，就**不删**——空白天比少一张图更糟；
        - 删完必须重算 seq 与预算分项，否则界面还会显示已被去掉那项的钱。
        """
        from ..core.planner import _breakdown
        from ..models.enums import Category, SlotType

        keep_types = {SlotType.SIGHT, SlotType.SHOW, SlotType.NIGHTLIFE}
        removed = 0
        for day in plan.days:
            survivors = []
            for s in day.slots:
                photoless = (
                    s.type in keep_types
                    and s.poi is not None
                    and s.poi.category is not Category.FOOD
                    and not s.poi.photos
                )
                if photoless:
                    removed += 1
                    outcome.trace.append(f"实拍图兜底：「{s.title[:20]}」没有合格公开图，已从行程去掉")
                    continue
                survivors.append(s)
            if len(survivors) == len(day.slots):
                continue
            if not any(s.type in keep_types for s in survivors):
                continue          # 宁可留一张空洞卡片，也不排出一个空白日
            day.slots = survivors
            for i, s in enumerate(day.slots, start=1):
                s.seq = i
            day.recompute_totals()
        if removed:
            lodging = plan.days[0].lodging if plan.days else None
            plan.budget_breakdown = _breakdown(plan.days, lodging, plan.request)
        return removed

    # ------------------------------------------------------------------
    def attach_photos(self, city: CityInfo, plan: TripPlan, outcome: PlanOutcome) -> int:
        """给排定的每个活动补实拍图。

        主检索（高德）已经带图，缺图的主要是大模型归纳出来的文旅项目——
        它们没有坐标，所以按名字回查一次高德 POI 拿同一批公开图。
        """
        from ..providers.images import filter_poi_photos

        wanted = [s.poi for s in plan.all_slots if s.poi and s.type.label not in ("入住退房", "交通移动")]
        targets = [p for p in wanted if not p.photos]
        filled = 0
        if targets:
            import time as _time

            t = _time.time()
            res = self.chain.run("images", "fetch_batch", city, [p.name for p in targets][:14], 6)
            self.last_photo_ms = int((_time.time() - t) * 1000)
            table = res.value or {}
            for poi in targets:
                urls = table.get(poi.name) or []
                if urls:
                    poi.photos = urls
                    filled += 1
            outcome.trace.append(
                f"实拍图：{filled}/{len(targets)} 个活动补到图，耗时 {self.last_photo_ms / 1000:.1f}s"
                f"（来源 {res.source if filled else 'none'}）"
            )
        # 高德检索直接带回来的图同样要过清晰度闸门：糊图比没图更伤观感
        dropped, touched = filter_poi_photos(self.chain, self.settings, wanted)
        if touched:
            outcome.trace.append(f"图片质检：{touched} 个地点共淘汰 {dropped} 张糊图/小图")
        return filled

    # ------------------------------------------------------------------
    def crosscheck_pois(self, city: CityInfo, plan: TripPlan, outcome: PlanOutcome) -> dict:
        """用腾讯位置服务交叉校验已排进行程的地点（没配 Key 时整步跳过）。

        只校验真正会显示出来的前 8 家：腾讯个人版配额与并发都有限，
        为看不见的候选花钱买数据没有意义。
        """
        from ..core.crosscheck import reconcile

        pois = [s.poi for s in plan.all_slots if s.poi and s.type.label not in ("入住退房", "交通移动")]
        if not pois:
            return {}
        res = self.chain.run("poi_crosscheck", "check", city, [p.name for p in pois][:4])
        table = res.value or {}
        if not table:
            # 配了 Key 却一条都没校验上（Key 错、配额用尽、字段变了）必须留痕：
            # 静默跳过会让站长以为双源校验在跑，实际早就断了
            failed = [a for a in res.attempts if a.provider == "tencent" and not a.ok]
            if failed:
                outcome.trace.append(f"双源校验失败（腾讯）：{failed[0].error[:120]}")
            elif res.source == "tencent":
                outcome.trace.append("双源校验：腾讯没查到对应地点，本次未做交叉校验")
            return {}
        stats = reconcile(pois, table)
        outcome.trace.append(
            f"双源校验（腾讯）：{len(table)} 家比对，评分一致 {stats['agree']}、分歧 {stats['conflict']}、"
            f"补到人均 {stats['filled']}、另一地图也收录 {stats['exists']}"
        )
        return stats

    # ------------------------------------------------------------------
    def attach_poems(self, city: CityInfo, plan: TripPlan, outcome: PlanOutcome) -> int:
        """诗词彩蛋：把本地公版联句按地点挂到时段上（零网络、零 token、约 1 毫秒）。

        刻意不做成"每天一句"：库里成都只有 6 条，硬摊到 12 个时段必然重复，
        重复的装饰比没有装饰更像机器生成的。挂不上的时段就空着。
        """
        from ..core.poetry import attach

        res = self.chain.run("poetry", "lookup", city)
        entries = res.value or []
        if not entries:
            return 0
        n = attach(plan, entries)
        if n:
            outcome.trace.append(f"诗词彩蛋：{n} 个时段挂上本地联句（{city.name} 库内 {len(entries)} 条）")
        return n

    # ------------------------------------------------------------------
    def fetch_weather(self, city: CityInfo, horizon: int = 16) -> tuple[CityWeather, list[str]]:
        fr = self.chain.run("forecast", "fetch_forecast", city, horizon)
        ar = self.chain.run("alarm", "fetch_alarms", city, allow_empty=True)
        weather = CityWeather(
            city=city.name,
            city_key=city.key,
            lat=city.lat,
            lng=city.lng,
            forecast=fr.value or [],
            alarms=ar.value or [],
            notes={"forecast": fr.source, "alarm": ar.source},
        )
        trace = [
            f"预报：{fr.source} {len(fr.value or [])} 天"
            + ("" if fr.source == "open_meteo" else "（含预警能力）"),
            f"预警：{ar.source} {len(ar.value or [])} 条",
        ]
        return weather, trace

    def collect_pois(self, city: CityInfo, req: TripRequest) -> tuple[dict[Category, list[POIRecord]], list[str]]:
        cats = categories_for(req.interests or [Interest.PHOTO, Interest.FOOD, Interest.HISTORY])
        for must in (Category.SIGHT, Category.FOOD):
            if must not in cats:
                cats.insert(0, must)
        if req.nightlife and not any(c in NIGHT_CATEGORIES for c in cats):
            cats += [Category.BAR, Category.SHOW]
        cats = cats[:9]

        pool: dict[Category, list[POIRecord]] = {}
        trace: list[str] = []
        for cat in cats:
            q = POIQuery(
                city=city.name,
                category=cat,
                keywords=([req.lodging_area] if cat is Category.HOTEL and req.lodging_area else []),
                limit=22 if cat in (Category.SIGHT, Category.FOOD) else 14,
            )
            res = self.chain.run("poi", "search", city, q, model=POIRecord)
            if res.value:
                pool[cat] = res.value
                trace.append(f"POI {cat.value}：{res.source} {len(res.value)} 条")
        return pool, trace

    def collect_evidence(self, city: CityInfo, req: TripRequest) -> tuple[list[EvidenceRecord], str]:
        topics = [f"{req.destination} 必去景点 预约 门票 避坑"]
        if req.has_interest(Interest.FOOD) or not req.interests:
            topics.append(f"{req.destination} 特色美食 人均 排队")
        if req.nightlife or req.has_interest(Interest.NIGHTLIFE):
            topics.append(f"{req.destination} 酒吧 KTV 夜生活 营业时间 评价")
        if req.has_interest(Interest.PHOTO):
            topics.append(f"{req.destination} 网红打卡点 最佳时段")
        # 联网归纳单次约 30s，3 个 topic 串行会把一次规划拖到 2 分钟以上；
        # 本地库命中时（前 6 城）这里根本不会联网。
        res = self.chain.run("evidence", "collect", city, topics[:2], limit=10, model=EvidenceRecord)
        return (res.value or []), (res.source if res.value else "")

    def pick_lodging(
        self, city: CityInfo, req: TripRequest, nights: int, rows: list[POIRecord] | None = None,
        feats: dict | None = None,
    ) -> POIRecord | None:
        """按「住宿偏好人群」的多特征加权模型选住宿，而不是只按评分取第一。

        特征 = 高德结构化字段 + 通义千问联网归纳的六维口碑；
        权重 = core/lodging.py 的人群先验，用户反馈过"选它/不选它"之后在线纠偏。
        feats 由 gather() 并发预取，没预取到才在这里补一次。
        """
        from ..core.lodging import FEATURE_LABEL, LodgingFeatures, LodgingPersona, LodgingRanker, extract_structural
        from ..core.lodging import pick_candidates

        q = POIQuery(
            city=city.name,
            category=Category.HOTEL,
            keywords=[req.lodging_area] if req.lodging_area else [],
            limit=16,
        )
        rows: list[POIRecord] = list(rows) if rows is not None else (
            self.chain.run("poi", "search", city, q, model=POIRecord).value or []
        )
        if not rows:
            return None
        cands, cap, warning = pick_candidates(rows, req, nights)
        self.lodging_warning = warning

        if feats is None:
            scored = self.chain.run("lodging", "score", city.name, cands)
            feats = scored.value if isinstance(scored.value, dict) else {}
        center = GeoPoint(city.lat, city.lng)
        pairs = []
        for h in cands:
            dist = center.distance_m_to(h.point) if h.point else None
            struct = extract_structural(h, dist, cap)
            f = feats.get(h.name) or struct
            for name in struct.known:
                if name not in f.known:
                    f.values[name] = struct.values[name]
                    f.known.add(name)
            for k, v in struct.note.items():
                f.note.setdefault(k, v)
            pairs.append((h, f))

        persona = LodgingPersona(req.lodging_persona or "综合")
        ranker = LodgingRanker(self.settings.data_dir / "lodging_model.json")
        ranked = ranker.rank(pairs, persona)
        self.lodging_ranking = [
            {
                "name": h.name,
                "score": round(s, 4),
                "coverage": round(f.coverage, 2),
                "price": f.note.get("price") or f.note.get("price_hint") or "价格待确认",
                "evidence": f.note.get("reviews_evidence", ""),
                "top": "、".join(
                    f"{FEATURE_LABEL[k]} {v:.2f}" for k, w, v in sorted(parts, key=lambda x: -x[2])[:3]
                ),
                "features": dict(f.values),
                "known": sorted(f.known),
            }
            for h, f, s, parts in ranked
        ]
        self.lodging_persona_used = persona.value
        self.lodging_weights = [(FEATURE_LABEL[k], round(v, 3)) for k, v in ranker.weights_table(persona)]
        if not any((h.price_cents or 0) > 0 for h, *_ in ranked) and not self.lodging_warning:
            self.lodging_warning = (
                f"「{city.name}」的住宿数据没有可用价格（高德 business.cost 缺失），"
                f"预算校验只覆盖门票与餐饮；住宿请另按 ¥{(cap or 0) / 100:.0f}/间/晚 以内自行确认。"
            )
        return ranked[0][0]

    # ------------------------------------------------------------------
    def ask(self, text: str, city: str | None = None, category: Category | None = None, limit: int = 5) -> AdvisorAnswer:
        return self.advisor.ask(text, city_hint=city, category=category, limit=limit)

    def chain_trace(self) -> list[str]:
        """完整降级轨迹（谁试过、谁失败），路演时能直接展示"不是黑箱"。"""
        return self.chain.trace_lines()

    def doctor(self) -> list[tuple[str, str, bool, str]]:
        """自检表：能力 / provider / 是否可用 / 说明。CLI 与 UI 都展示它。"""
        rows: list[tuple[str, str, bool, str]] = []
        for capability in ("forecast", "air_quality", "alarm", "poi", "poi_crosscheck",
                           "images", "transit", "evidence", "guide", "poetry", "llm"):
            for name, inst in self.chain.providers_for(capability):
                try:
                    ok, msg = inst.describe()
                except Exception as exc:  # provider 自检本身出错也不该让整个 doctor 崩
                    ok, msg = False, f"自检异常 {exc}"
                rows.append((capability, name, ok, msg or ("可用" if ok else "不可用")))
        return rows


def _dedupe_str(rows: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _bundle_source(pois: dict) -> str:
    """候选池里实际生效的数据源名（并发取数后没有单一 trace 字符串可用了）。"""
    sources = [res.source for res in pois.values() if res.value]
    if not sources:
        return "none"
    primary = max(set(sources), key=sources.count)
    return primary if len(set(sources)) == 1 else f"{primary}+{len(set(sources))}源"


def _poi_source(traces: list[str]) -> str:
    """从 "POI 风景名胜：fixture 22 条" 这类轨迹里取出 provider 名。"""
    for line in traces:
        if "：" in line:
            return line.split("：", 1)[1].split()[0]
    return "none"


def _first_trace_source(chain: Chain, capability: str) -> str:
    for a in chain.log:
        if a.capability == capability and a.ok:
            return a.provider
    return "none"


def _template_narrative(plan: TripPlan) -> str:
    """零 key 也要有"整体点评"，但必须是事实汇总，不是编故事。"""
    req = plan.request
    days = len(plan.days)
    slots = plan.all_slots
    night = sum(1 for s in slots if s.type.value in ("夜间娱乐", "观看演出"))
    indoor = sum(1 for s in slots if s.indoor)
    cost = plan.total_cost_cents
    bits = [f"{req.destination}{days} 天，共 {len(slots)} 项安排（其中夜间娱乐 {night} 项、室内 {indoor} 项）"]
    if cost:
        from ..models.common import fmt_cny

        bits.append(f"，预计花费 {fmt_cny(cost)}")
    if req.budget_cents:
        ratio = cost / req.budget_cents
        bits.append(f"，占预算 {ratio:.0%}")
    if plan.blocking_advice:
        bits.append("。⚠️ 天气存在风险，请先看结论再决定是否出发")
    score = plan.scorecard
    if score:
        bits.append(f"。方案校验：{score.verdict()}（通过 {score.pass_rate():.0%}）")
    return "".join(bits) + "。"
