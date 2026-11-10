"""Markdown 报告：终端、UI 预览、以及作品帖正文都直接用它。"""

from __future__ import annotations

from datetime import datetime

from ..models.common import fmt_cny
from ..models.enums import ConstraintKind, SlotType
from ..models.plan import TripPlan

_DIM_LABEL = {
    ConstraintKind.ENV: "环境事实",
    ConstraintKind.COMMONSENSE: "常识约束",
    ConstraintKind.HARD: "用户硬约束",
}


def to_markdown(plan: TripPlan) -> str:
    req = plan.request
    out: list[str] = []
    out.append(f"# {req.destination} {len(plan.days)} 天专属行程")
    out.append("")
    out.append(f"> {req.describe()}")
    out.append(
        f"> 生成 {datetime.now():%Y-%m-%d %H:%M} · 版本 v{plan.plan_version} · 数据源 "
        f"{'、'.join(plan.sources_used) or '本地 fixture'}"
    )
    out.append("")

    if plan.blocking_advice:
        out.append("## ⛔ 天气结论：不建议按原计划出行")
        out.append("")
        out.append("```text")
        out.append(plan.blocking_advice)
        out.append("```")
        out.append("")

    if plan.narrative:
        out.append("## 一句话点评")
        out.append("")
        out.append(plan.narrative)
        out.append("")

    out.append("## 行程一览")
    out.append("")
    out.append("| 天 | 日期 | 天气 | 风险 | 主题 | 花费 | 通勤 |")
    out.append("|---|---|---|---|---|---|---|")
    for d in plan.days:
        risk = d.risk.severity.label if d.risk else "-"
        out.append(
            f"| {d.index} | {d.date:%m/%d} | {d.weather_brief} | {risk} | {d.theme or '-'} "
            f"| {fmt_cny(d.total_cost_cents)} | {d.total_transit_minutes} 分 |"
        )
    out.append("")

    out.append("## 逐日安排")
    for d in plan.days:
        out.append("")
        out.append(f"### Day {d.index} · {d.date:%m/%d}　{d.weather_brief}　{d.theme}")
        if d.risk and d.risk.advice:
            out.append(f"- 天气策略：**{d.risk.severity.label}** · " + "；".join(d.risk.advice))
        for s in d.slots:
            head = f"- **{s.time_text}** {s.title}"
            extras = []
            if s.cost_cents:
                extras.append(fmt_cny(s.cost_cents))
            if s.transit_before and s.transit_before.minutes:
                extras.append(f"交通 {s.transit_before.minutes} 分")
            if s.poi and s.poi.rating:
                extras.append(f"评分 {s.poi.rating}")
            if extras:
                head += f"（{' · '.join(extras)}）"
            out.append(head)
            if s.reservation_hint:
                out.append(f"  - 预约：{s.reservation_hint}")
            if s.poi and s.poi.why:
                out.append(f"  - 为什么是它：{s.poi.why}")
            if s.notes:
                keep = [n for n in s.notes if n != s.reservation_hint][:4]
                if keep:
                    out.append(f"  - 依据：{'；'.join(keep)}")
            if s.poi and s.poi.pitfalls:
                out.append(f"  - 避坑：{'；'.join(s.poi.pitfalls[:3])}")
            if s.poi and s.poi.photo_spots:
                out.append(f"  - 出片机位：{'；'.join(s.poi.photo_spots[:3])}")
            poem = (s.extras or {}).get("poem") or {}
            if poem.get("lines"):
                who = f"{poem.get('author') or ''}《{poem['from']}》" if poem.get("from") else (poem.get("author") or "")
                line = "／".join(str(v) for v in poem["lines"] if str(v).strip())
                out.append(f"  - 这一句：「{line}」—— {who}")
                if poem.get("note"):
                    out.append(f"    - {poem['note']}")
            if s.type is SlotType.NIGHTLIFE and s.poi:
                out.append(f"  - 夜间提示：{'需带身份证' if s.poi.requires_id else ''}"
                           f"{ ' · ' + s.poi.age_policy if s.poi.age_policy else ''}")
        if d.notes:
            for n in d.notes[:2]:
                out.append(f"- 备注：{n}")

    if plan.intercity:
        out.append("")
        out.append("## 跨城交通")
        for leg in plan.intercity:
            out.append(f"- {leg.from_city} → {leg.to_city}　{leg.dep_time}　（{leg.note}）")

    out.append("")
    out.append("## 预算")
    out.append("")
    out.append("| 分项 | 金额 |")
    out.append("|---|---|")
    for k, v in plan.budget_breakdown.items():
        out.append(f"| {k} | {fmt_cny(v)} |")
    out.append(f"| **合计** | **{fmt_cny(plan.total_cost_cents)}** |")
    if req.budget_cents:
        left = req.budget_cents - plan.total_cost_cents
        out.append(f"| 预算 | {fmt_cny(req.budget_cents)} |")
        out.append(f"| 剩余 | {fmt_cny(left)} |")

    if plan.scorecard:
        sc = plan.scorecard
        out.append("")
        out.append(f"## 方案校验记分卡　{sc.verdict()}")
        out.append("")
        out.append(f"总体通过率 **{sc.pass_rate():.0%}**，明细：")
        out.append("")
        out.append("| 维度 | 通过率 | 说明 |")
        out.append("|---|---|---|")
        for kind in (ConstraintKind.ENV, ConstraintKind.COMMONSENSE, ConstraintKind.HARD):
            rows = [c for c in sc.checks if c.kind == kind]
            out.append(f"| {_DIM_LABEL[kind]} | {sc.pass_rate(kind):.0%} | {len(rows)} 项检查 |")
        out.append("")
        out.append("检查明细：")
        for c in sc.checks:
            out.append(f"- {c.one_line()}")

    if plan.packing:
        out.append("")
        out.append("## 行前清单")
        for p in plan.packing:
            out.append(f"- [ ] {p}")

    if plan.warnings:
        out.append("")
        out.append("## 已知局限")
        for w in plan.warnings:
            out.append(f"- {w}")

    out.append("")
    out.append("---")
    out.append("*天气：Open-Meteo 16 天预报 + 中国气象局/中央气象台实时预警；"
               "地点：高德 POI（配置 Key 后自动切换）或本地知识库；口碑：通义千问联网归纳。*")
    return "\n".join(out)
