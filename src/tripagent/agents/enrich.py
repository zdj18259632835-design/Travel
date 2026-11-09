"""UGC 提纯与大模型增强层。

两步都设计成「拿不到 LLM 也必须优雅退化」：
- refine_pois：把口碑里的 预约/时长/避坑/最佳时段 落到 POI 字段上；
- narrate：生成整体点评与逐日一句话亮点；失败时用模板文案顶上。
"""

from __future__ import annotations

import json
from typing import Any

from ..models.evidence import EvidenceRecord
from ..models.enums import Category, Severity
from ..models.plan import TripPlan
from ..models.poi import POIRecord
from ..providers.base import Chain


def attach_evidence(pool: list[POIRecord], evidence: list[EvidenceRecord]) -> int:
    """按名称匹配，把证据挂到 POI 上并回填结构化字段。返回被增强的 POI 数量。"""
    by_name: dict[str, list[EvidenceRecord]] = {}
    for e in evidence:
        key = (e.poi_name or "").strip()
        if key:
            by_name.setdefault(key, []).append(e)
    touched = 0
    for poi in pool:
        hits = _match_hits(poi, by_name)
        if not hits:
            continue
        touched += 1
        poi.evidence_ids = list({*poi.evidence_ids, *[h.id for h in hits]})
        for h in hits:
            ex = h.extracted or {}
            _apply(poi, ex)
            if h.snippet and (not poi.why or len(poi.why) < 20):
                poi.why = h.brief(90)
            if h.tags:
                poi.tags = list(dict.fromkeys([*poi.tags, *h.tags]))[:10]
    return touched


def _match_hits(poi: POIRecord, by_name: dict[str, list[EvidenceRecord]]) -> list[EvidenceRecord]:
    out: list[EvidenceRecord] = []
    name = poi.name.strip()
    for key, rows in by_name.items():
        k = key.strip()
        if not k:
            continue
        if k == name or k in name or name in k:
            out.extend(rows)
    return out[:4]


def _apply(poi: POIRecord, ex: dict[str, Any]) -> None:
    stay = ex.get("stay_minutes")
    if isinstance(stay, (int, float)) and 15 <= stay <= 600:
        poi.stay_minutes = int(stay)
    if ex.get("need_reservation") is True:
        poi.need_reservation = True
        poi.reservation_channel = poi.reservation_channel or str(ex.get("reservation_channel") or "官方公众号/小程序")
        lead = ex.get("reservation_lead_days")
        if isinstance(lead, (int, float)) and 0 <= lead <= 30:
            poi.reservation_lead_days = int(lead)
        cutoff = ex.get("reservation_cutoff")
        if isinstance(cutoff, str) and cutoff:
            poi.reservation_cutoff = cutoff[:20]
    pitfalls = ex.get("pitfalls")
    if isinstance(pitfalls, list):
        clean = [str(p)[:60] for p in pitfalls if p]
        poi.pitfalls = list(dict.fromkeys([*poi.pitfalls, *clean]))[:6]
    photos = ex.get("photo_spots")
    if isinstance(photos, list):
        poi.photo_spots = list(dict.fromkeys([*poi.photo_spots, *[str(p)[:50] for p in photos if p]]))[:6]
    best = ex.get("best_time")
    if isinstance(best, str) and best:
        poi.best_time = best[:12]
    verdict = ex.get("verdict")
    if isinstance(verdict, str) and verdict:
        if "劝退" in verdict:
            poi.popularity = max(0.05, poi.popularity - 0.3)
        elif "值得" in verdict:
            poi.popularity = min(1.0, poi.popularity + 0.15)


NARRATE_PROMPT = """你是行程规划师的文案助手。基于以下 JSON 行程，输出 JSON：
{{"narrative":"120-200字整体点评，指出这条线路的节奏、亮点与最大风险",\
"day_headlines":["每天一句话亮点，顺序与 days 对应"],\
"extra_tips":["3-6条针对这条行程的具体提醒，如排队时段、购票、交通切换"]}}
要求：只依据给定数据，不要编造价格与营业时间；语气务实、中文。\n行程 JSON：\n{plan}"""


def narrate(plan: TripPlan, chain: Chain, max_days: int = 7) -> bool:
    """成功返回 True；无 key/失败时保持模板文案，不抛异常。"""
    payload = _compact_plan(plan, max_days)
    messages = [
        {"role": "system", "content": "只输出合法 JSON，中文。"},
        {"role": "user", "content": NARRATE_PROMPT.format(plan=json.dumps(payload, ensure_ascii=False))},
    ]
    # 用 chat + 容错抽取，而不是 json_call：强制 response_format=json_object 时
    # qwen 会把长中文内容截断成不合法 JSON（实测"大模型点评：失败"就是这个）
    res = chain.run("llm", "chat", messages, temperature=0.5)
    raw = str(res.value or "")
    try:
        from ..providers.llm import extract_json

        data = extract_json(raw) if raw else None
    except Exception:
        data = None
    if not isinstance(data, dict):
        return False
    text = str(data.get("narrative", "")).strip()
    if not text:
        return False
    plan.narrative = text[:800]
    heads = data.get("day_headlines")
    if isinstance(heads, list):
        for day, head in zip(plan.days, heads):
            day.theme = f"{day.theme}｜{str(head)[:40]}" if head else day.theme
    tips = data.get("extra_tips")
    if isinstance(tips, list):
        plan.packing = list(dict.fromkeys([*plan.packing, *[str(t)[:80] for t in tips if t]]))[:20]
    plan.extras["llm_narrated"] = True
    return True


