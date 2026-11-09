"""意图解析：一句中文自由文本 → TripRequest。

为什么先用规则而不是大模型：解析必须可预测、可单测、零成本、离线可用；
LLM 只在规则拿不准时做补充（agents/service.py 的 llm_refine_request）。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from ..knowledge.cities import resolve_city
from ..knowledge.poi_codes import CATEGORY_ALIASES, INTEREST_ALIASES
from ..models.common import to_cents
from ..models.enums import Category, Interest
from ..models.request import Party, TripRequest

_CN_NUM = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10,
}
_HOLIDAYS = {
    "元旦": (1, 1), "春节": (2, 17), "清明": (4, 5), "劳动节": (5, 1),
    "端午": (6, 19), "中秋": (10, 6), "国庆": (10, 1), "暑假": (7, 15), "寒假": (1, 20),
}
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_DATE_SPAN = re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日号]?|\d{1,2}\s*月\s*\d{1,2}[日号]?")
_NIGHT_KEYS = ("夜生活", "酒吧", "ktv", "蹦迪", "夜店", "livehouse", "清吧", "卡拉ok", "唱k", "夜市")


def _cn_int(token: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    if len(token) == 1:
        return _CN_NUM.get(token)
    if token.startswith("十") and len(token) == 2:
        v = _CN_NUM.get(token[1])
        return 10 + v if v else None
    if len(token) == 2 and token[1] == "十":
        v = _CN_NUM.get(token[0])
        return 10 * v if v else None
    return None


def parse_days(text: str) -> int | None:
    stripped = _DATE_SPAN.sub(" ", text or "")  # 先抹掉日期，避免「11月20日」被读成 20 天
    m = re.search(r"(\d+|[一二两三四五六七八九十])\s*(?:天|日|晚|days?|nights?)", stripped, re.I)
    if not m:
        return None
    v = _cn_int(m.group(1))
    return v if v and 1 <= v <= 30 else None


def _money(num: str, unit: str) -> int:
    v = float(num)
    if unit in ("k", "K", "千"):
        v *= 1000
    elif unit in ("w", "W", "万"):
        v *= 10000
    return to_cents(v)


def parse_budget(text: str) -> tuple[int | None, bool]:
    """返回 (总额分, 是否人均)。"""
    low = text or ""
    per_head = bool(re.search(r"人均|每人|每个人", low))
    m = re.search(r"(?:人均|每人|每个人)\s*(?:预算)?\s*(\d+(?:\.\d+)?)\s*(k|K|千|w|W|万|元|块)?", low)
    if m:
        return _money(m.group(1), m.group(2) or ""), True
    m = re.search(r"(?:预算|费用|花费|一共|总共|控制在)\D{0,3}(\d+(?:\.\d+)?)\s*(k|K|千|w|W|万|元|块|rmb)?", low, re.I)
    if m:
        return _money(m.group(1), m.group(2) or ""), per_head
    m = re.search(r"(\d{3,6})\s*(?:元|块|块钱|rmb|¥|￥)", low, re.I)
    if m:
        return _money(m.group(1), ""), False
    return None, False


def parse_date(text: str, today: date | None = None) -> date | None:
    today = today or date.today()
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})[日号]?", text)
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        return d if d >= today else date(today.year + 1, d.month, d.day)
    if "后天" in text:
        return today + timedelta(days=2)
    if "明天" in text or "明日" in text:
        return today + timedelta(days=1)
    if "今天" in text or "今晚" in text:
        return today
    for name, (mo, dd) in _HOLIDAYS.items():
        if name in text:
            try:
                d = date(today.year, mo, dd)
            except ValueError:
                continue
            return d if d >= today else date(today.year + 1, mo, dd)
    m = re.search(r"(本|下|这)?(?:周|星期|礼拜)([一二三四五六日天])", text)
    if m:
        target = _WEEKDAY[m.group(2)]
        delta = (target - today.weekday()) % 7
        if m.group(1) == "下":
            delta += 7
        elif delta == 0:
            delta = 7
        return today + timedelta(days=delta)
    if "周末" in text:
        return today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    return None


def parse_party(text: str) -> Party:
    p = Party(adults=2, kids=0, elders=0)
    low = text or ""
    m = re.search(r"(\d+|[一二两三四五六七八九十])\s*(?:个|位|名)?\s*(?:朋友|人|同伴|同学|伙伴|同行)", low)
    if m:
        v = _cn_int(m.group(1))
        if v:
            p.adults = max(1, min(v, 20))
    if re.search(r"一个人|独自|单人|自己一个|独行", low):
        p.adults = 1
    if re.search(r"亲子|带[小孩娃宝]|儿童|遛娃|宝宝|小朋友", low):
        p.kids = max(1, p.kids)
    if re.search(r"父母|爸妈|长辈|老人|老年|带老人", low):
        p.elders = max(2, p.elders)
    if re.search(r"宠物|带狗|带猫|携宠|遛狗|毛孩子|狗狗|猫咪|[一两几]只\s*(?:狗|猫|犬)", low):
        p.pets = 1
    if re.search(r"轮椅|行动不便|无障碍|腿脚", low):
        p.accessible = True
    if re.search(r"情侣|对象|女朋友|男朋友|老婆|老公|蜜月", low):
        p.adults = max(2, p.adults)
    return p


def parse_interests(text: str) -> list[Interest]:
    found: list[Interest] = []
    low = (text or "").lower()
    for alias, interest in sorted(INTEREST_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if len(alias) >= 2 and alias in low and interest not in found:
            found.append(interest)
    if _wants_nightlife(low) and Interest.NIGHTLIFE not in found:
        found.append(Interest.NIGHTLIFE)
    if re.search(r"穷游|省钱|性价比|平价|便宜", low) and Interest.BUDGET not in found:
        found.append(Interest.BUDGET)
    return found


def _wants_nightlife(text: str) -> bool:
    low = (text or "").lower()
    if any(k in low for k in ("不要夜生活", "不熬夜", "早点回酒店", "不蹦迪")):
        return False
    return any(k in low for k in _NIGHT_KEYS)


def parse_must_visit(text: str) -> list[str]:
    out: list[str] = []
    # (?<!不) 很关键：否则「不想去宽窄巷子」会被当成"想去宽窄巷子"
    for m in re.finditer(r"(?:一定要去|必须去|(?<!不)想去|不能错过|必去)\s*([^\s，,。；;、]{2,12})", text or ""):
        token = m.group(1).strip("的了吗呢啊")
        if not token or token in out:
            continue
        hit = resolve_city(token)
        # 只有"整个词就是一个行政区"才算城市；武侯祠含「武侯」、
        # 大唐不夜城含「大唐」这类景点名不能被误杀
        if hit and hit.name == token:
            continue
        out.append(token)
    return out[:6]


def parse_avoid(text: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"(?:不想|不喜欢|不爱|别安排|不要|拒绝)\s*([^\s，,。；;]{1,10})", text or ""):
        token = m.group(1).strip("的了吗呢啊去")
        if token and token not in out:
            out.append(token)
    return out[:6]


def parse_pace(text: str) -> str:
    low = text or ""
    if re.search(r"特种兵|赶一赶|紧凑|排满|高效", low):
        return "packed"
    if re.search(r"休闲|慢游|轻松|度假|躺平|不赶|随意", low):
        return "relaxed"
    return "normal"


def parse_origin(text: str) -> str:
    m = re.search(r"(?:从|自)\s*([\u4e00-\u9fa5A-Za-z]{2,8})(?:出发|飞|坐高铁|来|到)", text or "")
    return m.group(1) if m else ""


def parse_lodging_area(text: str) -> str:
    m = re.search(r"(?:住|入住)\s*(?:在|靠近)?\s*([\u4e00-\u9fa5A-Za-z]{2,10})(?:附近|一带|商圈)?", text or "")
    if not m:
        return ""
    token = m.group(1)
    return "" if any(k in token for k in ("酒店", "民宿", "青旅", "谢谢")) else token


def parse(text: str, today: date | None = None) -> TripRequest:
    """把用户原话解析成结构化请求；认不出的部分留在 raw_text 里由 LLM 兜底。"""
    text = (text or "").strip()
    city = resolve_city(text)
    budget, per_head = parse_budget(text)
    party = parse_party(text)
    req = TripRequest(
        raw_text=text,
        destination=city.name if city else "",
        city_key=city.key if city else "",
        adcode=city.adcode if city else "",
        origin_city=parse_origin(text),
        days=parse_days(text) or 3,
        start_date=parse_date(text, today),
        party=party,
        nightlife=_wants_nightlife(text),
        pace=parse_pace(text),
        must_visit=parse_must_visit(text),
        avoid=parse_avoid(text),
        lodging_area=parse_lodging_area(text),
    )
    req.interests = parse_interests(text)
    if budget:
        yuan = budget / 100
        req.set_budget_yuan(yuan * party.head_count if per_head else yuan)
    return req


def city_candidates(text: str) -> list[str]:
    """静态表没命中时，给出可能是城市名的候选词，交给动态地理编码去确认。

    只切词、只过滤，不做判断——判断权在 providers.geo.resolve_any（会真的问高德），
    这样 intent 层保持纯离线、可单测。
    """
    low = re.sub(r"\d+", " ", text or "")
    parts = re.split(r"[\s,，。；;、！!？?]+", low)
    noise = ("出发", "游玩", "旅游", "攻略", "计划", "预算", "人均", "天数", "左右", "大概", "谢谢", "帮我", "安排", "推荐", "想去", "玩")
    out: list[str] = []
    for token in parts:
        for piece in re.findall(r"[\u4e00-\u9fa5]{2,6}", token):
            cand = re.sub(r"(?:天|日|晚|月份?|号|周边|附近|以及|和|跟)", "", piece)
            # 只剥「市/县/区」，绝不能剥「州」——否则潮州变潮、广州变广，全部解析失败
            cand = re.sub(r"(?:市|县|区)$", "", cand) or cand
            if len(cand) < 2 or cand in out or any(n in cand for n in noise):
                continue
            out.append(cand)
    return out[:5]


def detect_query(text: str) -> tuple[str | None, Category | None, dict[str, str]]:
    """娱乐问答入口：「成都高新区评分高、性价比好的酒吧」→ (城市, 类目, 排序偏好)。"""
    low = (text or "").lower()
    city = resolve_city(text or "")
    category: Category | None = None
    for alias, cat in sorted(CATEGORY_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if len(alias) >= 2 and alias in low:
            category = cat
            break
    prefs = {
        "cheap": bool(re.search(r"性价比|便宜|平价|不贵|实惠", low)),
        "high_rating": bool(re.search(r"评分|口碑|好评|排名|最好|人气|高分", low)),
        "quiet": bool(re.search(r"安静|清吧|小酒馆|聊天|不吵", low)),
        "lively": bool(re.search(r"热闹|蹦迪|嗨|夜店|人多|氛围", low)),
        "near_hotel": bool(re.search(r"附近|周边|步行|离酒店|住宿附近", low)),
        "open_late": bool(re.search(r"深夜|通宵|凌晨|营业到|24小时|不打烊", low)),
        "kid": bool(re.search(r"亲[子子]|儿童|小朋友|带娃", low)),
    }
    return (city.name if city else None), category, {k: v for k, v in prefs.items() if v}
