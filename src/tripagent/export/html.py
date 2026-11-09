"""HTML 报告：单文件、内联样式、可直接浏览器打印成 PDF。

地图刻意不用高德 JS / Leaflet 瓦片：两者一个要 key、一个在境内常不可达，
这里用「按经纬度归一化的 SVG 示意路线」，零依赖、离线可渲染、折返一眼可见。
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..models.common import fmt_cny
from ..models.enums import ConstraintKind
from ..models.plan import DayPlan, TripPlan

_TEMPLATES = Path(__file__).resolve().parent / "templates"

_DIM_LABEL = {
    ConstraintKind.ENV: "环境事实",
    ConstraintKind.COMMONSENSE: "常识约束",
    ConstraintKind.HARD: "用户硬约束",
}


def _day_svg(day: DayPlan, width: int = 340, height: int = 150) -> str:
    pts = [(s, s.poi.point) for s in day.slots if s.poi and s.poi.point]
    if len(pts) < 2:
        return ""
    lngs = [p.lng for _, p in pts]
    lats = [p.lat for _, p in pts]
    min_lng, max_lng, min_lat, max_lat = min(lngs), max(lngs), min(lats), max(lats)
    span_x = max(max_lng - min_lng, 1e-4)
    span_y = max(max_lat - min_lat, 1e-4)
    pad = 26

    def xy(lng: float, lat: float) -> tuple[float, float]:
        x = pad + (lng - min_lng) / span_x * (width - 2 * pad)
        y = height - pad - (lat - min_lat) / span_y * (height - 2 * pad)
        return round(x, 1), round(y, 1)

    coords = [xy(p.lng, p.lat) for _, p in pts]
    line = " ".join(f"{x},{y}" for x, y in coords)
    dots = []
    for i, ((s, _), (x, y)) in enumerate(zip(pts, coords), start=1):
        color = "#e8590c" if s.type.label in ("夜间娱乐", "观看演出") else ("#1971c2" if s.indoor else "#2f9e44")
        dots.append(
            f'<circle cx="{x}" cy="{y}" r="9" fill="{color}" opacity="0.92"/>'
            f'<text x="{x}" y="{y + 3.6}" font-size="10" text-anchor="middle" fill="#fff">{i}</text>'
        )
    body = "".join(dots)
    return (
        f'<svg viewBox="0 0 {width} {height}" class="route" role="img" aria-label="Day{day.index} 示意路线">'
        f'<polyline points="{line}" fill="none" stroke="#adb5bd" stroke-width="1.6" stroke-dasharray="5 4"/>'
        f"{body}</svg>"
    )


def _slot_rows(day: DayPlan) -> list[dict]:
    rows = []
    for s in day.slots:
        poi = s.poi
        rows.append(
            {
                "time": s.time_text,
                "title": s.title,
                "type": s.type.label,
                "indoor": "室内" if s.indoor else "户外",
                "cost": fmt_cny(s.cost_cents) if s.cost_cents else "—",
                "rating": (f"{poi.rating}" if poi and poi.rating else ""),
                "transit": (
                    f"{s.transit_before.minutes} 分钟"
                    + (f"（{s.transit_before.summary}）" if s.transit_before.summary else "")
                    if s.transit_before and s.transit_before.minutes
                    else ""
                ),
                "reservation": s.reservation_hint,
                "why": poi.why if poi else "",
                "pitfalls": poi.pitfalls if poi else [],
                "photos": poi.photo_spots if poi else [],
                "notes": s.notes[:3],
            }
        )
    return rows


def build_context(plan: TripPlan) -> dict:
    req = plan.request
    sc = plan.scorecard
    days = []
    for d in plan.days:
        days.append(
            {
                "index": d.index,
                "date": d.date.strftime("%m/%d") if d.date else "",
                "weekday": "周" + "一二三四五六日"[d.date.weekday()] if d.date else "",
                "weather": d.weather_brief,
                "risk": d.risk.severity.label if d.risk else "",
                "risk_advice": d.risk.advice if d.risk else [],
                "theme": d.theme,
                "cost": fmt_cny(d.total_cost_cents),
                "transit": d.total_transit_minutes,
                "svg": _day_svg(d),
                "slots": _slot_rows(d),
                "notes": d.notes,
            }
        )
    budget = [{"name": k, "value": fmt_cny(v)} for k, v in plan.budget_breakdown.items()]
    budget.append({"name": "合计", "value": fmt_cny(plan.total_cost_cents), "strong": True})
    if req.budget_cents:
        budget.append({"name": "预算", "value": fmt_cny(req.budget_cents)})
        budget.append(
            {
                "name": "结余",
                "value": fmt_cny(req.budget_cents - plan.total_cost_cents),
                "warn": plan.total_cost_cents > req.budget_cents,
            }
        )
    return {
        "title": f"{req.destination} {len(plan.days)} 天专属行程",
        "describe": req.describe(),
        "generated": plan.created_at.strftime("%Y-%m-%d %H:%M"),
        "version": plan.plan_version,
        "sources": plan.sources_used,
        "blocking": plan.blocking_advice,
        "narrative": plan.narrative,
        "days": days,
        "budget": budget,
        "packing": plan.packing,
        "warnings": plan.warnings,
        "intercity": [i.model_dump() for i in plan.intercity],
        "verdict": sc.verdict() if sc else "",
        "pass_rate": f"{sc.pass_rate():.0%}" if sc else "",
        "dimensions": (
            [
                {
                    "name": _DIM_LABEL[k],
                    "rate": f"{sc.pass_rate(k):.0%}",
                    "count": len([c for c in sc.checks if c.kind == k]),
                }
                for k in (ConstraintKind.ENV, ConstraintKind.COMMONSENSE, ConstraintKind.HARD)
            ]
            if sc
            else []
        ),
        "checks": (
            [
                {
                    "icon": c.icon,
                    "name": c.name,
                    "kind": _DIM_LABEL[c.kind],
                    "passed": c.passed,
                    "detail": c.detail,
                    "hint": c.fix_hint,
                }
                for c in sc.checks
            ]
            if sc
            else []
        ),
    }


def to_html(plan: TripPlan) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    ctx = build_context(plan)
    # SVG 是我们自己按坐标生成的可信内容，不参与转义
    ctx["days"] = [{**d, "svg_raw": d.pop("svg") or ""} for d in ctx["days"]]
    return env.get_template("report.html.j2").render(**ctx)
