"""生活娱乐问答（LifeHack 主战场）：「推荐个评分高、性价比好的酒吧」也能答。

与行程规划的差别只在入口：这里不排时间轴，只做「检索 → 按诉求排序 → 归纳口碑」，
所以能即时回答；同一套 POI provider、同一套打分函数，避免两条逻辑长期分叉。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings, get_settings
from ..core import intent
from ..knowledge.cities import CityInfo, resolve_city
from ..knowledge.poi_codes import AMAP_CATEGORY, categories_for
from ..models.common import GeoPoint
from ..models.enums import Category, Interest
from ..models.evidence import EvidenceRecord
from ..models.poi import POIQuery, POIRecord
from ..models.request import TripRequest
from ..providers.base import Chain
from ..providers.geo import resolve_any

_PER_ITEM_SUMMARY_PROMPT = "下面是「{city}」的{cat}候选（JSON）。请用中文写 80-150 字总结：\
先给一句结论（哪家最稳、适合什么场景），再点出 2-3 家各自特色与价位差异，最后给一条实用提醒\
（如需身份证/末班交通/人均区间）。只依据给定数据，不编造。数据：{rows}"


@dataclass
class Recommendation:
    poi: POIRecord
    score: float
    reasons: list[str] = field(default_factory=list)
    reviews: list[EvidenceRecord] = field(default_factory=list)
    transit_note: str = ""


@dataclass
class AdvisorAnswer:
    question: str
    city: str = ""
    category: str = ""
    items: list[Recommendation] = field(default_factory=list)
    summary: str = ""
    sources_used: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.items)


class EntertainmentAdvisor:
    def __init__(self, settings: Settings | None = None, chain: Chain | None = None) -> None:
        self.settings = settings or get_settings()
        self.chain = chain or Chain(self.settings)

    def ask(
        self,
        text: str,
        city_hint: str | None = None,
        category: Category | None = None,
        limit: int = 5,
        anchor: CityInfo | None = None,
    ) -> AdvisorAnswer:
        city_name, detected_cat, prefs = intent.detect_query(text)
        city_name = city_name or city_hint or ""
        # 与行程规划同一条解析链：内建表 → 高德地理编码，保证"全国任意城市"在问答里也成立
        city = resolve_any(city_name, self.chain)
        cat = category or detected_cat
        answer = AdvisorAnswer(question=text, city=city_name, category=cat.value if cat else "综合推荐")

        if not city:
            answer.warnings.append("没识别出城市，请在问题里带上城市名（例如「成都有哪些评价好的清吧」）。")
            return answer
        if cat is None:
            cat = self._guess_category_from_need(text)
            answer.category = cat.value

        keywords = _extract_keywords(text, cat)
        query = POIQuery(
            city=city.name,
            category=cat,
            keywords=keywords,
            near=None,
            limit=max(20, limit * 4),
        )
        res = self.chain.run("poi", "search", city, query, model=POIRecord)
        cands: list[POIRecord] = res.value or []
        answer.sources_used = [f"poi:{res.source}"]
        if not cands:
            answer.warnings.append(
                f"「{city.name}·{cat.value}」暂无可检索数据。配置 AMAP_KEY 后这里会换成高德实时 POI（含评分/人均/营业时间）。"
            )
            return answer

        req = TripRequest(
            raw_text=text,
            destination=city.name,
            city_key=city.key,
            adcode=city.adcode,
            nightlife=True,
            interests=self._interests_for(cat, prefs),
        )
        ev_res = self.chain.run("evidence", "collect", city, [cat.value, *keywords], limit=24)
        evidence: list[EvidenceRecord] = ev_res.value or []
        if evidence:
            answer.sources_used.append(f"evidence:{ev_res.source}")

        from ..core.scoring import ScoreContext, score

        ranked: list[Recommendation] = []
        for poi in cands:
            s = score(poi, ScoreContext(request=req, slot="night" if cat.group == "night" else "day"))
            reasons = list(s.reasons)
            if prefs.get("cheap") and poi.avg_cost_cents and poi.avg_cost_cents <= 15000:
                s.value += 25
                reasons.append("人均 ≤¥150，符合性价比诉求")
            if prefs.get("high_rating") and (poi.rating or 0) >= 4.5:
                s.value += 30
                reasons.append("评分 ≥4.5")
            if prefs.get("quiet") and poi.category is Category.CLUB:
                s.value -= 40
                reasons.append("夜店氛围吵，与「安静」诉求冲突")
            if prefs.get("quiet") and any(t in " ".join(poi.tags) for t in ("清吧", "安静", "聊天", "小酒馆")):
                s.value += 25
                reasons.append("标签含清吧/安静")
            if prefs.get("lively") and poi.category in (Category.CLUB, Category.KTV):
                s.value += 25
            if prefs.get("open_late"):
                if poi.open_to_minute is not None and poi.open_to_minute <= (poi.open_from_minute or 0):
                    s.value += 30
                    reasons.append("营业跨零点")
                elif (poi.open_to_minute or 0) >= 23 * 60:
                    s.value += 15
            if prefs.get("kid") and poi.kid_friendly:
                s.value += 30
                reasons.append("亲子友好")
            if anchor and poi.point:
                d = anchor_center(city).distance_m_to(poi.point)
                if prefs.get("near_hotel"):
                    s.value -= d / 1000 * 3
                    if d <= 2000:
                        reasons.append(f"距中心约 {d / 1000:.1f}km，步行可达")
            ev = [e for e in evidence if e.poi_name and (e.poi_name in poi.name or poi.name in e.poi_name)]
            ranked.append(Recommendation(poi=poi, score=s.value, reasons=reasons, reviews=ev[:2]))

        ranked.sort(key=lambda r: -r.score)
        answer.items = ranked[: max(1, limit)]
        answer.summary = self._summarize(city, cat, answer.items) or _template_summary(city, cat, answer.items, prefs)
        _append_compliance_notes(answer)
        return answer

    # ---------- 内部 ----------
    def _guess_category_from_need(self, text: str) -> Category:
        low = text.lower()
        if any(k in low for k in ("带孩子", "亲子", "小朋友")):
            return Category.AMUSEMENT
        if any(k in low for k in ("吃", "餐", "夜宵", "小吃")):
            return Category.FOOD
        if any(k in low for k in ("玩", "逛", "景点")):
            return Category.SIGHT
        return Category.SIGHT

    def _interests_for(self, cat: Category | None, prefs: dict[str, bool]) -> list[Interest]:
        out: list[Interest] = []
        if cat and cat.group == "night":
            out.append(Interest.NIGHTLIFE)
        if cat is Category.AMUSEMENT:
            out.append(Interest.THEME_PARK)
        if cat in (Category.FOOD, Category.CAFE):
            out.append(Interest.FOOD)
        if cat in (Category.MUSEUM, Category.HISTORIC):
            out.append(Interest.HISTORY)
        if prefs.get("cheap"):
            out.append(Interest.BUDGET)
        if prefs.get("kid"):
            out.append(Interest.FAMILY)
        return out or categories_for([Interest.PHOTO])

    def _summarize(self, city: CityInfo, cat: Category, items: list[Recommendation]) -> str:
        if not items or not self.settings.dashscope_api_key:
            return ""
        rows = [
            {
                "name": r.poi.name,
                "rating": r.poi.rating,
                "avg_yuan": round((r.poi.avg_cost_cents or r.poi.ticket_cents or 0) / 100),
                "open": r.poi.open_hours_raw,
                "tags": r.poi.tags,
                "why": r.poi.why,
                "pitfalls": r.poi.pitfalls,
                "reviews": [e.brief(80) for e in r.reviews],
            }
            for r in items
        ]
        import json

        messages = [
            {"role": "system", "content": "你是本地玩乐顾问，中文回答，务实不吹。"},
            {
                "role": "user",
                "content": _PER_ITEM_SUMMARY_PROMPT.format(
                    city=city.name, cat=cat.value, rows=json.dumps(rows, ensure_ascii=False)
                ),
            },
        ]
        res = self.chain.run("llm", "chat", messages, temperature=0.5, max_tokens=600)
        return str(res.value or "")[:600]


def anchor_center(city: CityInfo) -> GeoPoint:
    return GeoPoint(city.lat, city.lng)


def _extract_keywords(text: str, cat: Category | None) -> list[str]:
    """把口语词翻成检索词：去掉城市名与助词，保留有区分度的名词。"""
    stop = {t for t in ("推荐", "有没有", "哪些", "什么", "好的", "评价", "口碑", "附近", "周边", "求", "帮", "介绍")}
    words: list[str] = []
    for token in (text or "").replace("，", ",").replace("、", ",").split(","):
        for piece in token.split(" "):
            p = piece.strip()
            if len(p) >= 2 and p not in stop and not resolve_city(p):
                words.append(p)
    if cat:
        _, defaults = AMAP_CATEGORY.get(cat, ("", ""))
        words += [w for w in defaults.split(",") if w]
    return list(dict.fromkeys(words))[:8]


def _template_summary(city: CityInfo, cat: Category, items: list[Recommendation], prefs: dict) -> str:
    """没有 LLM 时的模板归纳——保证「问酒吧」这种即时问答在零 key 下依然有结论。"""
    if not items:
        return ""
    top = items[0]
    bits = [f"在{city.name}按「{cat.value}」筛出 {len(items)} 处，优先推荐 {top.poi.name}"]
    det = []
    if top.poi.rating:
        det.append(f"评分 {top.poi.rating}")
    price = top.poi.avg_cost_cents or top.poi.ticket_cents
    if price:
        from ..models.common import fmt_cny

        det.append(f"人均 {fmt_cny(price)}")
    if top.poi.open_hours_raw:
        det.append(f"营业 {top.poi.open_hours_raw}")
    if det:
        bits.append("（" + "、".join(det) + "）")
    if top.poi.why:
        bits.append(f"，理由：{top.poi.why}")
    if top.reviews:
        bits.append(f"；网友反馈：{top.reviews[0].brief(70)}")
    if len(items) > 1:
        bits.append(f"。次选 {items[1].poi.name}")
        second_price = items[1].poi.avg_cost_cents
        if prefs.get("cheap") and second_price and price and second_price < price:
            from ..models.common import fmt_cny

            bits.append(f"（{fmt_cny(second_price)}，更省钱）")
    if any(r.poi.pitfalls for r in items):
        bits.append(f"。注意：{next(r.poi.pitfalls for r in items if r.poi.pitfalls)[0]}")
    return "".join(bits) + "。"


def _append_compliance_notes(answer: AdvisorAnswer) -> None:
    pois = [r.poi for r in answer.items]
    if any(p.requires_id or p.category in (Category.BAR, Category.CLUB, Category.KTV) for p in pois):
        answer.warnings.append("酒吧/夜店/KTV 需实名核验身份证，人证不一致不得接待；未成年人禁入。")
    if any(p.category is Category.CLUB for p in pois):
        answer.warnings.append("夜店散场多在 01:00-02:00，请提前确认末班地铁或安排代驾/出租车。")
    if any((p.open_to_minute or 0) >= 23 * 60 or ((p.open_to_minute or 0) < (p.open_from_minute or 0)) for p in pois):
        answer.warnings.append("跨零点营业的场所，建议把住宿选在 3km 内，压缩深夜打车成本。")
