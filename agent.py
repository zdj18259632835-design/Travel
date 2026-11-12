"""TripAgent Loop —— 自主规划智能体（Coze 式工作流，跑在本地仓库里）。

它和 app.py 的区别是本质性的：
    app.py   = 一条**固定**流水线（意图 → 取数 → 装配 → 校验 → 文案），顺序写死在代码里；
    agent.py = 一个**会自己决定下一步做什么**的主控循环，工具由模型按需调用。

四条硬约束（用户提的，全部可验证）：
    1. 最多 10 步，第 10 步强制收尾；
    2. token 预算上限 1000 万，超了就停；
    3. 超时 60 秒（墙钟），到点就停并交付已有产物；
    4. 必须有**显式任务完成信号**——不是"循环自然结束"，而是模型调用 `finish` 工具，
       或者被强制收尾时由系统补发一个 `forced_finish`。

为什么要第 4 条：一个只会"跑完 N 步"的智能体，无法区分"它做完了"和"它不知道干嘛了"。
完成信号让调用方（CLI / 界面 / 以后的接口）能据此决定要不要重试。

跑法：
    .venv\\Scripts\\python.exe agent.py "成都 3天 11月20日出发 带一个小孩 爱吃火锅 预算2000"
    .venv\\Scripts\\python.exe agent.py "三亚 2天 情侣" --verbose
    .venv\\Scripts\\python.exe -m streamlit run agent.py        # 带步骤轨迹的网页版
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
_SRC = ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# 放在模块层而不是函数里：token 计量是这四条约束之一的依据，测试要能把它换成桩
from tripagent.providers.usage import UsageLedger  # noqa: E402

MAX_STEPS = 10
TOKEN_BUDGET = 10_000_000
TIMEOUT_S = 60.0
OBS_CAP = 1400          # 单条观察结果喂回模型的字符上限：不封顶的话 prompt 会指数膨胀
# 实测这两个工具各要 25-28 秒（联网检索 + 完整流水线），
# 剩余时间少于下面的阈值时直接拒绝执行，否则 60 秒超时形同虚设。
HEAVY = {"build_itinerary", "ask_entertainment"}
HEAVY_MIN_LEFT = 32.0
# 单个工具的等待上限。每步开始前看表只能挡住"步与步之间"，
# 实测一次 build_itinerary 因图片冷下载跑了 40+ 秒，60 秒的闸门被单步冲穿到 108.7s。
HEAVY_TOOL_CAP_S = 45.0
TOOL_CAP_S = 20.0

# ---------------------------------------------------------------------------
# 工具层：每个工具都包住仓库里已经存在、已经被 118 项测试盯着的真实能力。
# 智能体的价值不在于它比流水线聪明，而在于它**出错时可以换个工具再来**，
# 而工具本身必须是可信的——所以这里一个业务逻辑都不重写。
# ---------------------------------------------------------------------------


@dataclass
class Ctx:
    """一次运行的共享状态。工具之间靠它传数据，而不是靠模型复述。"""

    svc: Any = None
    last_plan: Any = None            # TripPlan
    last_outcome: Any = None         # PlanOutcome
    weather_seen: bool = False
    places_seen: int = 0
    calls: dict[str, int] = field(default_factory=dict)

    def count(self, name: str) -> int:
        self.calls[name] = self.calls.get(name, 0) + 1
        return self.calls[name]


def _tool_resolve_city(ctx: Ctx, args: dict) -> dict:
    from tripagent.providers.geo import resolve_any

    city = resolve_any(str(args.get("city") or ""), ctx.svc.chain)
    if not city:
        return {"error": f"识别不出城市「{args.get('city')}」，换用标准市名（如「成都」「杭州」）"}
    return {"name": city.name, "key": city.key, "adcode": city.adcode,
            "province": city.province, "tags": list(city.tags)}


def _tool_check_weather(ctx: Ctx, args: dict) -> dict:
    from datetime import date, timedelta

    from tripagent.core.weather_risk import window_verdicts
    from tripagent.providers.geo import resolve_any

    city = resolve_any(str(args.get("city") or ""), ctx.svc.chain)
    if not city:
        return {"error": "城市识别失败，先调用 resolve_city"}
    days = max(1, min(int(args.get("days") or 3), 14))
    raw = str(args.get("start_date") or "")
    try:
        start = date.fromisoformat(raw) if raw else date.today() + timedelta(days=2)
    except ValueError:
        return {"error": f"start_date 格式不对：{raw}，要 YYYY-MM-DD"}
    horizon = min(16, max(days + 2, (start - date.today()).days + days + 2))
    weather, _ = ctx.svc.fetch_weather(city, horizon=horizon)
    window = [start + timedelta(days=i) for i in range(days)]
    groups = window_verdicts(weather, window)
    ctx.weather_seen = True
    blocked = [v for v in groups["avoid"]]
    return {
        "预报源": weather.notes.get("forecast"),
        "覆盖": f"{len(weather.forecast)} 天，行程 {days} 天（{start:%m-%d} 起）",
        "能全户外": [v["date"] for v in groups["outdoor"]],
        "建议室内": [v["date"] for v in groups["indoor"]],
        "别出门": [v["date"] for v in blocked],
        "逐日建议": [{"日期": v["date"], "结论": v["verdict"], "说明": v["advice"][:90]}
                    for v in (groups["outdoor"] + groups["indoor"] + blocked)[:6]],
        "预警": [a.title[:40] for a in weather.alarms[:4]],
        "阻塞建议": (blocked[0]["advice"] if blocked else ""),
    }


def _tool_find_places(ctx: Ctx, args: dict) -> dict:
    from tripagent.knowledge.poi_codes import categories_for
    from tripagent.models.enums import Category, Interest
    from tripagent.models.poi import POIQuery
    from tripagent.providers.geo import resolve_any

    city = resolve_any(str(args.get("city") or ""), ctx.svc.chain)
    if not city:
        return {"error": "城市识别失败，先调用 resolve_city"}
    label = str(args.get("category") or "风景名胜")
    if label not in [c.value for c in Category]:
        return {"error": f"类目名要用枚举原文，可选：{'、'.join(c.value for c in Category)}"}
    cat = Category(label)
    limit = max(1, min(int(args.get("limit") or 6), 10))
    # 字段名是单数 category：写成 categories 会被 POIQuery 的 extra="allow" 静静收下，
    # 结果是 query.category=None 走默认值，无论模型要什么都只返回风景名胜。
    # 这个坑是实测三亚时抓到的（智能体连续两次 find_places 拿到同样的结果后自己换了工具）。
    res = ctx.svc.chain.run("poi", "search", city,
                            POIQuery(city=city.name, category=cat,
                                     keywords=[str(args.get("keyword") or "")], limit=limit))
    rows = res.value or []
    ctx.places_seen += len(rows)
    return {
        "来源": res.source,
        "命中": len(rows),
        "清单": [{"名称": p.name, "类目": p.category.value, "室内": bool(p.indoor),
                 "评分": p.rating, "人均元": round((p.avg_cost_cents or p.ticket_cents or 0) / 100),
                 "营业": (p.open_hours_raw or "")[:24], "有图": bool(p.photos)}
                for p in rows[:limit]],
    }


def _tool_ask_entertainment(ctx: Ctx, args: dict) -> dict:
    from tripagent.agents.advisor import EntertainmentAdvisor

    q = str(args.get("question") or "")
    if not q:
        return {"error": "question 不能为空"}
    ans = EntertainmentAdvisor(ctx.svc.settings, ctx.svc.chain).ask(q, city_hint=str(args.get("city") or ""))
    return {
        "城市": ans.city, "类目": ans.category,
        "清单": [{"名称": r.poi.name, "评分": r.poi.rating, "人均元": round((r.poi.avg_cost_cents or 0) / 100),
                 "理由": "；".join(r.reasons[:2])[:80]} for r in ans.items[:6]],
        "合规提醒": ans.warnings[:3],
        "说明": ans.summary[:200],
    }


def _tool_build_itinerary(ctx: Ctx, args: dict) -> dict:
    text = str(args.get("request_text") or "")
    if not text:
        return {"error": "request_text 不能为空，要是一句中文诉求"}
    out = ctx.svc.plan_trip(text)
    if not out.plan:
        return {"error": "没能生成行程", "原因": out.errors[:3]}
    ctx.last_plan, ctx.last_outcome = out.plan, out
    sc = out.plan.scorecard
    return {
        "天数": len(out.plan.days),
        "安排数": len(out.plan.all_slots),
        "总花费元": round(out.plan.total_cost_cents / 100),
        "预算元": round(out.plan.request.budget_cents / 100),
        "记分卡": f"{sum(1 for c in sc.checks if c.passed)}/{len(sc.checks)}",
        "结论": sc.verdict(),
        "未通过": [f"{c.name}：{c.detail[:60]}（建议：{c.fix_hint[:40]}）" for c in sc.failed],
        "阻塞建议": out.plan.blocking_advice[:120],
        "每天": [f"{d.day_label} {d.weather_brief} → " + "、".join(s.title[:12] for s in d.slots[:4])
                for d in out.plan.days],
        "耗时": out.timings.get("总计"),
    }


def _tool_review_plan(ctx: Ctx, args: dict) -> dict:
    if not ctx.last_plan:
        return {"error": "还没调用过 build_itinerary，没有可复核的行程"}
    p = ctx.last_plan
    sc = p.scorecard
    return {
        "记分卡": f"{sum(1 for c in sc.checks if c.passed)}/{len(sc.checks)}",
        "逐条": [f"{'通过' if c.passed else '未通过'} {c.name}｜{c.detail[:70]}" for c in sc.checks],
        "行前清单": p.packing[:8],
        "实拍图": sum(1 for s in p.all_slots if s.poi and s.poi.photos),
        "诗句": sum(1 for s in p.all_slots if (s.extras or {}).get("poem")),
        "数据源": p.sources_used[:8],
    }


def _schema() -> str:
    return """可用工具（action 只能取其中之一）：
