"""光照时段规则：把"什么时候该在户外"变成可解释的加减分。

为什么需要它：装配器原来只在**恶劣天气**（预警/大雨）时才偏向室内，
而 23~29℃ 晴天的三亚根本触发不了那条规则，于是：

- 中午 12 点排了露天公园野餐（分组说明还写着"避开正午暴晒"，自相矛盾）；
- 下午 3 点光线最硬的时候排了阳光强烈的开阔园区（用户原话：这该是傍晚去的）。

这里刻意只做**排序偏置 + 一句理由**，不做硬否决：
阴天、雨天、山地小气候都可能让规则不适用，
把露天项目在正午彻底禁掉会让行程排不出来；扣分让它排在室内之后就够了。
"""

from __future__ import annotations

HARD_START = 11 * 60          # 11:00
HARD_END = 15 * 60            # 15:00
UV_STRONG = 6.0               # 紫外线指数 ≥6 需要防晒
TEMP_HOT = 28.0               # 当日最高温
SOFT_BEFORE_SUNSET = 60       # 日落前 60 分钟是"光线最柔"的窗口

# 露天最怕的天气文字（拿不到降水数据时按文字兜底）
_WET_WORDS = ("雨", "雪", "雾", "霾")


def _minute(hhmm: str | None) -> int | None:
    if not hhmm or ":" not in hhmm:
        return None
    h, m = hhmm.split(":")[:2]
    try:
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def light_level(start_minute: int, forecast) -> str:
    """硬光 / 柔光 / 弱光 / 未知。只用当日预报里的事实，不做任何猜测。"""
    if forecast is None:
        return "未知"
    text = str(forecast.text or "")
    wet = any(w in text for w in _WET_WORDS)
    uv = forecast.uv_index or 0
    hot = (forecast.tmax_c or 0) >= TEMP_HOT
    sunset = _minute(forecast.sunset)
    if HARD_START <= start_minute < HARD_END and not wet and (uv >= UV_STRONG or hot):
        return "硬光"
    if sunset and sunset - SOFT_BEFORE_SUNSET <= start_minute <= sunset:
        return "柔光"
    if sunset and start_minute > sunset:
        return "弱光"
    if wet:
        return "阴雨"
    return "常规"


def bias(is_indoor: bool, start_minute: int, forecast) -> tuple[float, str]:
    """返回 (加减分, 给用户看的理由)。理由为空表示这一档不改分。"""
    level = light_level(start_minute, forecast)
    if level == "硬光":
        facts = []
        if forecast.uv_index:
            facts.append(f"紫外线 {forecast.uv_index:.0f}")
        if forecast.tmax_c is not None:
            facts.append(f"最高 {forecast.tmax_c:.0f}℃")
        head = "、".join(facts) or "正午光照强"
        if is_indoor:
            return 20.0, f"{head}：室内项目排在正午更舒服（+20）"
        return -25.0, f"{head}：正午露天体验最差，让位给室内（−25）"
    if is_indoor:
        return 0.0, ""
    if level == "柔光":
        return 15.0, "日落前一小时光线最柔，露天景观与拍照加分（+15）"
    if level == "弱光":
        return -20.0, "日落之后露天视线差，除非是夜景灯光类项目（−20）"
    if level == "阴雨":
        return -15.0, "当日有降水，露天体验打折（−15）"
    return 0.0, ""
