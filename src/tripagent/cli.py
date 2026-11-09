"""命令行入口（第二个运行入口是 app.py 的 Streamlit 网页）。

    python -m tripagent plan "成都 3天 11月20日出发 带爸妈 爱吃 预算3000 想体验夜生活"
    python -m tripagent ask "成都有哪些评分高、性价比好的清吧？"
    python -m tripagent weather 哈尔滨
    python -m tripagent doctor
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .agents.service import PlanOutcome, TripService
from .core import intent
from .export import export_plan
from .memory import TripStore

console = Console(highlight=False)

DEMO_PROMPTS = [
    "成都 3天 11月20日出发 人均预算1500 爱吃火锅 想体验夜生活 带一个朋友",
    "哈尔滨 4天 12月25日 冰雪和室内演出 带爸妈 预算4000",
    "长沙 2天 下周五出发 大学生穷游 网红打卡 夜市 不要太赶",
]


def _utf8_stdout() -> None:
    """Windows 控制台默认 GBK，中文与天气 emoji 会直接崩，这里强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def _print_request(req) -> None:
    missing = "、".join(req.missing) or "无"
    console.print(
        Panel(
            f"[bold]{req.describe()}[/bold]\n原话：{req.raw_text}\n[dim]未识别：{missing}[/dim]",
            title="解析到的诉求",
            border_style="cyan",
        )
    )


def _print_weather(service, city_name: str, days: int = 16) -> int:
    from .knowledge.cities import resolve_city

    city = resolve_city(city_name)
    if not city:
        console.print(f"[red]认不出城市「{city_name}」[/red]")
        return 2
    weather, trace = service.fetch_weather(city, days)
    for t in trace:
        console.print(f"[dim]{t}[/dim]")
    table = Table(title=f"{city.name} 未来 {len(weather.forecast)} 天天气与风险", header_style="bold")
    for col in ("日期", "星期", "天气", "气温", "降水", "风", "舒适", "风险", "依据"):
        table.add_column(col)
    from .core import weather_risk

    window = [date.fromisoformat(k) for k in weather.risks] if weather.risks else [f.date for f in weather.forecast]
    summary = weather_risk.assess(weather, window)
    for f in weather.forecast:
        r = weather.risk_on(f.date)
        sev = r.severity.label if r else "-"
        color = {"适宜": "green", "关注": "yellow", "警告": "orange3", "危险": "red"}.get(sev, "white")
        factors = "、".join(x.name for x in (r.factors if r else []))[:26] or "-"
        temps = f"{f.tmin_c:.0f}~{f.tmax_c:.0f}℃" if f.tmin_c is not None and f.tmax_c is not None else "-"
        table.add_row(
            f.date.strftime("%m-%d"),
            "周" + "一二三四五六日"[f.date.weekday()],
            f"{f.text}",
            temps,
            f"{f.precip_mm:.1f}mm",
            f"{(f.wind_max_kmh or 0):.0f}km/h",
            str(weather_risk.comfort_index(f)),
            Text(sev, style=color),
            factors,
        )
    console.print(table)
    if weather.alarms:
        console.print(f"[bold]{city.name}当前生效预警 {len(weather.alarms)} 条[/bold]")
        for a in weather.alarms[:6]:
            console.print(f"  · [{a.level or '-'}] {a.alarm_type or ''} {a.title[:44]} — {a.text[:60]}")
    else:
        console.print(f"[green]{city.name}当前无生效气象预警[/green]")
    if summary.get("blocking_advice"):
        console.print(Panel(str(summary["blocking_advice"]), title="天气结论", border_style="red"))
    return 0


