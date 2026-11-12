"""时段分组说明：从固定文案改成"对得上才说，对不上就解释"。

起因是用户的一条反馈：界面上写着"中午避开正午暴晒与饭点人流，适合安排吃饭或回酒店歇脚"，
下面排的却是露天主题公园野餐；另一条写着"下午适合洞穴、博物馆"，配图是阳光强烈的开阔园区。
**说明与安排互相打脸，比没有说明更糟**——用户会认为这套话术是模板贴上去的。

规则：
1. 先如实摘要这一档实际排了什么（类目 + 室内/户外）；
2. 有当日预报时，把光照事实摆出来（气温、紫外线、日落时刻）；
3. 通用说明与实际安排**不冲突**才引用它；冲突就改成解释句，
   说明"为什么还是排了它"以及怎么缓解（遮阳、挪时段、缩短停留）。
"""

from __future__ import annotations

from .light import light_level

# 与 ui_render.BAND_NOTE 同一批时段词；这里只保留"能被光照证伪"的那几档
_CONFLICTABLE = {
    "中午": ("避开正午暴晒", ("硬光",)),
    "下午": ("不受光照影响", ("硬光",)),
}


def _kinds(slots) -> str:
    """把这一档实际排了什么说清楚：露天两处、室内一处。"""
    out = sum(1 for s in slots if not s.indoor and s.poi)
    inner = sum(1 for s in slots if s.indoor and s.poi)
    parts = []
    if out:
        parts.append(f"露天 {out} 处")
    if inner:
        parts.append(f"室内 {inner} 处")
    if not parts:
        return ""
    names = "、".join((s.poi.name if s.poi else s.title)[:12] for s in slots if s.poi)[:60]
    return f"这一档实际排了{'，'.join(parts)}（{names}）"


def schedule_note(band: str, slots, forecast, generic: str = "") -> str:
    """生成一句与实际安排一致的时段说明。"""
    summary = _kinds(slots)
    level = light_level(min((s.start_minute for s in slots), default=12 * 60), forecast) if slots else "未知"
    facts = ""
    if forecast is not None:
        bits = []
        if forecast.tmax_c is not None:
            bits.append(f"{forecast.tmax_c:.0f}℃")
        if forecast.uv_index:
            bits.append(f"紫外线 {forecast.uv_index:.0f}")
        if forecast.sunset:
            bits.append(f"日落 {forecast.sunset}")
        facts = "、".join(bits)
    conflict = (
        band in _CONFLICTABLE
        and level in _CONFLICTABLE[band][1]
        and any(not s.indoor for s in slots)
    )
    if conflict:
        # 说明不成立，就别再念它——改成"为什么还是这么排 + 怎么缓解"
        head = f"当日{facts}，正午前后光照偏强" if facts else "这一时段光照偏强"
        return (
            f"{head}；{summary}。"
            "露天项放在这里是因为当天室内名额已排满或它本身有树荫/水面可避暑，"
            "建议带遮阳与饮水、把停留压短，或与傍晚那档对调。"
        )
    if generic and summary:
        return f"{generic}；{summary}" + (f"（当日{facts}）" if facts else "")
    return generic or summary
