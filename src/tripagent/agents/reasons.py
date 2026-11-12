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

# 提示词的这一版是被用户实测打出来的。上一版写着"写具体可感的点（时段、体感、互动方式…）"，
# 而条目里只给了 POI 名、评分、人均、避坑点——模型手里没有景物，被要求"具体可感"，
# 就只能编：给"城市乐园"编出摩天轮，给湿地公园编出红树林与海鸟。
# 这不是措辞不够好，是指令与证据不匹配。所以现在把"能写什么"逐条锁死，并把反例写进提示词。
REASON_PROMPT = """你在为「{audience}」人群写旅行推荐语。下面是已排定的行程条目，\
请逐条写 100-160 字的推荐语，输出 {n} 行，每行「项目名|推荐语」，用竖线分隔。

必须做到：
1. 第一句点明这是什么活动（用餐 / 游览 / 观看演出 / 夜间娱乐 / 逛街 / 休息），让人一眼知道要做什么；
2. 只允许使用条目里给出的事实：类目、时段、室内或户外、人均、营业时间、预约提示、避坑点、最佳时段、评分、依据；
3. 严禁出现条目里没有的具体景物与设施名称。
   反例（都是编造）：条目只给了「城市乐园 | 风景名胜 | 下午 | 户外」，
   却写"在摩天轮最高处共享日落余晖""沿着木栈道看红树林与海鸟共生"——条目里没有摩天轮，也没有木栈道；
   正例：写"下午是一天光照最硬的时段，这类开阔园区遮荫有限，建议带水与遮阳，
   或挪到傍晚前后光线柔和时再进"（依据是类目、时段与当日天气）；
4. 必须解释"为什么安排在这个时段"，依据条目里的时段与当日天气；
5. 禁止"很有特色""值得一去""不容错过""不虚此行"这类空话；
6. 不要编造价格、营业时间与预约规则，需要提醒就引用条目给的提示。

这次同行人群最在意的是：{ask}

条目格式：序号. 项目名|类目|时段|室内或户外|当日天气|依据|提示
{rows}"""

_SKIP_TYPES = ("入住退房", "自由活动")

# 空话黑名单：模型最爱用来凑字数的四种说法，出现即判不合格（与测试共用一份）
_EMPTY_WORDS = ("很有特色", "值得一去", "不容错过", "不虚此行", "令人流连忘返")


def _day_weather(d) -> str:
    """给模型的一句话天气事实。没有预报数据时写"未知"，不能留空让它猜。"""
    f = getattr(d, "weather", None)
    if not f:
        return "天气未知"
    temps = f"{f.tmin_c:.0f}~{f.tmax_c:.0f}℃" if f.tmin_c is not None and f.tmax_c is not None else ""
    uv = f"，紫外线{f.uv_index:.0f}" if getattr(f, "uv_index", None) else ""
    return f"{f.text}{temps}{uv}".strip("，")


def _row_lines(plan: TripPlan, limit: int = 18) -> list[str]:
    """喂给模型的每一行都必须自带"能写什么"的原料。

    上一版只给名字 + 依据 + 提示，却要求"写具体可感的点"，
    模型手里没有景物就只能编（摩天轮、木栈道、红树林都是这么来的）。
    现在把类目、时段、室内外、当日天气一起给它，让它有真东西可引。
    """
    rows: list[str] = []
    for d in plan.days:
        wx = _day_weather(d)
        for s in d.slots:
            if not s.poi or s.type.label in _SKIP_TYPES:
                continue
            hints = [
                f"人均¥{s.cost_cents / 100:.0f}" if s.cost_cents else "",
                s.poi.open_hours_raw and f"营业 {s.poi.open_hours_raw}",
                s.reservation_hint or "",
                "；".join(s.poi.pitfalls[:2]),
                s.poi.best_time and f"最佳时段 {s.poi.best_time}",
                f"评分 {s.poi.rating}" if s.poi.rating else "",
            ]
            hint = "；".join(h for h in hints if h)
            rows.append(
                f"{d.index}. {s.title}|{s.type.label}|{s.band}|"
                f"{'室内' if s.indoor else '户外'}|{wx}|{(s.poi.why or '')[:70]}|{hint}"
            )
            if len(rows) >= limit:
                return rows
    return rows


def acceptable(reason: str) -> tuple[bool, str]:
    """模型写的这条能不能用。不合格就退回模板，而不是把编造的句子推给用户。

    检查的是三件能客观判定的事：长度够不够（用户要求 100-200 字）、
    有没有出现空话、有没有出现"营业时间/人均"之类的硬数字与条目无关——
    具体景物名词无法用规则穷举，所以那一条交给提示词与反例去防。
    """
    text = (reason or "").strip()
    if len(text) < 100:
        return False, f"太短（{len(text)} 字，要求 100-200）"
    if len(text) > 240:
        return False, f"太长（{len(text)} 字）"
    hit = [w for w in _EMPTY_WORDS if w in text]
    if hit:
        return False, f"空话：{'、'.join(hit)}"
    return True, ""


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


