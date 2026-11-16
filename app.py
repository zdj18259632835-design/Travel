"""Streamlit 网页入口（第二个入口是命令行 `python -m tripagent.cli`）。

启动（在项目根目录）：
    .venv\\Scripts\\python.exe -m streamlit run app.py
浏览器会自动打开 http://localhost:8501
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

# 平台型运行环境（魔搭创空间的 Streamlit 框架）只会 pip install requirements.txt，
# 不会执行 `pip install -e .`。不把 src 加进 sys.path，第一行 import 就是 ModuleNotFoundError。
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import ui_render
import ui_theme
from tripagent.agents.service import PlanOutcome, TripService
from tripagent.config import reload_settings
from tripagent.core import weather_risk
from tripagent.export import export_plan
from tripagent.knowledge.cities import all_cities
from tripagent.memory import TripStore
from tripagent.models.common import precip_level, temp_span
from tripagent.models.enums import Interest
from tripagent.models.request import Party

st.set_page_config(page_title="TripAgent · 山海行旅", page_icon="🧭", layout="wide")
ui_theme.inject()

CITY_INDEX = sorted(all_cities(), key=lambda c: (c.level != "city", c.name))
CITY_NAMES = [c.name for c in CITY_INDEX[:400]]
INTEREST_OPTIONS = {i.label: i for i in Interest}
PLAN_COOLDOWN_S = 20  # 同一个浏览器会话两次规划的最小间隔（秒）


def _service(offline: bool) -> TripService:
    os.environ["TRIPAGENT_OFFLINE"] = "1" if offline else "0"
    return TripService(reload_settings())


def _offline_flag() -> bool:
    """离线开关的状态存在 session_state，控件本体画在「数据源自检」页。

    主界面因此不再挂一条侧边栏；切换开关会让 Streamlit 整页重跑，
    下一次执行就能读到新值。
    """
    return bool(st.session_state.get("offline", False))


def _inputs(free_text: str) -> dict:
    col1, col2, col3, col4 = st.columns([2, 1, 1.4, 1.2])
    with col1:
        # 自由输入而不是下拉：全国 3600 多个县市区 + 乡镇（乡镇走地理编码）
        raw_city = st.text_input("城市 / 县 / 乡镇", value="成都", placeholder="如：阿勒泰、泰宁县、凤凰县沱江镇")
        hits = [c for c in CITY_INDEX if raw_city and raw_city.strip().lower() in c.name.lower()][:8]
        if hits:
            picked = st.pills("猜你要找", [h.name for h in hits], selection_mode="single", key="cityPick")
            if picked:
                raw_city = picked
        city = raw_city.strip()
    with col2:
        days = st.slider("天数", 1, 30, 3, help="最长一个月；超过 16 天的部分拿不到预报，校验会如实提示")
    with col3:
        start = st.date_input("出发日期", value=date.today() + timedelta(days=7), min_value=date.today())
    with col4:
        pace = st.select_slider("节奏", options=["relaxed", "normal", "packed"], value="normal",
                                format_func=lambda v: {"relaxed": "休闲", "normal": "常规", "packed": "特种兵"}[v])

    colb1, colb2, colb3, colb4 = st.columns([1.4, 1, 1, 1])
    with colb1:
        budget = st.number_input("总预算（元，0=不限）", min_value=0, value=0, step=100)
    with colb2:
        adults = st.number_input("成人", 1, 10, 2)
    with colb3:
        kids = st.number_input("儿童", 0, 6, 0)
    with colb4:
        elders = st.number_input("长辈", 0, 6, 0)

    interests = st.multiselect("兴趣偏好", list(INTEREST_OPTIONS), default=[], placeholder="可多选：美食探店 / 夜生活 / 亲子遛娃…")
    nightlife = st.checkbox("安排夜生活（酒吧 / KTV / 演出）", value=False)
    from tripagent.core.audience import Audience, detect_audience

    guess, why = detect_audience(" ".join([raw_city, free_text]), Party(adults=int(adults)))
    aud_names = [a.value for a in Audience]
    audience = st.selectbox(
        "这次和谁一起（决定推荐什么项目、以及为什么推荐）",
        aud_names,
        index=aud_names.index(guess.value) if guess else 0,
        key="audience",
        help=f"已从你的输入推断：{why}" if why else "没从描述里推断出来，默认按综合排；选了你自己的场景，理由会重写",
    )

    return dict(city=city, days=days, start=start, pace=pace, budget=budget, audience=audience, adults=int(adults),
                kids=int(kids), elders=int(elders), interests=interests, nightlife=nightlife)


def _build_text(p: dict, free_text: str) -> str:
    if free_text.strip():
        return free_text.strip()
    bits = [p["city"], f"{p['days']}天", f"{p['start']:%m月%d日}出发"]
    if p["budget"]:
        bits.append(f"预算{p['budget']}元")
    if p["adults"] != 2:
        bits.append(f"{p['adults']}人")
    if p["kids"]:
        bits.append(f"带{p['kids']}个小孩")
    if p["elders"]:
        bits.append(f"带{p['elders']}位长辈")
    if p["interests"]:
        bits.append("喜欢" + "、".join(p["interests"]))
    if p.get("audience") and p["audience"] != "朋友团玩":
        bits.append(p["audience"])
    if p["nightlife"]:
        bits.append("想体验夜生活")
    if p["pace"] == "relaxed":
        bits.append("节奏休闲")
    if p["pace"] == "packed":
        bits.append("特种兵式")
    return " ".join(bits)


def _render_weather(outcome: PlanOutcome) -> None:
    """天气不再列表格：直接给"哪几天适合户外 / 哪几天改室内 / 哪几天别去"。"""
    w = outcome.weather
    if not w or not w.forecast:
        st.info("没有拿到预报数据（可能离线或超出预报窗口）。")
        return
    req = outcome.request
    horizon = max(1, req.days) + 2
    window = [req.start_date + timedelta(days=i) for i in range(req.days)] if req.start_date else []
    ui_theme.section("壹", "天气与出行建议", f"{req.start_date:%m月%d日}起 {req.days} 天 · 先看天，再论行程")
    ui_render.weather_panel(w, window or [f.date for f in w.forecast[: req.days]], w.alarms)
    shown = min(len(window or w.forecast), horizon)
    with st.expander(f"看这 {shown} 天的原始逐日数据（需要核对的人用得上）"):
        rows = [
            {
                "日期": f.date.strftime("%m-%d"),
                "天气": f.text,
                "降水程度": precip_level(f.precip_mm, f.precip_prob, f.text),
                "气温": temp_span(f.tmin_c, f.tmax_c),
                "风险": (w.risk_on(f.date).severity.label if w.risk_on(f.date) else "-"),
                "风险因素": "、".join(x.name for x in (w.risk_on(f.date).factors if w.risk_on(f.date) else [])) or "-",
            }
            for f in w.forecast[:horizon]
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _render_plan(outcome: PlanOutcome) -> None:
    plan = outcome.plan
    if plan is None:
        for e in outcome.errors:
            st.error(e)
        return
    for e in outcome.errors:
        st.warning(e)
    if plan.blocking_advice:
        st.error(f"**⛔ {plan.blocking_advice}**")
    ui_theme.section("贰", "为你生成的行程", plan.request.describe())
    if plan.narrative:
        st.info(plan.narrative)

    with st.container(key="daytabs"):
        tabs = st.tabs([d.day_label for d in plan.days] + ["预算与清单", "校验记分卡"])
    for day, tab in zip(plan.days, tabs[:-2]):
        with tab:
            from tripagent.models.common import fmt_cny

            unknown = [s for s in day.slots if s.poi and not s.cost_cents and s.poi.price_cents]
            c1, c2 = st.columns(2)
            c1.metric("天气", f"{day.weather.text} {temp_span(day.weather.tmin_c, day.weather.tmax_c)}" if day.weather else "天气未知")
            c2.metric("当日已知花费", fmt_cny(day.total_cost_cents) + (f"（另有 {len(unknown)} 项待确认）" if unknown else ""))
            if day.weather:
                sig = "、".join(weather_risk.day_signals(day.weather))
                tips = weather_risk.safety_tips(day.weather, day.risk)
                st.caption(sig + (f"　·　⚕️ {'；'.join(tips)}" if tips else ""))
            if day.risk and day.risk.advice:
                st.caption(f"风险等级：**{day.risk.severity.label}** · " + "；".join(day.risk.advice))
            ui_render.day_view(day, plan.request.audience or "通用")
            if unknown:
                st.caption("价格待确认的项目：" + "、".join(s.title for s in unknown)[:120])
    with tabs[-2]:
        st.markdown("#### 住宿候选排序（按你的偏好人群加权）")
        svc_ref = st.session_state.get("svc")
        ranking = getattr(svc_ref, "lodging_ranking", []) if svc_ref else []
        if not ranking:
            st.info("这次没有拿到住宿候选数据。")
        else:
            from tripagent.core.lodging import LodgingFeatures, LodgingPersona, LodgingRanker

            persona = plan.request.lodging_persona or "综合"
            c1, c2 = st.columns([2, 1])
            with c1:
                picked = st.selectbox(
                    "住宿偏好人群（已从你的话里推断，可覆盖）",
                    [x.value for x in LodgingPersona],
                    index=[x.value for x in LodgingPersona].index(persona)
                    if persona in [x.value for x in LodgingPersona] else 0,
                    key="lodgingPersona",
                )
            with c2:
                st.metric("推断依据", plan.request.lodging_reason or "未推断，默认综合")
            st.dataframe(
                pd.DataFrame([
                    {
                        "住宿": r["name"],
                        "综合分": r["score"],
                        "口碑覆盖": r["coverage"],
                        "价格": r["price"],
                        "得分来源": r["top"],
                        "网友依据": (r.get("evidence") or "")[:60],
                    }
                    for r in ranking
                ]),
                hide_index=True, width="stretch",
            )
            w = getattr(svc_ref, "lodging_weights", [])
            if w:
                st.caption("该人群权重：" + " ｜ ".join(f"{k} {v}" for k, v in w))
            if st.button("用这个人群重新排序", key="reRank"):
                ranker = LodgingRanker(svc_ref.settings.data_dir / "lodging_model.json")
                pairs = []
                for r in ranking:
                    f = LodgingFeatures(values=dict(r["features"]), known=set(r["known"]))
                    poi = next((s.poi for s in plan.all_slots if s.poi and s.poi.name == r["name"]), None)
                    if poi:
                        pairs.append((poi, f))
                if pairs:
                    st.session_state["lodgingReordered"] = [
                        (h.name, round(s, 4)) for h, f, s, _ in ranker.rank(pairs, LodgingPersona(picked))
                    ]
            if "lodgingReordered" in st.session_state:
                st.write("重排结果：", st.session_state["lodgingReordered"])
            st.markdown("**反馈给模型（可选）**：你实际会选哪家？点一下就会更新该人群的权重，下次推荐更贴合你。")
            cols = st.columns(min(4, len(ranking)))
            for col, r in zip(cols, ranking[:4]):
                if col.button(f"选 {r['name'][:10]}", key=f"pick_{r['name']}"):
                    f = LodgingFeatures(values=dict(r["features"]), known=set(r["known"]))
                    LodgingRanker(svc_ref.settings.data_dir / "lodging_model.json").learn(
                        LodgingPersona(picked), f, chosen=True
                    )
                    st.success(f"已记录：{picked} 人群更偏好 {r['name']}")
                    st.rerun()

    with tabs[-2]:
        b = [{"分项": k, "金额": f"¥{v / 100:.0f}"} for k, v in plan.budget_breakdown.items()]
        b.append({"分项": "合计", "金额": f"¥{plan.total_cost_cents / 100:.0f}"})
        if plan.request.budget_cents:
            b.append({"分项": "预算", "金额": f"¥{plan.request.budget_yuan:.0f}"})
            over = plan.total_cost_cents > plan.request.budget_cents
            b.append({"分项": "超支" if over else "结余", "金额": f"¥{abs(plan.request.budget_cents - plan.total_cost_cents) / 100:.0f}"})
        st.dataframe(pd.DataFrame(b), hide_index=True, width="stretch")
        st.markdown("**行前清单**")
        for item in plan.packing:
            st.markdown(f"- ☐ {item}")
        if plan.intercity:
            st.markdown("**跨城交通**")
            for leg in plan.intercity:
                st.caption(f"{leg.from_city} → {leg.to_city} {leg.dep_time}｜{leg.note}")
    with tabs[-1]:
        sc = plan.scorecard
        if not sc:
            st.info("未执行校验")
        else:
            ui_render.scorecard_view(sc)
        if plan.warnings:
            for wtext in plan.warnings:
                st.warning(wtext)


def _export_block(plan, svc: TripService, stem: str) -> None:
    """导出与下载：从主界面挪到「历史」页，主界面只留行程本身。"""
    paths = export_plan(plan, stem=stem, settings=svc.settings)
    mimes = {"md": "text/markdown", "html": "text/html", "ics": "text/calendar"}
    labels = {"md": "下载 Markdown", "html": "下载 HTML（可打印成 PDF）", "ics": "下载日历 .ics"}
    cols = st.columns(3)
    for col, fmt in zip(cols, ("md", "html", "ics")):
        if fmt in paths:
            col.download_button(
                labels[fmt], paths[fmt].read_bytes(), file_name=paths[fmt].name,
                mime=mimes[fmt], key=f"dl_{fmt}_{stem}",
            )


def _gate(svc: TripService):
    """口令闸门 + 到量即停。两个配置都为空时完全不启用，本地开发零打扰。"""
    from tripagent.core.access import decide
    from tripagent.providers.usage import UsageLedger

    s = svc.settings
    if not s.access_codes and not s.owner_code:
        return decide("", "", 0, 0)
    led = UsageLedger(s.usage_path)
    return decide(s.access_codes, s.owner_code, s.llm_daily_token_budget,
                  led.tokens_today(), st.session_state.get("accessInput", ""))


def _render_login(gate) -> None:
    with st.container(key="searchcard", border=True):
        st.markdown('<div class="tx-lab">体验入口 · 输入口令解锁规划</div>', unsafe_allow_html=True)
        if gate.reason:
            st.info(gate.reason)
        c1, c2 = st.columns([4.6, 1.4])
        with c1:
            code = st.text_input("体验口令", type="password", placeholder="由主办方发放")
        with c2:
            entered = st.button("进入", type="primary", width="stretch")
        if entered:
            st.session_state["accessInput"] = code.strip()
            st.rerun()
        st.markdown(
            '<div class="tx-note">天池 2026 Qoder 高校创造力大赛演示 · 联网规划消耗全站共享的模型额度，因此需要口令。</div>',
            unsafe_allow_html=True,
        )


def _use_example(ex: str) -> None:
    """快捷灵感 chip 的回调：在下一轮控件实例化之前写入，避免 widget 状态冲突。"""
    st.session_state["freeText"] = ex


def tab_plan(svc: TripService, can_run: bool = True) -> None:
    import time as _time

    waited = _time.time() - float(st.session_state.get("lastPlanAt", 0))
    cooling = max(0, PLAN_COOLDOWN_S - int(waited))

    # 头图只挂在规划页：搜索卡要压住头图下缘，中间不能隔着页签栏
    ui_theme.hero()
    with st.container(key="searchcard", border=True):
        st.markdown('<div class="tx-lab">想去哪儿 · 说一句话就够了</div>', unsafe_allow_html=True)
        c1, c2 = st.columns([4.6, 1.4])
        with c1:
            free_text = st.text_input(
                "一句话描述", key="freeText", label_visibility="collapsed",
                placeholder="成都 3天 带爸妈 爱吃火锅 预算3000 想蹦迪",
            )
        with c2:
            go = st.button("开始规划", type="primary", width="stretch",
                           disabled=cooling > 0 or not can_run)
        if cooling:
            st.caption(f"两次规划之间请等 {cooling} 秒：模型额度是全站点共享的，这条限制防止单个人反复刷。")
        if not can_run:
            st.warning("今日模型额度已用完，联网规划已暂停；「关于」页可看到今日用量，天气速查不受影响。")
        st.markdown(
            '<div class="tx-note">支持全国 3600+ 县市区与乡镇 · 先查 16 天预报再排行程 · '
            '每条推荐可回溯数据源 · 展开下方「自定义条件」可精确控制天数 / 预算 / 同行人</div>',
            unsafe_allow_html=True,
        )

    with st.container(key="quickchips"):
        examples = ["成都 3天 带小孩 爱吃火锅", "长沙 2天 朋友团玩 想蹦迪",
                    "哈尔滨 4天 情侣 预算4000", "厦门 3天 带爸妈 节奏休闲"]
        cols = st.columns(len(examples))
        for col, ex in zip(cols, examples):
            col.button(ex, key=f"chip:{ex[:4]}", on_click=_use_example, args=(ex,))

    with st.expander("自定义条件（城市 / 天数 / 日期 / 预算 / 同行人 / 兴趣）"):
        p = _inputs(free_text)
    text = _build_text(p, free_text)
    if go:
        with st.spinner("正在查天气、抓预警、检索地点、装配行程、逐条校验……"):
            outcome = svc.plan_trip(text)
        st.session_state["lastPlanAt"] = _time.time()
        st.session_state["outcome"] = outcome
        st.session_state["svc"] = svc
        if outcome.plan:
            # 落库与导出静默完成：用户要的是行程，不是"存到哪了"
            TripStore(svc.settings).save(outcome.plan, "Web 生成")
            export_plan(outcome.plan, stem=f"{p['city']}_{p['days']}天", settings=svc.settings)
    outcome = st.session_state.get("outcome")
    if outcome:
        _render_weather(outcome)
        _render_plan(outcome)


def tab_ask(svc: TripService, can_run: bool = True) -> None:
    ui_theme.section("问", "即时问一句", "酒吧 / KTV / 游乐园 / 演出 / 夜市…")
    with st.container(border=True, key="askbox"):
        text = st.text_input("问题", placeholder="成都有哪些评分高、性价比好的清吧？最好离春熙路近")
        c1, c2 = st.columns([3, 1])
        with c1:
            top = st.slider("推荐几个", 3, 10, 5)
        with c2:
            asked = st.button("问一问", type="primary", width="stretch", disabled=not can_run)
    if asked and text.strip():
        st.session_state["answer"] = svc.ask(text.strip(), limit=top)
    ans = st.session_state.get("answer")
    if not ans:
        st.caption("示例：「长沙解放西附近评分高的酒吧」「哈尔滨有什么适合亲子的室内玩乐」")
        return
    if not ans.ok:
        for wtext in ans.warnings:
            st.warning(wtext)
        return
    from html import escape as _e

    st.markdown(f"**{ans.city} · {ans.category}**")
    st.success(ans.summary)
    for i, r in enumerate(ans.items):
        with st.container(border=True, key=f"askitem-{i}"):
            if r.poi.photos:
                ui_render.photo_gallery(r.poi, f"ask:{r.poi.name[:12]}")
            badges = []
            if r.poi.rating:
                badges.append(f'<span class="tx-badge">★ {r.poi.rating}</span>')
            if r.poi.avg_cost_cents or r.poi.ticket_cents:
                badges.append(f'<span class="tx-badge price">人均 ¥{(r.poi.avg_cost_cents or r.poi.ticket_cents) / 100:.0f}</span>')
            if r.poi.open_hours_raw:
                badges.append(f'<span class="tx-badge">营业 {_e(r.poi.open_hours_raw)}</span>')
            st.markdown(
                f'<div class="tx-head"><div class="tx-med night">荐</div>'
                f'<div class="tx-title">{_e(r.poi.name)}</div>'
                f'<span class="tx-badge">{_e(r.poi.category.label)}</span>{"".join(badges)}</div>',
                unsafe_allow_html=True,
            )
            if r.poi.district or r.poi.metro:
                st.caption(f"{r.poi.district} {('· 地铁 ' + r.poi.metro) if r.poi.metro else ''}")
            if r.reasons:
                ui_render.content_line("推荐理由：" + "；".join(r.reasons[:4]), "why")
            for e in r.reviews:
                st.caption(f"网友：{e.brief(110)}  · {e.source}")
            if r.poi.pitfalls:
                ui_render.content_line("避坑：" + "；".join(r.poi.pitfalls), "pit")
            if r.poi.age_policy or r.poi.requires_id:
                st.warning(f"准入：{r.poi.age_policy or '需实名核验身份证'}")
    for wtext in ans.warnings:
        st.info(wtext)


def tab_history(svc: TripService) -> None:
    from html import escape as _e

    ui_theme.section("拾", "历史行程", "生成一次就自动落库，随时回看与下载")
    store = TripStore(svc.settings)
    rows = store.list(30)
    if not rows:
        st.info("还没有保存过行程。生成一次行程会自动落库。")
        return
    cols = st.columns(3)
    for i, r in enumerate(rows[:9]):
        with cols[i % 3]:
            with st.container(border=True, key=f"histcard-{i}"):
                st.markdown(
                    f'<div class="tx-cover c{i % 3}"><b>{_e(str(r["city"] or "行程"))}</b></div>',
                    unsafe_allow_html=True,
                )
                meta = [f'{r["days"]} 天', str(r["created_at"])[:10]]
                if r.get("budget_cents"):
                    meta.append(f'预算 ¥{r["budget_cents"] / 100:.0f}')
                if r.get("note"):
                    meta.append(str(r["note"]))
                st.markdown(
                    '<div class="tx-meta">' + "".join(f"<i>{_e(m)}</i>" for m in meta) + "</div>",
                    unsafe_allow_html=True,
                )
                if st.button("载入这份行程", key=f"load:{r['trip_id']}", width="stretch"):
                    plan = store.load(r["trip_id"])
                    if plan:
                        st.session_state["outcome"] = PlanOutcome(request=plan.request, plan=plan)
                        st.session_state["histPlan"] = plan
                        st.session_state["histTid"] = r["trip_id"]
                        st.success("已载入，切到「行程规划」页查看。")
                    else:
                        st.warning("这份历史数据读不出来（可能结构已升级）。重新生成一次即可。")
    if len(rows) > 9:
        st.caption(f"仅展示最近 9 份，共 {len(rows)} 份。")
    plan = st.session_state.get("histPlan")
    if plan:
        st.markdown("**下载最近载入的行程**")
        _export_block(plan, svc, st.session_state.get("histTid", "trip"))


def tab_about(svc: TripService) -> None:
    ui_theme.section("鉴", "关于 TripAgent", "会看天气 · 有据可查 · 逐条校验")
    st.markdown(
        "**它和其他 AI 旅行攻略的差别**\n\n"
        "1. 天气不是装饰：极端天气会直接给「不建议出行」结论并推荐改期窗口，可救的情况自动把户外换成室内；\n"
        "2. 每条推荐都有依据：评分/人均/营业时间/预约窗口/避坑点，都能回溯到数据源；\n"
        "3. 方案有记分卡：环境事实、常识约束、用户硬约束三个维度的通过率（参考 TravelPlanner 的约束分类）；\n"
        "4. 生活娱乐是一等公民：酒吧/KTV/夜店/剧场/乐园/密室有独立类目、准入门槛与夜间返程校验；\n"
        "5. 零 Key 也能跑：全部外部数据源都有本地降级，断网可演示。\n\n"
        "**数据来源**：和风天气 / Open-Meteo（数值预报）· 中国气象局 / 中央气象台（实时预警）· "
        "高德开放平台（地点、评分、人均、营业时间与实拍图）· 通义千问（联网口碑归纳与受众化推荐语）· "
        "本地知识库（离线兜底）。"
    )
    st.markdown("---")
    st.markdown("**演示与排查**（给开发者，普通用户不用管）")
    st.toggle(
        "离线演示模式（只用本地数据源，断网也能跑）",
        key="offline",
        help="开启后所有联网数据源被跳过，行程仍会完整生成，用于现场演示兜底",
    )
    s = svc.settings
    c1, c2, c3, c4 = st.columns(4)
    c1.caption(f"通义千问：{'已配置' if s.dashscope_api_key else '未配置'}")
    c2.caption(f"高德地图：{'已配置' if s.amap_key else '未配置'}")
    c3.caption(f"和风天气：{'已配置' if (s.qweather_key and s.qweather_host) else ('缺 Host' if s.qweather_key else '未配置')}")
    c4.caption(f"模型：{s.llm_model}")
    st.caption(
        f"腾讯双源校验：{'已配置' if s.tencent_map_key else '未配置（评分与营业时间只用高德单源）'}"
        + (f" · 日额度 {s.llm_daily_token_budget:,} tokens" if s.llm_daily_token_budget else "")
    )
    from tripagent.providers.usage import UsageLedger

    led = UsageLedger(s.usage_path)
    cap = s.llm_daily_token_budget
    used = led.tokens_today()
    st.caption(
        f"今日模型用量：{used:,} tokens（{led.calls_today()} 次调用）"
        + (f" · 上限 {cap:,}，剩余 {max(0, cap - used):,}" if cap > 0 else " · 未设上限")
    )
    st.caption(
        "想看某次规划的数据来源与降级轨迹（谁试过、谁失败）：\n\n"
        "`.venv\\Scripts\\python.exe -m tripagent.cli \"成都 3天\" --trace`\n\n"
        "接口自检：`http://localhost:8501` 对应的服务层 `GET /api/health`。"
    )


def main() -> None:
    ui_theme.brand()
    svc = _service(_offline_flag())
    gate = _gate(svc)
    if not gate.allowed:
        ui_theme.hero()
        _render_login(gate)
        return
    if gate.enabled and gate.who == "owner":
        st.caption("站长身份：即使今日额度用完也可继续使用。")
    t1, t2, t3, t4 = st.tabs(["🗺️ 行程规划", "🍸 即时问答", "🧳 历史行程", "🧭 关于"])
    with t1:
        tab_plan(svc, gate.costly_allowed)
    with t2:
        tab_ask(svc, gate.costly_allowed)
    with t3:
        tab_history(svc)
    with t4:
        tab_about(svc)


main()