- resolve_city      {"city":"成都"}
- check_weather     {"city":"成都","start_date":"2026-11-20","days":3}
- find_places       {"city":"成都","category":"风景名胜","keyword":"","limit":6}
                    category 必须是：风景名胜/博物馆展览馆/公园广场/人文古迹/特色餐饮/咖啡茶饮/
                    酒吧酒馆/KTV歌厅/夜店夜场/剧场演出/影院/主题乐园/运动休闲/密室与沉浸娱乐/
                    温泉洗浴/购物街区/住宿
- ask_entertainment {"question":"成都有哪些评分高的清吧","city":"成都"}
- build_itinerary   {"request_text":"成都 3天 11月20日出发 带一个小孩 爱吃火锅 预算2000"}
- review_plan       {}
- finish            {"summary":"给用户的最终交付说明","plan_ready":true}"""


TOOLS: dict[str, Callable[[Ctx, dict], dict]] = {
    "resolve_city": _tool_resolve_city,
    "check_weather": _tool_check_weather,
    "find_places": _tool_find_places,
    "ask_entertainment": _tool_ask_entertainment,
    "build_itinerary": _tool_build_itinerary,
    "review_plan": _tool_review_plan,
}

SYSTEM = """你是 TripAgent 的规划主控，负责在无人值守条件下自主完成一次旅行方案规划。
每一步**只输出一个 JSON 对象**，不要解释文字，不要代码围栏：
{"thought":"一句话说明你为什么要做这步","action":"工具名","args":{...}}

