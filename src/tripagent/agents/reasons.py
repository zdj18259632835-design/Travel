"""受众绑定的推荐语生成。

解决的是"推荐理由很笼统"这个具体抱怨：不再输出"很有特色、值得一去"，
而是针对当次同行人群回答一个明确的问题——
亲子要写"孩子能收获什么"，情侣要写"靠什么增进关系"，长辈要写"体力与舒适度怎么被照顾"。

输出格式用「项目名|推荐语」的竖线行，不用 JSON：
qwen 开 enable_search 后会在中文里写未转义引号打断 JSON，且失败点每次都不一样
（providers/culture.py 与 evidence.py 都踩过），扁平格式对这种噪声天然免疫。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from ..models.plan import TripPlan
from ..providers.base import Chain

REASON_PROMPT = """你在为「{audience}」人群写旅行推荐语。下面是已排定的行程条目，\
请逐条写一句 40-80 字的推荐语，必须回答这个问题：{ask}
输出 {n} 行，每行「项目名|推荐语」，用竖线分隔。
硬性要求：写具体可感的点（时段、体感、互动方式、能带走什么、要排多久的队）；
禁止"很有特色""值得一去""不容错过"这类空话；
不要编造价格、营业时间与预约规则，需要提醒就引用条目里给的 hint。
条目：
{rows}"""

_SKIP_TYPES = ("入住退房", "自由活动")


def _row_lines(plan: TripPlan, limit: int = 18) -> list[str]:
    rows: list[str] = []
    for d in plan.days:
        for s in d.slots:
            if not s.poi or s.type.label in _SKIP_TYPES:
                continue
            hints = [
                f"人均¥{s.cost_cents / 100:.0f}" if s.cost_cents else "",
                s.reservation_hint or "",
                "；".join(s.poi.pitfalls[:2]),
                s.poi.best_time or "",
            ]
            hint = "；".join(h for h in hints if h)
            rows.append(f"{d.index}. {s.title}|{(s.poi.why or '')[:70]}|提示:{hint}")
            if len(rows) >= limit:
                break
    return rows


def parse_reason_lines(raw: str) -> dict[str, str]:
    """解析「项目名|推荐语」。

    必须先把全角 ｜ 换成半角 |：喂给模型的样例用的是全角分隔符，
    模型会照着抄，于是 16 条里只有 6 条被认出来，其余全部退回模板 —— 
    用户看到的就是"推荐理由很笼统"。
    """
    out: dict[str, str] = {}
    for line in (raw or "").splitlines():
        text = line.strip().lstrip("-*•　 ")
        text = text.replace("｜", "|")
        if "|" not in text:
            continue
        name, _, reason = text.partition("|")
        name = re.sub(r"^\d+[.、]\s*", "", name).strip()
        reason = reason.strip()
        if len(name) >= 2 and len(reason) >= 12:
            out[name] = reason[:180]
    return out


def fallback_reason(poi, audience_label: str) -> str:
    """没有大模型时的模板：只组装已有证据，不编形容词。"""
    bits = []
    if poi.best_time:
        bits.append(f"最佳时段 {poi.best_time}")
    if poi.pitfalls:
        bits.append(f"注意：{poi.pitfalls[0]}")
    if poi.tags:
        bits.append("标签 " + "、".join(poi.tags[:3]))
    body = "；".join(bits)
    lead = f"【{audience_label}】" if audience_label else ""
    return f"{lead}{poi.why or '该地点依据公开评分与网友反馈入选'}" + (f"；{body}" if body else "")


def personalize(
    plan: TripPlan,
    audience_label: str,
    ask: str,
    chain: Chain,
    chunk: int = 6,
) -> tuple[int, str]:
    """返回 (被改写的条数, 说明)。失败时条数为 0，原依据保留，绝不空手而归。

    为什么要分片并发：一次让模型写 14 条，输出长度就是耗时（实测 18.5s）。
    拆成每片 6 条并发跑，单片 5-7s，总耗时接近一片——内容一条没少，
    只是不再让模型一次憋完。
    """
    rows = _row_lines(plan)
    n = len(rows)
    if not n:
        return 0, "没有需要写推荐语的实体安排"

    def ask_chunk(part: list[str]) -> dict[str, str]:
        messages = [
            {"role": "system", "content": "你只输出「项目名|推荐语」这些行，不输出解释。"},
            {
                "role": "user",
                "content": REASON_PROMPT.format(
                    audience=audience_label or "通用", ask=ask, n=len(part), rows="\n".join(part)
                ),
            },
        ]
        res = chain.run("llm", "chat", messages, temperature=0.5)
        table = parse_reason_lines(str(res.value or ""))
        if not table:
            why = " | ".join(a.error for a in res.attempts if not a.ok)[:160]
            raise RuntimeError(why or "输出格式不符")
        return table

    groups = [rows[i : i + chunk] for i in range(0, n, chunk)]
    table: dict[str, str] = {}
    errors: list[str] = []
    if len(groups) == 1:
        try:
            table = ask_chunk(groups[0])
        except RuntimeError as exc:
            errors.append(str(exc))
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(groups))) as pool:
            for got in pool.map(_safe_chunk(ask_chunk), groups):
                if isinstance(got, str):
                    errors.append(got)
                else:
                    table.update(got)

    if not table:
        for slot in plan.all_slots:
            if slot.poi and slot.type.label not in _SKIP_TYPES:
                slot.notes = [f"【{audience_label}】{fallback_reason(slot.poi, audience_label)}", *slot.notes][:4]
        return 0, f"大模型推荐语未生成（{(errors or ['输出格式不符'])[0]}），已退回模板依据"

    touched = 0
    for slot in plan.all_slots:
        if not slot.poi or slot.type.label in _SKIP_TYPES:
            continue
        reason = table.get(slot.title)
        if not reason:
            for name, r in table.items():
                if name and (name in slot.title or slot.title in name):
                    reason = r
                    break
        if not reason:
            continue
        slot.notes = [f"【{audience_label}】{reason}"] + [x for x in slot.notes if not x.startswith("【")][:2]
        # 合并而不是整块替换：替换会把装配阶段已经挂进去的 extras（如诗词彩蛋）抹掉
        slot.extras = {**(slot.extras or {}), "audience_reason": reason, "audience": audience_label}
        touched += 1
    note = f"按「{audience_label}」分 {len(groups)} 片并发改写 {touched}/{n} 条推荐语"
    if errors:
        note += f"（{len(errors)} 片失败，已用模板补齐）"
    return touched, note


def _safe_chunk(fn):
    """线程池里抛异常会吞掉整批结果，这里把每片失败降级成一条错误说明。"""

    def wrapper(part: list[str]):
        try:
            return fn(part)
        except Exception as exc:
            return f"{type(exc).__name__}: {str(exc)[:100]}"

    return wrapper
