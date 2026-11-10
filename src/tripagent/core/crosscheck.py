"""双源比对：把第二个数据源用来提高可信度，而不是用来覆盖第一个。

三条原则：

1. **一致 → 加置信度**。两个独立来源给出同样的评分，这条推荐的可靠性确实更高。
2. **分歧 → 两个数字都摆出来**，绝不擅自挑一个"看起来对的"。
   用户看到"高德 4.3 / 腾讯 3.9"会自己判断，这比我们把 3.9 悄悄写进方案诚实。
3. **缺失 → 可以补，但要记来源**。高德的 `business.cost` 经常是空的，
   腾讯给了人均就用，同时在 raw 里标 `cost_source=tencent`，报告与界面都能说明出处。
"""

from __future__ import annotations

import re
from typing import Any

_AGREE_DELTA = 0.2      # 评分差在这个以内算一致
_CONFLICT_DELTA = 0.4   # 超过这个就必须告诉用户两源打架


def _hhmm(text: str) -> tuple[int, int] | None:
    """从 '10:00-22:00' / '每日 09:00-17:00' 里抠出分钟区间。"""
    m = re.search(r"(\d{1,2})[:：](\d{2})\s*[-~至]\s*(\d{1,2})[:：](\d{2})", text or "")
    if not m:
        return None
    lo = int(m.group(1)) * 60 + int(m.group(2))
    hi = int(m.group(3)) * 60 + int(m.group(4))
    return lo, hi


def _time_conflict(a: str, b: str) -> bool:
    """两边都能解析出区间、且任一边界相差 60 分钟及以上，才算冲突。

    一小时不是"小误差"：按 9 点到了却发现 10 点开门，整个上午就废了。
    解析不出来不算冲突——"格式不同"和"内容不同"是两件事，
    把前者报成后者会让人以为数据源在乱来。
    """
    ta, tb = _hhmm(a), _hhmm(b)
    if not ta or not tb:
        return False
    return abs(ta[0] - tb[0]) >= 60 or abs(ta[1] - tb[1]) >= 60


def reconcile(pois: list[Any], table: dict[str, dict]) -> dict[str, int]:
    """就地更新 POIRecord，返回统计（agree/conflict/filled/skipped）。"""
    stats = {"agree": 0, "conflict": 0, "filled": 0, "skipped": 0}
    for p in pois:
        t = table.get(p.name)
        if not t:
            stats["skipped"] += 1
            continue
        checked = False

        r1, r2 = p.rating, t.get("rating")
        if r1 and r2:
            checked = True
            delta = abs(float(r1) - float(r2))
            if delta <= _AGREE_DELTA:
                p.confidence = min(0.95, (p.confidence or 0.6) + 0.05)
                if "双源评分一致" not in p.tags:
                    p.tags = [*p.tags, "双源评分一致"][:8]
                stats["agree"] += 1
            elif delta >= _CONFLICT_DELTA:
                p.confidence = max(0.2, (p.confidence or 0.6) - 0.12)
                note = f"评分两源不一致（高德 {r1:.1f} / 腾讯 {r2:.1f}），建议现场核实"
                if note not in p.pitfalls:
                    p.pitfalls = [note, *p.pitfalls][:6]
                stats["conflict"] += 1

        if p.open_hours_raw and t.get("open_time") and _time_conflict(p.open_hours_raw, t["open_time"]):
            checked = True
            note = f"营业时间两源不一致（高德 {p.open_hours_raw[:24]} / 腾讯 {t['open_time'][:24]}），出发前电话确认"
            if note not in p.pitfalls:
                p.pitfalls = [note, *p.pitfalls][:6]
            stats["conflict"] += 1

        if not p.avg_cost_cents and t.get("cost"):
            checked = True
            p.avg_cost_cents = int(round(float(t["cost"]) * 100))
            p.raw = {**(p.raw or {}), "cost_source": "tencent"}
            stats["filled"] += 1

        if not checked:
            stats["skipped"] += 1
    return stats