{schema}

必须遵守：
1. 最多 {steps} 步。到第 {steps} 步时，无论完成与否都必须调用 finish。
2. 先看天气再排行程：check_weather 的"别出门/建议室内"决定室内外配比与是否改期。
3. 只有当 build_itinerary 已成功、且你看过 review_plan 的记分卡之后，才允许
   finish(plan_ready=true)。没产出可交付方案时 plan_ready 必须是 false，并在 summary 里说清卡在哪。
4. 工具返回里若有 error，要换参数或换工具，**不要重复同一个失败调用**。
5. 信息不足时不要反问用户（这是无人值守模式）：用合理默认值，并在 thought 里写明你补了什么假设。
6. 你的目标不是多调用工具，而是**尽快交付一份能通过校验的方案**。
7. 重工具要省着用：build_itinerary 约 25 秒、ask_entertainment 约 28 秒，一次就够，
   不要重复调用同一个；**剩余时间少于 35 秒时不要再调用 build_itinerary**，
   改为基于已有观察直接 finish。
8. 每步观察后面会附上剩余时间/预算/步数，据此决定要不要再要一个工具。"""


@dataclass
class Step:
    index: int
    thought: str
    action: str
    args: dict
    observation: dict
    tokens: int
    seconds: float
    error: str = ""


@dataclass
class AgentRun:
    goal: str
    status: str = ""                  # finished / step_limit / timeout / token_budget / no_llm / error
    signal: str = ""                  # task_complete / forced_finish / aborted
    steps_used: int = 0
    tokens_used: int = 0
    seconds: float = 0.0
    summary: str = ""
    plan_ready: bool = False
    mode: str = "llm"
    trace: list[Step] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"status": self.status, "signal": self.signal, "completed": self.signal == "task_complete",
                "steps_used": self.steps_used, "steps_limit": MAX_STEPS,
                "tokens_used": self.tokens_used, "token_budget": TOKEN_BUDGET,
                "seconds": round(self.seconds, 1), "timeout_s": TIMEOUT_S,
                "plan_ready": self.plan_ready, "mode": self.mode,
                "summary": self.summary}


# ---------------------------------------------------------------------------
# 模型输出解析：先当 JSON 读，读不懂就抓最像 action 的片段。
# 这里刻意不用 strict json_mode：qwen 在 JSON 里写未转义引号打断结构的事故
# 在 providers/culture.py、evidence.py 都出现过，宽容解析比"要求模型听话"可靠。
# ---------------------------------------------------------------------------


def parse_action(raw: str) -> tuple[dict | None, str]:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    cand: list[str] = [text]
    m = re.search(r"\{.*\}", text, flags=re.S)
    if m:
        cand.insert(0, m.group(0))
    for s in cand:
        try:
            obj = json.loads(s)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("action"):
            return obj, ""
    # 兜底：正则抠 action/args，模型常在 JSON 前后多写一句解释
    a = re.search(r'"action"\s*:\s*"?([a-z_]+)"?', text)
    if a and a.group(1) in (set(TOOLS) | {"finish"}):
        return {"thought": "", "action": a.group(1), "args": {}}, "args 解析失败，按空调用"
    return None, f"输出不是可执行动作：{text[:80]}"


def _truncate(obs: dict) -> str:
    s = json.dumps(obs, ensure_ascii=False)
    if len(s) > OBS_CAP:
        s = s[:OBS_CAP] + f"…（已截断，共 {len(s)} 字）"
    return s


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


def _call_with_deadline(fn: Callable[[Ctx, dict], dict], ctx: Ctx, args: dict, limit: float) -> dict:
    """带期限地执行一个工具。超时返回错误观察并让循环停下。

    为什么必须这样：只在"每步开始前"看表，等于默认"工具都会很快"。
    实测杭州那次 build_itinerary 单步跑了 40+ 秒（图片冷下载），
    结果 60 秒的约束被一个工具轻松冲破到 108.7s——那不是超时，那是装饰。
    这里改成"愿意等多久就等多久，到点就不等了"：
    线程用 daemon，主进程退出时不会被卡住；已经发出去的网络请求无法召回，
    但结果不再影响交付。
    """
    import threading

    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["ok"] = fn(ctx, args)
        except Exception as exc:
            box["err"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(max(1.0, limit))
    if t.is_alive():
        return {"error": f"工具执行超过 {limit:.0f} 秒，已中止等待。请基于已有观察立即 finish。"}
    if "err" in box:
        return {"error": box["err"]}
    return box.get("ok") or {"error": "工具没有返回内容"}


def run_agent(goal: str, verbose: bool = False, svc: Any = None) -> AgentRun:
    from tripagent.agents.service import TripService
    from tripagent.config import reload_settings

    settings = svc.settings if svc is not None else reload_settings()
    svc = svc or TripService(settings)
    ctx = Ctx(svc=svc)
    run = AgentRun(goal=goal)
    t0 = time.time()

    def tokens_now() -> int:
        # 每次都新建台账：UsageLedger 在构造时读文件，之后只缓存。
        # 用同一个实例做差值会永远得到 0——provider 写的是它自己那个实例。
        return UsageLedger(settings.usage_path).tokens_today()

    llm = None
    if settings.dashscope_api_key and settings.online:
        llm = next((i for n, i in svc.chain.providers_for("llm") if n == "dashscope"), None)
    if llm is None:
        # 没有 key 也要能演示：走确定性策略（这条策略本身就是流水线的顺序，
        # 差别是它照样产出 trace、照样给完成信号，调用方看不出"降级"的断层）
        run.mode = "rule"
        return _rule_policy(ctx, goal, run, t0, verbose)

    # 用 replace 而不是 str.format：模板里全是 JSON 的花括号，format 会把它们当占位符
    system = SYSTEM.replace("{schema}", _schema()).replace("{steps}", str(MAX_STEPS))
    transcript: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"任务：{goal}\n请开始第一步。"},
    ]

    while run.steps_used < MAX_STEPS:
        left = TIMEOUT_S - (time.time() - t0)
        if left <= 0:
            run.status = "timeout"
            break
        if run.tokens_used >= TOKEN_BUDGET:
            run.status = "token_budget"
            break
        if left < 15 or TOKEN_BUDGET - run.tokens_used < 40_000:
            transcript.append({"role": "user", "content":
                               "剩余时间或 token 已不足，立即调用 finish（plan_ready 按实际情况填）。"})
            transcript = transcript[:1] + transcript[-4:]      # 收尾不必带全史，控住 prompt 体积

        before = tokens_now()
        step_t = time.time()
        try:
            raw = llm.chat(transcript, temperature=0.2, max_tokens=600)
        except Exception as exc:
            run.status = "error"
            run.summary = f"模型调用失败：{str(exc)[:160]}"
            break
        used = max(0, tokens_now() - before)
        run.tokens_used += used

        action, perr = parse_action(raw)
        run.steps_used += 1
        if action is None:
            st = Step(run.steps_used, "", "invalid", {}, {"error": perr}, used, time.time() - step_t, perr)
            run.trace.append(st)
            transcript += [{"role": "assistant", "content": raw[:600]},
                           {"role": "user", "content": f"上一步无法执行：{perr}。请只输出一个 JSON 动作对象。"}]
            if verbose:
                print(f"  [{run.steps_used}] 无效输出 → 要求重发")
            continue

        name = str(action.get("action") or "")
        args = action.get("args") or {}
        thought = str(action.get("thought") or "")[:120]
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        transcript.append({"role": "assistant", "content": json.dumps(
            {"thought": thought, "action": name, "args": args}, ensure_ascii=False)})

        if name == "finish":
            run.status = "finished"
            run.signal = "task_complete"
            run.plan_ready = bool(args.get("plan_ready")) or ctx.last_plan is not None
            run.summary = str(args.get("summary") or "")[:1200]
            run.trace.append(Step(run.steps_used, thought, "finish", args, {"accepted": True},
                                  used, time.time() - step_t))
            if verbose:
                print(f"  [{run.steps_used}] finish ← {thought}")
            break

        if name not in TOOLS:
            obs = {"error": f"没有工具「{name}」。可用：{'、'.join(set(TOOLS) | {'finish'})}"}
        elif name in HEAVY and (TIMEOUT_S - (time.time() - t0)) < HEAVY_MIN_LEFT:
            # 到点前 32 秒还允许调 25 秒的工具，超时就是句空话。
            # 这里直接拒绝执行，而不是"建议模型别调"——约束要落在系统上才叫约束。
            obs = {"error": f"{name} 是重工具（约 25 秒），剩余时间不足 {HEAVY_MIN_LEFT:.0f} 秒，已拒绝执行。"
                            "请基于已有观察立即调用 finish。"}
        else:
            # 给工具的时间 = 剩余时间，但不超过单工具上限（重工具 45s，轻工具 20s）
            left_for_tool = TIMEOUT_S - (time.time() - t0)
            cap = HEAVY_TOOL_CAP_S if name in HEAVY else TOOL_CAP_S
            obs = _call_with_deadline(TOOLS[name], ctx,
                                      args if isinstance(args, dict) else {},
                                      min(max(left_for_tool, 1.0), cap))
            if "已中止等待" in str(obs.get("error") or ""):
                run.status = "timeout"
        st = Step(run.steps_used, thought, name, args if isinstance(args, dict) else {}, obs,
                  used, time.time() - step_t, str(obs.get("error") or ""))
        run.trace.append(st)
        # 把剩余预算摊在模型眼前。不给它看，它就会在只剩 20 秒时决定调用一个 25 秒的工具
        left_after = TIMEOUT_S - (time.time() - t0)
        budget_note = (f"｜剩余 {max(0, left_after):.0f}s、token 剩 {TOKEN_BUDGET - run.tokens_used:,}、"
                       f"步数 {run.steps_used}/{MAX_STEPS}")
        transcript.append({"role": "user", "content": f"观察｜{name} → {_truncate(obs)}{budget_note}"})
        if verbose:
            print(f"  [{run.steps_used}] {name} ← {thought}  ({used} tok, {st.seconds:.1f}s)")
            print(f"       {json.dumps(obs, ensure_ascii=False)[:200]}")
        if run.status == "timeout":
            # 工具已经吃掉整个 60 秒，再问一次模型只会拿到第二次超时。
            # 观察已经写进 transcript，下面的强制收尾会用它把方案交付出去。
            break

    if run.signal != "task_complete":
        # 强制收尾也必须给信号：调用方永远能区分"做完了"和"被中止"
        run.signal = "forced_finish"
        run.status = run.status or "step_limit"
        if ctx.last_plan is not None:
            sc = ctx.last_plan.scorecard
            run.plan_ready = True
            run.summary = (f"{run.status}（已交付方案：{len(ctx.last_plan.days)} 天 "
                           f"{len(ctx.last_plan.all_slots)} 项，记分卡 "
                           f"{sum(1 for c in sc.checks if c.passed)}/{len(sc.checks)}，结论 {sc.verdict()}）")
        else:
            run.summary = run.summary or f"{run.status}：未能产出可交付方案"
    run.seconds = time.time() - t0
    return run


def _rule_policy(ctx: Ctx, goal: str, run: AgentRun, t0: float, verbose: bool) -> AgentRun:
    """无 key / 离线时的确定性策略：天气 → 建方案 → 复核 → 完成。

    刻意保留与 LLM 模式相同的 trace 与完成信号，这样"降级"是可见的、可对比的，
    而不是悄悄换了另一套行为。
    """
    steps = [
        ("check_weather", {"city": re.split(r"\s", goal)[0] if goal else "", "days": 3}),
        ("build_itinerary", {"request_text": goal}),
        ("review_plan", {}),
    ]
    for name, args in steps:
        if run.steps_used + 1 > MAX_STEPS or time.time() - t0 > TIMEOUT_S:
            break
        st = time.time()
        try:
            obs = TOOLS[name](ctx, args)
        except Exception as exc:
            obs = {"error": f"{type(exc).__name__}: {str(exc)[:180]}"}
        run.steps_used += 1
        run.trace.append(Step(run.steps_used, "确定性策略", name, args, obs, 0, time.time() - st,
                              str(obs.get("error") or "")))
        if verbose:
            print(f"  [{run.steps_used}] {name}（规则模式）")
    run.status = "finished" if ctx.last_plan else "no_llm"
    run.signal = "task_complete" if ctx.last_plan else "forced_finish"
    run.plan_ready = ctx.last_plan is not None
    if ctx.last_plan:
        sc = ctx.last_plan.scorecard
        run.summary = (f"已生成 {len(ctx.last_plan.days)} 天 {len(ctx.last_plan.all_slots)} 项方案，"
                       f"记分卡 {sum(1 for c in sc.checks if c.passed)}/{len(sc.checks)}，结论 {sc.verdict()}。"
                       f"（规则模式：未配置模型 key，主控循环未启用，交付物与 LLM 模式同源）")
    else:
        run.summary = "规则模式也没能产出方案，请看 trace 里的 error"
    run.seconds = time.time() - t0
    return run


# ---------------------------------------------------------------------------
# 入口：CLI 与 Streamlit 两种壳，共用同一个 run_agent
# ---------------------------------------------------------------------------


def _cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="TripAgent Loop：自主规划智能体")
    ap.add_argument("goal", nargs="?", default="成都 3天 11月20日出发 带一个小孩 爱吃火锅 预算2000")
    ap.add_argument("--verbose", action="store_true", help="打印每一步思考与观察")
    ap.add_argument("--json", action="store_true", help="只输出结果 JSON")
    a = ap.parse_args(argv)
    # 中文 Windows 控制台是 GBK：⚠ 这类字符不在表里，打印会直接抛 UnicodeEncodeError，
    # 于是"方案已经排好了"的进程因为一个警告符号退出码变 1。宁可显示成 ?，也不要崩。
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    t0 = time.time()
    print(f"目标：{a.goal}\n约束：≤{MAX_STEPS} 步 ｜ ≤{TOKEN_BUDGET:,} token ｜ ≤{TIMEOUT_S:.0f}s\n")
    run = run_agent(a.goal, verbose=a.verbose)
    if a.json:
        print(json.dumps(run.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"\n状态 {run.status} ｜ 完成信号 {run.signal}（completed={run.signal == 'task_complete'}）")
        print(f"用了 {run.steps_used}/{MAX_STEPS} 步、{run.tokens_used:,}/{TOKEN_BUDGET:,} token、"
              f"{run.seconds:.1f}/{TIMEOUT_S:.0f}s ｜ 模式 {run.mode}")
        print(f"方案就绪：{run.plan_ready}")
        print(f"\n{run.summary}")
        if run.plan_ready and run.trace:
            for st in run.trace:
                if st.action == "build_itinerary" and not st.error:
                    print("\n方案要点：")
                    for line in (st.observation.get("每天") or []):
                        print("  ·", line)
                    for bad in (st.observation.get("未通过") or []):
                        print("  !", bad)
    print(f"\n[墙钟 {time.time() - t0:.1f}s]")
    return 0 if run.signal == "task_complete" else 1


def _streamlit() -> None:  # pragma: no cover - 需要真实 Streamlit 运行时
    import streamlit as st

    st.set_page_config(page_title="TripAgent Loop", layout="wide")
    st.title("TripAgent Loop · 自主规划智能体")
    st.caption(f"最多 {MAX_STEPS} 步 ｜ token 预算 {TOKEN_BUDGET:,} ｜ 超时 {TIMEOUT_S:.0f} 秒 ｜ 需显式完成信号")
    goal = st.text_input("任务", "成都 3天 11月20日出发 带一个小孩 爱吃火锅 想体验夜生活 预算2000")
    if not st.button("运行智能体", type="primary"):
        st.info("点上面的按钮跑一次。每一步的思考、工具、观察结果都会列出来，卡住时能看出是谁的问题。")
        return
    with st.spinner("智能体正在自主规划（最多 10 步 / 60 秒）……"):
        run = run_agent(goal)
    done = run.signal == "task_complete"
    (st.success if done else st.warning)(
        f"完成信号 {run.signal} ｜ 状态 {run.status} ｜ {run.steps_used}/{MAX_STEPS} 步 ｜ "
        f"{run.tokens_used:,}/{TOKEN_BUDGET:,} token ｜ {run.seconds:.1f}/{TIMEOUT_S:.0f}s ｜ 模式 {run.mode}"
    )
    st.subheader("交付说明")
    st.write(run.summary or "（无）")
    st.subheader("步骤轨迹")
    for s in run.trace:
        with st.expander(f"第 {s.index} 步 · {s.action} · {s.tokens:,} tok · {s.seconds:.1f}s"
                         f"{'｜失败' if s.error else ''}"):
            if s.thought:
                st.caption(s.thought)
            st.json(s.observation, expanded=False)
    for s in run.trace:
        if s.action == "build_itinerary" and not s.error:
            st.subheader("方案摘要（来自 build_itinerary 的观察）")
            st.json(s.observation, expanded=True)
            break


if __name__ == "__main__":
    # 只在"确实是被 streamlit run 拉起"时才碰 streamlit：CLI 模式下提前 import 它，
    # 会打出一句 missing ScriptRunContext 的警告，用户会以为程序有问题。
    _under_streamlit = False
    if "streamlit.runtime.scriptrunner" in sys.modules:
        try:
            from streamlit.runtime.scriptrunner import get_script_run_ctx

            _under_streamlit = get_script_run_ctx() is not None
        except ImportError:
            _under_streamlit = False
    if _under_streamlit:
        _streamlit()
    else:
        sys.exit(_cli(sys.argv[1:]))
