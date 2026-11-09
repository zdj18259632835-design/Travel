"""用户请求模型：一句自然语言 → 结构化诉求。"""

from __future__ import annotations

from datetime import date as _date

from pydantic import BaseModel, ConfigDict, Field

from .common import to_cents
from .enums import Interest


class Party(BaseModel):
    model_config = ConfigDict(extra="allow")

    adults: int = 2
    kids: int = 0
    elders: int = 0
    pets: int = 0
    accessible: bool = False

    @property
    def head_count(self) -> int:
        return max(1, self.adults + self.kids + self.elders)

    @property
    def room_count(self) -> int:
        """住宿按「间」计价，不是按「人」：4 个人是 2 间房，不是 4 间。
        按人头乘会把预算额度瞬间吃光，行程就排不出东西了。"""
        import math

        return max(1, math.ceil(self.head_count / 2))

    @property
    def needs_care(self) -> bool:
        """需要放慢节奏/规避高门槛场所的人群组合。"""
        return self.kids > 0 or self.elders > 0 or self.accessible


class TripRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    raw_text: str = ""
    destination: str = ""
    city_key: str = ""
    adcode: str = ""
    origin_city: str = ""
    days: int = 3
    start_date: _date | None = None
    budget_cents: int = 0
    party: Party = Field(default_factory=Party)
    interests: list[Interest] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    must_visit: list[str] = Field(default_factory=list)
    pace: str = "normal"          # relaxed / normal / packed（特种兵）
    nightlife: bool = False
    lodging_area: str = ""        # 期望住宿商圈，用于夜间回酒店可行性判断
    lodging_persona: str = "综合"   # 住宿偏好人群：见 core/lodging.py LodgingPersona
    lodging_reason: str = ""       # 该人群是怎么定下来的（原话/同行人数），保证可解释
    audience: str = ""             # 文旅推荐的受众：见 core/audience.py Audience
    audience_reason: str = ""      # 受众是怎么定下来的（原话/同行人数）
    lodging_max_nightly_cents: int = 0  # 每晚每间上限，0=按总预算推算
    notes: str = ""

    @property
    def budget_yuan(self) -> float:
        return self.budget_cents / 100

    def set_budget_yuan(self, yuan: float | int | str | None) -> None:
        self.budget_cents = to_cents(yuan)

    def has_interest(self, *items: Interest) -> bool:
        return any(i in self.interests for i in items)

    @property
    def missing(self) -> list[str]:
        out = []
        if not self.destination:
            out.append("目的城市")
        if not self.start_date:
            out.append("出发日期")
        if not self.interests:
            out.append("兴趣偏好")
        return out

    def describe(self) -> str:
        bits = [f"{self.destination}{self.days}天"]
        if self.start_date:
            bits.append(self.start_date.strftime("%m月%d日出发"))
        if self.budget_cents:
            bits.append(f"预算 ¥{self.budget_yuan:.0f}")
        p = self.party
        if p.kids:
            bits.append(f"带{p.kids}娃")
        if p.elders:
            bits.append(f"含{p.elders}位长辈")
        if p.pets:
            bits.append("携宠")
        if self.interests:
            bits.append("偏好：" + "、".join(i.label for i in self.interests))
        if self.nightlife:
            bits.append("含夜生活")
        return " · ".join(bits)
