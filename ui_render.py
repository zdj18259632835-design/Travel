"""界面渲染层：天气结论视图 + 时段行程视图。

三处刻意的设计决定，都来自用户的真实反馈：

1. 天气不列表格。用户不关心 26.9mm 降水，只关心"这天该不该出门、出门该去哪类地方"，
   所以输出三堆：适合户外 / 建议室内 / 不建议出行。
2. 行程不显示钟点。09:00-11:00 这种写法会让人感觉在赶任务，
   旅游是来放松的；改成"上午/下午/傍晚/晚上"，保留先后但不制造打卡焦虑。
3. 通勤分钟数不显示。没有高德真实路径时它是直线估算，
   精度撑不起"28 分钟"这种表述；显示出来只会让用户不信整个 agent。
   排序时仍然用它减少折返，只是不冒充结论给用户看。
"""

from __future__ import annotations

import streamlit as st

from tripagent.core.weather_risk import window_verdicts
from tripagent.models.plan import DayPlan, Slot

BAND_ORDER = ["清晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "深夜"]

BAND_NOTE = {
    "清晨": "人最少、光线最软，适合需要早到才不用排队的地方",
    "早上": "开门第一批进，几乎不用排队，体力也最好",
    "上午": "光照正合适，室内馆厅也刚开完馆，是主力时段",
    "中午": "避开正午暴晒与饭点人流，适合安排吃饭或回酒店歇脚",
    "下午": "午后光线偏硬，适合洞穴、博物馆、街区漫步这类不受光照影响的",
    "傍晚": "日落前后一小时光线最柔，也是本地人下班后的活动起点",
    "晚上": "夜场、演出、酒吧街的营业时间，白天景点此时已清场",
    "深夜": "只适合夜宵与返程，第二天要留足睡眠",
}

# (受众, 活动分组) → 这一时段为什么适合你们。group: day/night/meal
AUDIENCE_NOTE = {
    ("亲子带娃", "day"): "孩子体力窗口短，这个时段注意力最好，安排有互动性的地方他们能坐得住",
    ("亲子带娃", "night"): "孩子睡得早，夜场选就近、能提前散场的，避免抱着回酒店",
    ("亲子带娃", "meal"): "按孩子的饭点来，饿过头就会闹，就近吃比打卡名店更重要",
    ("情侣约会", "day"): "两人都不赶时间，慢慢逛比连刷景点更容易聊起来",
    ("情侣约会", "night"): "夜色是最好的氛围加成，散场后还能沿着河边再走一段",
    ("情侣约会", "meal"): "光线暗一点、座位舒服一点的店，两个人能坐久",
    ("长辈同行", "day"): "避开正午与台阶多的时段，节奏放慢，中间留坐",
    ("长辈同行", "night"): "长辈睡得早，夜场尽量短、就近，方便回房间",
    ("长辈同行", "meal"): "按时吃饭对长辈很重要，别为了赶景点拖到两点",
    ("朋友团玩", "day"): "白天可以排体力项目，人多的话拍照排队也不亏",
    ("朋友团玩", "night"): "这才是主场：夜场、密室、livehouse 都在这个时段开门",
    ("朋友团玩", "meal"): "一群人吃饭要提前订位，这个点去刚好不用等",
    ("一个人独行", "day"): "一个人行动自由，挑自己真感兴趣的，不用迁就别人",
    ("一个人独行", "night"): "选热闹、灯多、能拼桌的地方，安全感和氛围都够",
    ("一个人独行", "meal"): "一人食友好的店在这个时段坐下更自在",
    ("商务出差", "day"): "利用工作间隙，选通勤短、不用换装的地点",
    ("商务出差", "night"): "一天的会后需要低强度放松，就近比有意思更重要",
    ("商务出差", "meal"): "宴请或简餐都看这个时段，别选排队长的店",
}

_GROUP_BY_TYPE = {"游览": "day", "观看演出": "night", "夜间娱乐": "night", "用餐": "meal", "交通移动": "day"}


def _group(slot: Slot) -> str:
    if slot.type.label in _GROUP_BY_TYPE:
        return _GROUP_BY_TYPE[slot.type.label]
    return "night" if slot.indoor and slot.start_minute >= 19 * 60 else "day"


def slot_reason(slot: Slot, audience: str) -> str:
    """时段 + 受众的理由。优先用大模型按受众改写过的推荐语，没有就用模板兜底。

    模板话也是按 (受众, 活动分组) 组合出来的，不是"值得一去"这种空话——
    理由必须回答"为什么是这个时候去、对你们意味着什么"。
    """
    llm_reason = (slot.extras or {}).get("audience_reason")
    if llm_reason:
        return str(llm_reason)
    group = _group(slot)
    flavor = AUDIENCE_NOTE.get((audience, group))
    band = BAND_NOTE.get(slot.band, "")
    parts = [p for p in (band, flavor) if p]
    return "；".join(parts) if parts else "按先后顺序安排，避免同一天来回折返"


def weather_panel(weather, window, alarms) -> None:
    """天气结论：三堆分类 + 一句人话，不出现表格与毫米数。"""
    groups = window_verdicts(weather, window)
    label = {"outdoor": ("🌤 适合全天户外", "success"), "indoor": ("🏛 建议以室内为主", "warning"),
             "avoid": ("⛔ 不建议出行", "error")}
    for key, (title, _) in label.items():
        rows = groups.get(key) or []
        if not rows:
            continue
        st.markdown(f"**{title}**")
        for v in rows:
            st.markdown(
                f"`{v['date']} {v['weekday']}` **{v['headline']}** — {v['why']}\n\n"
                f"　→ {v['advice']}"
            )
            if v.get("tips"):
                st.caption(f"　　⚕️ {v['tips']}")
        st.write("")
    missing = groups.get("missing") or []
    if missing:
        st.caption(f"这些日期超出 16 天预报窗口，无法判断：{'、'.join(missing)}")
    if alarms:
        seen: set[tuple[str, str]] = set()
        lines = []
        for a in alarms:
            k = (a.alarm_type or a.title[:6], a.level)
            if k in seen:
                continue
            seen.add(k)
            lines.append(f"**[{a.level or '-'}] {a.alarm_type or '气象预警'}** — {a.title}")
        if lines:
            st.markdown("**当前生效气象预警**")
            for ln in lines:
                st.caption(ln)
    else:
        st.caption("当前无生效气象预警。")


def gallery(urls: list[str], caption: str = "", source_note: str = "高德公开图") -> str:
    """活动实拍图的纯 CSS 轮播。

    为什么不用 JS：Streamlit 的 markdown 会剥掉 <script>，塞 iframe 又要固定高度、
    一天十几个卡片各自一个文档，代价比效果大。CSS keyframes 交叉淡入淡出足够，
    而且断网、无 CDN、无依赖也能跑（和整体样式层一个原则）。
    """
    import html as _h

    pics = [u for u in urls if isinstance(u, str) and u.startswith("http")][:4]
    if not pics:
        return ""
    imgs = "".join(
        f'<img src="{_h.escape(u, quote=True)}" alt="" loading="lazy" referrerpolicy="no-referrer">'
        for u in pics
    )
    cap = _h.escape(f"{caption} · {source_note}" if caption else source_note)
    return f'<div class="gal n{len(pics)}">{imgs}<span class="cap">{cap}</span></div>'


def day_panel(day: DayPlan, audience: str) -> str:
    """一天的 HTML：实拍图 + 时段 + 项目 + 理由，不显示钟点与通勤。"""
    from tripagent.models.common import fmt_cny

    import html as _h

    chunks = []
    for band, slots in day.by_band():
        items = []
        for s in slots:
            badges = []
            if s.cost_cents:
                badges.append(fmt_cny(s.cost_cents))
            elif s.poi and s.poi.price_cents == 0 and s.type.label != "入住退房":
                badges.append("免费")
            elif s.poi:
                badges.append("价格待确认")
            if s.reservation_hint:
                badges.append(f"⏰ {s.reservation_hint}")
            lines = [f"为什么这个时段去：{slot_reason(s, audience)}"]
            if s.poi and s.poi.why and not (s.extras or {}).get("audience_reason"):
                lines.append(f"亮点：{s.poi.why[:90]}")
            if s.poi and s.poi.pitfalls:
                lines.append("避坑：" + "；".join(s.poi.pitfalls[:2]))
            kind = "night" if s.type.label in ("夜间娱乐", "观看演出") else ("meal" if s.type.label == "用餐" else "day")
            pic = gallery(s.poi.photos, s.poi.name) if s.poi else ""
            items.append(_card(_h.escape(s.title), kind, [_h.escape(b) for b in badges],
                               [_h.escape(x) for x in lines], pic))
        chunks.append(f'<div class="band"><div class="bandname">{_h.escape(band)}</div>{"".join(items)}</div>')
    return "".join(chunks)


def _card(title: str, kind: str, badges: list[str], lines: list[str], pic: str = "") -> str:
    tags = "".join(f'<span class="tag">{b}</span>' for b in badges)
    body = "".join(f'<div class="why">{ln}</div>' for ln in lines)
    return f'<div class="slot {kind}">{pic}<b>{title}</b>{tags}{body}</div>'
