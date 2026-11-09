"""POI 与路径模型：既能装高德真实返回，也能装本地 fixture 与 LLM 归纳结果。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import GeoPoint, estimate_minutes
from .enums import Category


class POIQuery(BaseModel):
    """一次结构化检索意图。新增数据源只要实现这个输入契约即可。"""

    model_config = ConfigDict(extra="allow")

    city: str
    category: Category | None = None
    keywords: list[str] = Field(default_factory=list)
    near: GeoPoint | None = None
    radius_m: int = 5000
    limit: int = 20
    only_open_now: bool = False
    min_rating: float | None = None
    tags: list[str] = Field(default_factory=list)


class POIRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    category: Category = Category.SIGHT
    city: str = ""
    district: str = ""
    address: str = ""
    lat: float | None = None
    lng: float | None = None

    # 口碑与价格（分）
    rating: float | None = None
    review_count: int | None = None
    avg_cost_cents: int | None = None      # 人均
    ticket_cents: int | None = None        # 门票/单次

    # 时间与可达性
    open_hours_raw: str = ""
    open_from_minute: int | None = None
    open_to_minute: int | None = None
    closed_weekdays: list[int] = Field(default_factory=list)
    stay_minutes: int = 90
    indoor: bool = False
    closed: bool = False      # 暂停营业/歇业/装修中：由名称与标签识别，不参与排程
    metro: str = ""

    # 预约与准入（LifeHack 场景里最能提升体验的一类字段）
    need_reservation: bool = False
    reservation_channel: str = ""
    reservation_lead_days: int | None = None
    reservation_cutoff: str = ""
    age_policy: str = ""
    requires_id: bool = False

    # 适配人群
    kid_friendly: bool | None = None
    pet_friendly: bool | None = None
    accessible: bool | None = None

    # 内容与来源
    tags: list[str] = Field(default_factory=list)
    why: str = ""
    pitfalls: list[str] = Field(default_factory=list)
    photo_spots: list[str] = Field(default_factory=list)
    photos: list[str] = Field(default_factory=list)  # 公开实拍图 URL（高德/文旅），界面做轮播
    best_time: str = ""
    popularity: float = 0.5
    source: str = "fixture"
    external_id: str = ""
    confidence: float = 0.6
    evidence_ids: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _derive_indoor(self) -> "POIRecord":
        """博物馆/KTV/酒吧/剧场等现实中必然在室内：数据源漏填时按类目兜底，
        否则天气引擎会把它们当户外，误判成「恶劣天气日仍排户外」。"""
        if not self.indoor and self.category.inherently_indoor:
            self.indoor = True
        return self

    @property
    def point(self) -> GeoPoint | None:
        if self.lat is None or self.lng is None:
            return None
        return GeoPoint(self.lat, self.lng)

    @property
    def is_night(self) -> bool:
        return self.category.group == "night"

    @property
    def price_cents(self) -> int:
        return self.ticket_cents or self.avg_cost_cents or 0

    def open_covers(self, start_minute: int, duration_min: int) -> bool | None:
        """None = 数据缺失，不做判断（宁可放行也不要误杀）。"""
        if self.open_from_minute is None or self.open_to_minute is None:
            return None
        end = start_minute + duration_min
        lo, hi = self.open_from_minute, self.open_to_minute
        if hi <= lo:  # 跨零点营业（酒吧/夜店常见）
            return start_minute >= lo or end <= hi
        return start_minute >= lo and end <= hi

    def display_price(self) -> str:
        from .common import fmt_cny

        if self.ticket_cents:
            return f"门票 {fmt_cny(self.ticket_cents)}"
        if self.avg_cost_cents:
            return f"人均 {fmt_cny(self.avg_cost_cents)}"
        return "价格待确认"


class RouteLeg(BaseModel):
    model_config = ConfigDict(extra="allow")

    mode: str = "transit"      # walk / transit / driving / bicycling
    minutes: int = 0
    distance_m: int = 0
    cost_cents: int = 0
    summary: str = ""
    source: str = "estimated"

    @classmethod
    def estimate(cls, origin: GeoPoint | None, target: GeoPoint | None, mode: str = "transit") -> "RouteLeg":
        if not origin or not target:
            return cls(mode=mode, minutes=15, summary="坐标缺失，按默认 15 分钟预留", source="fallback")
        d = origin.distance_m_to(target)
        return cls(
            mode=mode,
            minutes=estimate_minutes(d, mode),
            distance_m=int(d),
            summary=f"直线 {d / 1000:.1f}km，按{('步行' if mode == 'walk' else '公交')}估算",
            source="estimated",
        )
