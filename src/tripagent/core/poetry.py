"""诗词彩蛋的匹配规则（纯函数，不碰网络、不碰磁盘）。

为什么放在 core 而不是 provider 里：provider 只负责"把库拿出来"，
而"这一趟里同一句不许出现两遍"是**行程层面的规则**，它要看到整趟的顺序。

三条硬约束：
1. 关键词命中优先于城市兜底——"爱晚亭"必须拿到《山行》，不能拿到长沙那句通用句；
2. **一句诗在一趟行程里只出现一次**，库用完之后宁可留空。
   这条是被用户反馈教出来的：天气建议那阵子"五天五句一模一样，等于没说"。
   如果每个时段都摊一句"晓看红湿处，花重锦官城"，观感比没有彩蛋更差，
   因为一眼就能看出是模板；
3. 库里没有这个城市 → 整趟都没有诗。缺东西比编东西诚实。
"""

from __future__ import annotations

from ..models.enums import SlotType
from ..models.plan import Slot, TripPlan

# 这些时段挂诗会显得莫名其妙：正在坐高铁 / 正在酒店睡觉 / 休息缓冲
SKIP_TYPES = {SlotType.TRANSPORT, SlotType.CHECKIN, SlotType.REST}


def entry_score(entry: dict, text: str) -> int:
    """0 = 不相关；1 = 城市通用句；3 = 命中具体地点关键词。

    关键词比通用句分高，所以"寒山寺"一定拿到《枫桥夜泊》，
    不会拿到苏州那句泛写水巷的。
    """
    if not isinstance(entry, dict) or not text:
        return 0
    for kw in entry.get("match") or []:
        if kw and kw in text:
            return 3
    return 1 if not (entry.get("match") or []) else 0


def slot_text(s: Slot) -> str:
    """匹配用的文本：时段标题 + 地点名 + 标签 + 亮点。

    把标签也算进来，"川剧"这种不在 POI 名里、却最能决定选哪句的线索才不会丢。
    """
    parts = [s.title or ""]
    if s.poi:
        parts += [s.poi.name, " ".join(s.poi.tags or []), s.poi.why or ""]
    return " ".join(p for p in parts if p)


def relevant(entries: list[dict], text: str) -> list[int]:
    """与这段行程相关的条目下标，相关度高的在前；同分保持库内顺序（可复现）。"""
    scored = [(entry_score(entries[i], text), i) for i in range(len(entries))]
    return [i for _, i in sorted([sc for sc in scored if sc[0]], key=lambda x: (-x[0], x[1]))]


def attach(plan: TripPlan, entries: list[dict]) -> int:
    """按整趟顺序给时段挂诗，写进 slot.extras["poem"]，返回挂上的条数。"""
    if not entries:
        return 0
    used: set[int] = set()
    n = 0
    for s in plan.all_slots:
        if s.type in SKIP_TYPES:
            continue
        got = next((i for i in relevant(entries, slot_text(s)) if i not in used), None)
        if got is None:
            continue        # 库用完了就空着，不重复上一句
        used.add(got)
        e = entries[got]
        s.extras["poem"] = {
            "lines": [str(x) for x in (e.get("lines") or []) if str(x).strip()],
            "author": str(e.get("author") or ""),
            "from": str(e.get("from") or ""),
            "note": str(e.get("note") or ""),
        }
        n += 1
    return n
