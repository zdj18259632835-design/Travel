"""界面冒烟测试：真的把 app.py 跑一遍，而不是只做语法检查。

为什么需要这个文件：用户报过一次 `NameError: name 'p' is not defined`——
`py_compile` 完全抓不到这类错误（语法合法、运行时才炸），
而 Streamlit 的 AppTest 会真正执行脚本并收集异常，正好补上这个洞。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

os.environ["TRIPAGENT_OFFLINE"] = "1"
os.environ["DASHSCOPE_API_KEY"] = ""
os.environ["AMAP_KEY"] = ""
os.environ["QWEATHER_KEY"] = ""


def _make_plan():
    """离线生成一份真实结构的行程，喂给界面。"""
    from tripagent.agents.service import TripService
    from tripagent.config import reload_settings

    svc = TripService(reload_settings())
    return svc.plan_trip("成都 3天 带一个小孩 爱吃火锅 想体验夜生活 预算2400")


@pytest.fixture(scope="module")
def outcome():
    return _make_plan()


def test_app_renders_without_exception(outcome):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception, f"首次渲染就抛异常：{[e.value for e in at.exception]}"

    # 把已生成的行程塞进 session_state，强制走「结果渲染」分支——
    # 用户那次报错正是发生在这里
    at.session_state["outcome"] = outcome
    at.session_state["svc"] = None
    at.run()
    bad = [e.value for e in at.exception]
    assert not bad, f"渲染行程结果时抛异常：{bad}"


def test_weather_panel_has_no_numeric_table(outcome):
    """天气必须给结论而不是表格：用户明确要求去掉毫米数与"-"占位。"""
    from tripagent.core.weather_risk import window_verdicts

    req = outcome.request
    window = [req.start_date + timedelta(days=i) for i in range(req.days)]
    groups = window_verdicts(outcome.weather, window)
    for key in ("outdoor", "indoor", "avoid"):
        for v in groups[key]:
            assert v["verdict"] in ("outdoor", "indoor", "avoid")
            assert "mm" not in v["why"], "结论里不该出现毫米数"
            assert "-" != v["headline"].strip()
    assert sum(len(groups[k]) for k in ("outdoor", "indoor", "avoid")) == len(window)


def test_slots_expose_time_band_not_clock(outcome):
    """时段词必须覆盖所有安排，且顺序符合 上午→下午→傍晚→晚上。"""
    plan = outcome.plan
    order = ["清晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "深夜"]
    for day in plan.days:
        bands = [b for b, _ in day.by_band()]
        assert bands, f"Day{day.index} 没有时段分组"
        assert bands == sorted(bands, key=order.index), f"时段顺序乱了：{bands}"


def test_rendered_app_shows_no_clock_ranges(outcome):
    """用户连着两次反馈"09:00-11:00 还在"：旧的逐条卡片残留在 app.py 里。

    只测 day_panel 挡不住这种"新视图旁边又渲染了一遍旧视图"，
    所以这里跑真实的 app.py，检查旧卡片的时间列有没有复活。
    口碑正文里的"7:30-9:30 熊猫活跃"是描述性文字，不在禁止范围内。
    """
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.session_state["outcome"] = outcome
    at.session_state["svc"] = None
    at.run()
    assert not at.exception
    blobs = [str(getattr(m, "value", "")) for m in at.markdown]
    hits = [b[:120] for b in blobs if '<span class="t">' in b]
    assert not hits, f"界面仍在按钟点列出行程：{hits}"


def test_gallery_widgets_render_and_no_numeric_pitfalls_leak(outcome):
    """两件事一起守：

    1. 文旅字段错位时界面会输出「避坑：0.8」这种把置信度当避坑点的话；
    2. 图片改成原生翻页后，每个卡片都要有唯一 widget key —— key 撞了 Streamlit
       会直接报错，而这种错在纯 HTML 时代不存在，所以必须专门跑一次真实界面。
    """
    for day in outcome.plan.days:
        for s in day.slots:
            for p in (s.poi.pitfalls if s.poi else []):
                assert not re.fullmatch(r"0?\.\d+", str(p)), (s.title, p)

    # 造几张图，强制走图片分支（离线 fixture 默认没有图）
    for i, s in enumerate(outcome.plan.all_slots):
        if s.poi:
            s.poi.photos = [f"https://example.invalid/{i}.jpg", f"https://example.invalid/{i}b.jpg"]

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.session_state["outcome"] = outcome
    at.session_state["svc"] = None
    at.run()
    assert not at.exception, [e.value for e in at.exception]

    labels = _labels(at.button)
    assert "◀" in labels and "▶" in labels and "🔍 放大看" in labels, labels
    gal_keys = [str(getattr(b, "key", "")) for b in at.button
                if str(getattr(b, "key", "")).startswith(("prev:", "next:", "zoom:"))]
    assert len(gal_keys) >= 3 and len(gal_keys) == len(set(gal_keys)), gal_keys
    texts = " ".join(str(getattr(m, "value", "")) for m in at.markdown)
    assert not re.search(r"避坑[：:]\s*0?\.\d", texts), texts[:200]


def test_every_slot_has_audience_reason(outcome):
    """每条安排都要有理由，且理由不能是空话。"""
    forbidden = ("值得一去", "很有特色", "不容错过", "不错")
    plan = outcome.plan
    for day in plan.days:
        for s in day.slots:
            if not s.poi:
                continue
            from ui_render import slot_reason

            reason = slot_reason(s, plan.request.audience or "亲子带娃")
            assert len(reason) >= 12, f"{s.title} 的理由太短：{reason!r}"
            assert not any(word in reason for word in forbidden), f"空话：{reason}"


def test_cost_unknown_is_not_reported_as_zero(outcome):
    """价格缺失要标"待确认"，不能显示 ¥0 让用户以为免费。"""
    plan = outcome.plan
    for day in plan.days:
        for s in day.slots:
            if s.type.label == "入住退房":
                continue  # 住宿按「间 × 晚」计在 budget_breakdown，不落在逐日 slot 上
            if s.poi and not s.cost_cents and (s.poi.ticket_cents or s.poi.avg_cost_cents):
                pytest.fail(f"{s.title} 有价格却没计入花费")
    lodging = [s for d in plan.days for s in d.slots if s.type.label == "入住退房"]
    if lodging:
        assert any(k.startswith("住宿") for k in plan.budget_breakdown),             "住宿有候选却没进预算分项，等于悄悄漏掉最大的一笔开销"


def _labels(items):
    return [str(getattr(x, "label", "")) for x in items]


def test_login_gate_renders_and_unlocks_in_real_app(monkeypatch):
    """闸门只在真跑 app.py 时才算验证过：单测挡不住"控件接线错"这类问题。"""
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("TRIPAGENT_ACCESS_CODES", "tianchi2026")
    monkeypatch.setenv("TRIPAGENT_OWNER_CODE", "owner-x")

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "体验口令" in _labels(at.text_input), _labels(at.text_input)
    assert "开始规划" not in "".join(_labels(at.button)), "陌生访客不该看到功能按钮"

    # 错口令：仍然挡住
    at.text_input[0].set_value("猜一下")
    at.button[0].click().run()
    assert "开始规划" not in "".join(_labels(at.button)), _labels(at.button)

    # 主办方口令：放行到完整界面
    at2 = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at2.run()
    at2.text_input[0].set_value("tianchi2026")
    at2.button[0].click().run()
    assert "开始规划" in "".join(_labels(at2.button)), _labels(at2.button)


def test_owner_keeps_working_after_budget_exhausted(monkeypatch, outcome):
    """到量即停必须真的停在界面上：访客按钮变灰，站长按钮可用。"""
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("TRIPAGENT_ACCESS_CODES", "tianchi2026")
    monkeypatch.setenv("TRIPAGENT_OWNER_CODE", "owner-x")
    monkeypatch.setenv("TRIPAGENT_LLM_DAILY_TOKEN_BUDGET", "1")
    from tripagent.config import reload_settings
    from tripagent.providers.usage import UsageLedger

    s = reload_settings()
    UsageLedger(s.usage_path).record(prompt=10)
    try:
        for code, expect_disabled in (("tianchi2026", True), ("owner-x", False)):
            at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
            at.run()
            at.text_input[0].set_value(code)
            at.button[0].click().run()
            plan_btn = [b for b in at.button if "开始规划" in str(getattr(b, "label", ""))]
            assert plan_btn, f"{code} 应已进入主界面"
            assert bool(plan_btn[0].disabled) is expect_disabled, (code, plan_btn[0].disabled)
    finally:
        monkeypatch.delenv("TRIPAGENT_LLM_DAILY_TOKEN_BUDGET")
        UsageLedger(reload_settings().usage_path).reset()