LLM_REQUEST_PROMPT = """用户在规划旅行时说了下面这句话，请抽取信息并只输出 JSON：
{{"destination":"城市名（没有则空）","origin":"出发城市（没有则空）","days":整数或null,\
"start_date":"YYYY-MM-DD或null","budget_total_yuan":数字或null,"budget_per_head":true/false,\
"adults":整数,"kids":整数,"elders":整数,"pets":整数,"accessible":true/false,\
"interests":["从 美食探店/历史人文/山水自然/网红打卡/夜生活/亲子遛娃/主题乐园/展演活动/购物血拼/市井慢生活/户外徒步/温泉养生/无障碍友好/携宠出行/预算敏感 中选"],\
"nightlife":true/false,"pace":"relaxed|normal|packed","must_visit":["点名要去的地方"],"avoid":["明确排斥的东西"],\
"lodging_area":"想住的商圈或null","notes":"其它无法归类但有用的诉求"}}\n原话：{text}"""


def llm_fill_request(req, chain) -> list[str]:
    """规则解析缺字段时用 LLM 补齐；返回被补字段名列表。"""
    from ..models.request import TripRequest
    from ..knowledge.cities import resolve_city

    if not chain.settings.dashscope_api_key:
        return []
    messages = [
        {"role": "system", "content": "只输出合法 JSON，中文。"},
        {"role": "user", "content": LLM_REQUEST_PROMPT.format(text=req.raw_text or "")},
    ]
    res = chain.run("llm", "json_call", messages, temperature=0.2)
    data = res.value
    if not isinstance(data, dict):
        return []
    filled: list[str] = []
    from datetime import datetime

    if not req.destination and data.get("destination"):
        city = resolve_city(str(data["destination"]))
        if city:
            req.destination, req.city_key, req.adcode = city.name, city.key, city.adcode
            filled.append("destination")
    if not req.origin_city and data.get("origin"):
        req.origin_city = str(data["origin"])[:20]
        filled.append("origin")
    if data.get("days") and req.days == 3:
        try:
            req.days = max(1, min(int(data["days"]), 30))
            filled.append("days")
        except (TypeError, ValueError):
            pass
    if req.start_date is None and data.get("start_date"):
        try:
            req.start_date = datetime.strptime(str(data["start_date"])[:10], "%Y-%m-%d").date()
            filled.append("start_date")
        except ValueError:
            pass
    if not req.budget_cents and data.get("budget_total_yuan"):
        try:
            total = float(data["budget_total_yuan"])
            if data.get("budget_per_head"):
                total *= req.party.head_count
            req.set_budget_yuan(total)
            filled.append("budget")
        except (TypeError, ValueError):
            pass
    party = req.party
    for fld in ("adults", "kids", "elders", "pets"):
        v = data.get(fld)
        if isinstance(v, (int, float)) and getattr(party, fld) == (2 if fld == "adults" else 0):
            setattr(party, fld, max(0, min(int(v), 20)))
    if data.get("accessible"):
        party.accessible = True
    if not req.interests and isinstance(data.get("interests"), list):
        from ..models.enums import Interest

        labels = {i.label: i for i in Interest}
        req.interests = [labels[str(x)] for x in data["interests"] if str(x) in labels]
        if req.interests:
            filled.append("interests")
    if not req.nightlife and data.get("nightlife") is True:
        req.nightlife = True
        filled.append("nightlife")
    if req.pace == "normal" and data.get("pace") in ("relaxed", "normal", "packed"):
        req.pace = str(data["pace"])
        filled.append("pace")
    if not req.must_visit and isinstance(data.get("must_visit"), list):
        req.must_visit = [str(x)[:16] for x in data["must_visit"]][:6]
        if req.must_visit:
            filled.append("must_visit")
    if not req.avoid and isinstance(data.get("avoid"), list):
        req.avoid = [str(x)[:16] for x in data["avoid"]][:6]
    if not req.lodging_area and data.get("lodging_area"):
        req.lodging_area = str(data["lodging_area"])[:20]
    if not req.notes and data.get("notes"):
        req.notes = str(data["notes"])[:200]
    req.party = party
    if filled:
        req.extras = {**(getattr(req, "extras", None) or {}), "llm_filled": filled} \
            if isinstance(TripRequest, object) else {}
    return filled


def _compact_plan(plan: TripPlan, max_days: int) -> dict[str, Any]:
    return {
        "城市": plan.request.destination,
        "诉求": plan.request.describe(),
        "整体天气风险": plan.request and getattr(plan, "blocking_advice", "") or "无",
        "days": [
            {
                "第几天": d.index,
                "日期": str(d.date),
                "天气": d.weather_brief,
                "风险": (d.risk.severity.label if d.risk else "无"),
                "安排": [
                    {
                        "时间": s.time_text,
                        "项目": s.title,
                        "类型": s.type.label,
                        "室内": s.indoor,
                        "花费元": round(s.cost_cents / 100),
                        "预约": s.reservation_hint,
                    }
                    for s in d.slots
                ][:8],
            }
            for d in plan.days
        ][:max_days],
        "预算分项": {k: round(v / 100) for k, v in plan.budget_breakdown.items()},
        "校验通过率": plan.scorecard.scorecard() if plan.scorecard else None,
        "未通过项": [c.name for c in (plan.scorecard.failed if plan.scorecard else [])][:5],
    }