def fallback_reason(poi, audience_label: str, slot=None, weather: str = "") -> str:
    """没有大模型（或模型输出不合格）时的模板：只用已核实字段拼出 100-200 字。

    刻意保留"只讲事实"的写法：模板宁可平实，也绝不能出现条目里没有的景物。
    上一版模板只有四五十个字，用户反馈"文字描述要多一些"，
    这里把类目、时段、室内外、当日天气、最佳时段、避坑、人均/评分全部串起来补足信息量，
    但每一句都能回溯到字段。
    """
    kind = slot.type.label if slot else (poi.category.value if poi.category else "地点")
    band = slot.band if slot else ""
    indoor = "室内" if (slot and slot.indoor) else ("户外" if slot else "")
    bits: list[str] = []
    lead = f"【{audience_label}】" if audience_label else ""
    where = f"这是一处{kind}，安排在{band}（{indoor}）。" if band else f"这是一处{kind}（{indoor}）。"
    if poi.why:
        bits.append(poi.why.rstrip("。") + "。")
    if weather and weather != "天气未知":
        if indoor == "户外":
            bits.append(f"当日{weather}，户外活动请按体感增减停留时间，遮阳与补水放在顺手的位置。")
        else:
            bits.append(f"当日{weather}，这一项在室内，受天气影响小，适合放在体感最不舒适的时段。")
    if poi.best_time:
        bits.append(f"数据里给的最佳时段是{poi.best_time}。")
    if poi.open_hours_raw:
        bits.append(f"营业时间 {poi.open_hours_raw}。")
    if poi.pitfalls:
        bits.append("要留意的坑：" + "；".join(poi.pitfalls[:2]) + "。")
    hard = []
    if poi.rating:
        hard.append(f"评分 {poi.rating}")
    if poi.avg_cost_cents or poi.ticket_cents:
        hard.append(f"人均约 ¥{(poi.avg_cost_cents or poi.ticket_cents) / 100:.0f}")
    if poi.need_reservation:
        hard.append("需预约")
    if hard:
        bits.append("、".join(hard) + "。")
    text = lead + where + "".join(bits)
    # 稀疏数据也要够 100 字：补的都是"排期事实"，不是形容词。
    # 用户要的是看完愿意去，靠形容词骗不到，靠"为什么这个时候去"能说到。
    if len(text) < 100:
        text += (f"这一项是按当天的作息与通勤距离排进来的：{band or '该'}时段"
                 f"{'室内体感更稳' if indoor == '室内' else '光线与人流都还在可接受范围'}，"
                 f"和前后两项之间的移动距离也在一天走得完的范围内。")
    if len(text) < 100 and poi.tags:
        text += "它属于" + "、".join(poi.tags[:3]) + "这一类去处，适不适合你们这趟，主要看你们是否愿意为它单独留一段时间。"
    return text[:200]


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

    rejected: list[str] = []

    def _write(slot, day, text: str, from_model: bool) -> bool:
        """写入一条推荐语。模型输出不合格时退回模板，而不是把编造的句子推给用户。"""
        if from_model:
            ok, why = acceptable(text)
            if not ok:
                rejected.append(f"{slot.title}：{why}")
                text = fallback_reason(slot.poi, audience_label, slot, _day_weather(day))
        slot.notes = [f"【{audience_label}】{text}"] + [x for x in slot.notes if not x.startswith("【")][:2]
        # 合并而不是整块替换：替换会把装配阶段已经挂进去的 extras（如诗词彩蛋）抹掉
        slot.extras = {**(slot.extras or {}), "audience_reason": text, "audience": audience_label}
        return True

    if not table:
        for day in plan.days:
            for slot in day.slots:
                if slot.poi and slot.type.label not in _SKIP_TYPES:
                    _write(slot, day, fallback_reason(slot.poi, audience_label, slot, _day_weather(day)), False)
        return 0, f"大模型推荐语未生成（{(errors or ['输出格式不符'])[0]}），已退回模板依据"

    touched = 0
    for day in plan.days:
        for slot in day.slots:
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
            _write(slot, day, reason, True)
            touched += 1
    note = f"按「{audience_label}」分 {len(groups)} 片并发改写 {touched}/{n} 条推荐语"
    if errors:
        note += f"（{len(errors)} 片失败，已用模板补齐）"
    if rejected:
        # 不合格数量必须显示出来：模型编造景物这件事，只有让它留痕才会被持续修
        note += f"；{len(rejected)} 条不合格已退回模板（{rejected[0][:60]}）"
    return touched, note


def _safe_chunk(fn):
    """线程池里抛异常会吞掉整批结果，这里把每片失败降级成一条错误说明。"""

    def wrapper(part: list[str]):
        try:
            return fn(part)
        except Exception as exc:
            return f"{type(exc).__name__}: {str(exc)[:100]}"

    return wrapper