def _print_plan(outcome) -> None:
    plan = outcome.plan
    if plan is None:
        for e in outcome.errors:
            console.print(f"[red]{e}[/red]")
        return
    if outcome.errors:
        for e in outcome.errors:
            console.print(f"[yellow]提示：{e}[/yellow]")
    if plan.blocking_advice:
        console.print(Panel(plan.blocking_advice, title="⛔ 天气结论", border_style="red"))
    console.print(f"[bold green]{plan.request.destination} {len(plan.days)} 天行程[/bold green] · {plan.request.describe()}")
    console.print(f"[dim]数据来源：{'、'.join(plan.sources_used)}[/dim]")

    t = Table(title="行程一览", header_style="bold")
    for col in ("天", "日期", "天气", "风险", "主题", "项目", "花费", "通勤"):
        t.add_column(col)
    for d in plan.days:
        t.add_row(
            str(d.index),
            d.date.strftime("%m-%d") if d.date else "-",
            d.weather_brief,
            d.risk.severity.label if d.risk else "-",
            d.theme or "-",
            str(len(d.slots)),
            f"¥{d.total_cost_cents / 100:.0f}",
            f"{d.total_transit_minutes}分",
        )
    console.print(t)

    for d in plan.days:
        s = Table(title=f"Day {d.index} · {d.date:%m/%d}　{d.weather_brief}　{d.theme}", header_style="bold cyan", expand=True)
        for col in ("时间", "项目", "类型", "室内", "花费", "交通", "预约/提示", "为什么是它"):
            s.add_column(col, overflow="fold")
        for slot in d.slots:
            s.add_row(
                slot.time_text,
                slot.title,
                slot.type.label,
                "室内" if slot.indoor else "户外",
                f"¥{slot.cost_cents / 100:.0f}" if slot.cost_cents else "—",
                f"{slot.transit_before.minutes}分" if slot.transit_before and slot.transit_before.minutes else "—",
                slot.reservation_hint or (slot.poi.age_policy if slot.poi and slot.poi.age_policy else "—"),
                (slot.notes[0] if slot.notes else (slot.poi.why if slot.poi else "")) or "—",
            )
        console.print(s)
        for pit in [p for slot in d.slots if slot.poi for p in slot.poi.pitfalls[:1]]:
            console.print(f"  [orange3]避坑[/orange3] {pit}")
        for n in (d.risk.advice if d.risk else [])[:2]:
            console.print(f"  [dim]天气策略：{n}[/dim]")

    if plan.budget_breakdown:
        b = Table(title="预算", header_style="bold")
        b.add_column("分项")
        b.add_column("金额", justify="right")
        for k, v in plan.budget_breakdown.items():
            b.add_row(k, f"¥{v / 100:.0f}")
        b.add_row("[bold]合计[/bold]", f"[bold]¥{plan.total_cost_cents / 100:.0f}[/bold]")
        if plan.request.budget_cents:
            over = plan.total_cost_cents > plan.request.budget_cents
            b.add_row(
                "结余" if not over else "超支",
                f"[{'red' if over else 'green'}]¥{abs(plan.request.budget_cents - plan.total_cost_cents) / 100:.0f}[/]",
            )
        console.print(b)

    if plan.scorecard:
        sc = plan.scorecard
        c = Table(title=f"方案校验：{sc.verdict()}（总通过率 {sc.pass_rate():.0%}）", header_style="bold")
        c.add_column("结果")
        c.add_column("约束")
        c.add_column("维度")
        c.add_column("说明")
        for chk in sc.checks:
            c.add_row(
                "✅" if chk.passed else "[red]⚠️[/red]",
                chk.name,
                chk.kind.label,
                (chk.detail + (f"（建议：{chk.fix_hint}）" if chk.fix_hint else "")) if not chk.passed else "通过",
            )
        console.print(c)

    if plan.narrative:
        console.print(Panel(plan.narrative, title="点评", border_style="magenta"))
    if plan.packing:
        console.print("[bold]行前清单[/bold]：" + "；".join(plan.packing))
    if plan.blocking_advice:
        console.print("[red]以上为「若执意出行」的备选安排，已尽量转为室内项目。[/red]")


def _print_answer(answer) -> None:
    if not answer.ok:
        for w in answer.warnings or ["没有检索到符合条件的地点"]:
            console.print(f"[yellow]{w}[/yellow]")
        return
    t = Table(title=f"{answer.city} · {answer.category} 推荐", header_style="bold", expand=True)
    for col in ("名称", "评分", "人均", "营业", "特色", "网友反馈", "注意"):
        t.add_column(col, overflow="fold")
    for r in answer.items:
        p = r.poi
        t.add_row(
            p.name,
            f"{p.rating}" if p.rating else "—",
            f"¥{(p.avg_cost_cents or p.ticket_cents or 0) / 100:.0f}" if (p.avg_cost_cents or p.ticket_cents) else "—",
            p.open_hours_raw or "—",
            p.why or "、".join(p.tags[:3]) or "—",
            r.reviews[0].brief(60) if r.reviews else "—",
            "；".join([x for x in (p.pitfalls[:1] or [""]) + ([p.age_policy] if p.age_policy else []) if x]) or "—",
        )
    console.print(t)
    console.print(f"[bold]结论：[/bold]{answer.summary}")
    for w in answer.warnings:
        console.print(f"[orange3]· {w}[/orange3]")
    console.print(f"[dim]数据源：{'、'.join(answer.sources_used)}[/dim]")


