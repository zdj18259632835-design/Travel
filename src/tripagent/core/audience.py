"""受众判定：把"和谁一起去"变成可推理的结构化维度。

受众决定两件事：① 推荐哪些文旅娱乐项目；② 推荐理由怎么写
（亲子要写"孩子能收获什么"，情侣要写"怎么增进感情"，而不是同一句"很有特色"）。

推理优先、确认兜底：先从用户原话与同行人群推断，推不出来才在界面上问，
且推断结果始终可见、可覆盖——避免"AI 替用户决定了他是谁"。
"""

from __future__ import annotations

import re
from enum import Enum

from ..models.request import Party


class Audience(str, Enum):
    FAMILY = "亲子带娃"
    COUPLE = "情侣约会"
    ELDER = "长辈同行"
    FRIENDS = "朋友团玩"
    SOLO = "一个人独行"
    BUSINESS = "商务出差"

    @property
    def label(self) -> str:
        return self.value

    @property
    def hint(self) -> str:
        return _HINTS[self]

    @property
    def ask(self) -> str:
        """写理由时必须回答清楚的那个问题——这是个性化的抓手。"""
        return _ASKS[self]


_HINTS = {
    Audience.FAMILY: "节奏要缓、排队要短、有可参与的项目；安全与卫生优先",
    Audience.COUPLE: "有氛围、有共同体验、出片；不要一天赶 6 个点",
    Audience.ELDER: "少台阶少走弯路、有座位可歇、医疗与如厕便利；中午要能回酒店休息",
    Audience.FRIENDS: "夜生活、互动型项目（密室/剧本杀/演出）、可拍照打卡的长队也值",
    Audience.SOLO: "交通直达、安全、适合一人食与自由行的项目；夜归要方便",
    Audience.BUSINESS: "通勤时间与稳定性优先，晚间需要低强度放松",
}

_ASKS = {
    Audience.FAMILY: "孩子在这里能具体收获什么（认知/动手/体力/审美），以及家长省心的点在哪",
    Audience.COUPLE: "这个项目靠什么增进两人关系（共同完成/独处空间/仪式感/新鲜感）",
    Audience.ELDER: "长辈的体力与舒适度如何被照顾（步行距离、台阶、休息点、饮食清淡）",
    Audience.FRIENDS: "一群人一起玩时的氛围点与互动性在哪，适合几点去",
    Audience.SOLO: "一个人去是否自在（一人食、不尴尬、安全、交通直达）",
    Audience.BUSINESS: "在有限时间里如何最省力地获得休息或体验",
}

_RULES: list[tuple[Audience, str]] = [
    # "带一个小孩""带俩娃""带孩子" 都要命中：量词经常插在中间，
    # 只写 带[小孩] 会漏掉最常见的说法（实测漏过"带一个小孩"）
    (Audience.FAMILY, r"亲子|带\s*(?:[一两几]?\s*(?:个|只)?\s*)?(?:小孩|娃|宝|孩子|儿童)|小朋友|遛娃|宝宝|幼儿园|小学生"),
    (Audience.COUPLE, r"情侣|对象|女朋友|男朋友|老婆|老公|蜜月|约会|表白|求婚|两个人甜|二人世界"),
    (Audience.ELDER, r"父母|爸妈|长辈|老人|老年|带老人|爷爷|奶奶|外公|外婆"),
    (Audience.FRIENDS, r"朋友|同学|闺蜜|兄弟|组团|几个人一起|毕业旅行|社团"),
    (Audience.SOLO, r"一个人|独自|单人|自己一?个|独行|散心"),
    (Audience.BUSINESS, r"出差|公务|会议|培训|办事|考察"),
]


def detect_audience(text: str, party: Party | None = None) -> tuple[Audience | None, str]:
    """返回 (受众, 依据)。依据为空表示没推断出来，界面就该问。"""
    low = (text or "").lower()
    for audience, pattern in _RULES:
        m = re.search(pattern, low)
        if m:
            return audience, f"原话「{m.group(0)}」"
    if party:
        if party.kids:
            return Audience.FAMILY, f"同行 {party.kids} 名儿童"
        if party.elders:
            return Audience.ELDER, f"同行 {party.elders} 位长辈"
        if party.head_count == 1:
            return Audience.SOLO, "只有 1 人同行"
        if party.head_count >= 4:
            return Audience.FRIENDS, f"{party.head_count} 人同行"
    return None, ""