def _doctor(service) -> int:
    s = service.settings
    console.print(Panel(
        f"离线模式：{'开' if s.offline else '关'}　缓存 {s.cache_ttl_hours:.0f}h　模型 {s.llm_model}\n"
        f"密钥：DashScope {'✓' if s.dashscope_api_key else '✗'}　"
        f"高德 {'✓' if s.amap_key else '✗'}　和风 {'✓' if (s.qweather_key and s.qweather_host) else '✗'}",
        title="运行配置",
        border_style="cyan",
    ))
    t = Table(header_style="bold")
    for col in ("能力", "provider", "状态", "说明"):
        t.add_column(col)
    for cap, name, ok, msg in service.doctor():
        t.add_row(cap, name, "[green]可用[/green]" if ok else "[grey]跳过[/grey]", msg)
    console.print(t)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tripagent", description="LifeHack 旅游规划智能体（天气避让 + 公开数据源 + 可校验行程）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="生成行程")
    sp.add_argument("text", help="自然语言诉求，例如「成都 3天 带爸妈 爱吃 预算3000」")
    sp.add_argument("--out", default="md,html,ics", help="导出格式，逗号分隔（md/html/ics，none 关闭）")
    sp.add_argument("--save", action="store_true", help="写入 data/tripagent.db 历史")
    sp.add_argument("--offline", action="store_true", help="强制只用本地数据源")

    sa = sub.add_parser("ask", help="即时问答：推荐酒吧/KTV/游乐园/演出等")
    sa.add_argument("text", help="例如「成都有哪些评分高、性价比好的清吧」")
    sa.add_argument("--city", default=None)
    sa.add_argument("--top", type=int, default=5)
    sa.add_argument("--offline", action="store_true")

    sw = sub.add_parser("weather", help="查看城市 16 天天气与风险")
    sw.add_argument("city")
    sw.add_argument("--days", type=int, default=16)
    sw.add_argument("--offline", action="store_true")

    sub.add_parser("doctor", help="自检：哪些数据源可用")
    sub.add_parser("cities", help="列出已收录城市")

    sh = sub.add_parser("history", help="历史行程")
    sh.add_argument("--limit", type=int, default=10)

    ss = sub.add_parser("show", help="读取历史行程并重导报告")
    ss.add_argument("trip_id")
    ss.add_argument("--version", type=int, default=None)
    ss.add_argument("--out", default="md,html,ics")

    sd = sub.add_parser("demo", help="连跑 3 个示例诉求（路演彩排）")
    sd.add_argument("--offline", action="store_true")

    sv = sub.add_parser("serve", help="启动 HTTP API（供前端 / Qoder Sites 调用）")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--reload", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    args = build_parser().parse_args(argv)
    if getattr(args, "offline", False):
        import os

        os.environ["TRIPAGENT_OFFLINE"] = "1"
        from .config import reload_settings

        reload_settings()
    service = _service()

    if args.cmd == "plan":
        req = intent.parse(args.text)
        _print_request(req)
        outcome = service.plan_trip(args.text)
        _print_plan(outcome)
        if outcome.plan:
            if args.save:
                tid, ver = TripStore(service.settings).save(outcome.plan, "CLI 生成")
                console.print(f"[green]已存入历史：{tid} v{ver}[/green]")
            if args.out and args.out.lower() != "none":
                paths = export_plan(outcome.plan, formats=tuple(x.strip() for x in args.out.split(",")), settings=service.settings)
                for fmt, path in paths.items():
                    console.print(f"[cyan]{fmt}[/cyan] → {path}")
        return 0 if outcome.plan else 1

    if args.cmd == "ask":
        _print_answer(service.ask(args.text, city=args.city, limit=args.top))
        return 0

    if args.cmd == "weather":
        return _print_weather(service, args.city, args.days)

    if args.cmd == "doctor":
        return _doctor(service)

    if args.cmd == "cities":
        from .knowledge.cities import all_cities

        t = Table(title=f"已收录 {len(all_cities())} 个城市", header_style="bold")
        for col in ("key", "城市", "adcode", "纬度", "经度", "标签"):
            t.add_column(col)
        for c in all_cities():
            t.add_row(c.key, c.name, c.adcode, f"{c.lat:.2f}", f"{c.lng:.2f}", "、".join(c.tags))
        console.print(t)
        console.print("[dim]扩充：编辑 data/fixtures/cities_extra.json（key/name/adcode/lat/lng 必填）[/dim]")
        return 0

    if args.cmd == "history":
        store = TripStore(service.settings)
        t = Table(title="历史行程", header_style="bold")
        for col in ("trip_id", "城市", "天数", "预算", "版本", "创建时间", "原话"):
            t.add_column(col)
        for row in store.list(args.limit):
            t.add_row(row["trip_id"], row["city"], str(row["days"]), f"¥{row['budget_cents'] / 100:.0f}", str(row["latest_version"]), row["created_at"], (row["note"] or "")[:30])
        console.print(t)
        return 0

    if args.cmd == "show":
        plan = TripStore(service.settings).load(args.trip_id, args.version)
        if not plan:
            console.print("[red]找不到该行程[/red]")
            return 1
        _print_plan(PlanOutcome(request=plan.request, plan=plan))
        if args.out and args.out.lower() != "none":
            export_plan(plan, formats=tuple(x.strip() for x in args.out.split(",")), settings=service.settings)
        return 0

    if args.cmd == "serve":
        from .server import main as serve_main

        return serve_main(["--host", args.host, "--port", str(args.port)] + (["--reload"] if args.reload else []))

    if args.cmd == "demo":
        for i, prompt in enumerate(DEMO_PROMPTS, start=1):
            console.print(f"\n[bold]===== 示例 {i}：{prompt}[/bold]")
            outcome = service.plan_trip(prompt)
            _print_plan(outcome)
        return 0
    return 2


def _service():
    return TripService()


if __name__ == "__main__":
    raise SystemExit(main())
