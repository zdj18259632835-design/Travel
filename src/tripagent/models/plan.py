"""行程产物模型：TripPlan 是本项目对外的唯一契约，UI/导出/API 都只读它。"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .common import fmt_cny, minute_to_hhmm, time_band
from .enums import Category, SlotType
from .poi import POIRecord, RouteLeg
from .request import TripRequest
from .validation import ValidationReport
from .weather import DayForecast, WeatherRisk


class Slot(BaseModel):
    model_config = ConfigDict(extra="allow")

    seq: int = 0
    type: SlotType = SlotType.SIGHT
    title: str = ""
    start_minute: int = 0
    end_minute: int = 0
    poi: POIRecord | None = None
    cost_cents: int = 0
    transit_before: RouteLeg | None = None
    indoor: bool = False
    notes: list[str] = Field(default_factory=list)
    reservation_hint: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    flexible: bool = False   # 可被天气规则自动替换的时段
    extras: dict[str, Any] = Field(default_factory=dict)  # 受众推荐语等扩展信息

    @property
    def duration_min(self) -> int:
        return max(0, self.end_minute - self.start_minute)

    @property
    def time_text(self) -> str:
        return f"{minute_to_hhmm(self.start_minute)}-{minute_to_hhmm(self.end_minute)}"

    @property
    def band(self) -> str:
        """对外展示用时段词，不用钟点。"""
        return time_band(self.start_minute)

    @property
    def key(self) -> str:
        return self.poi.id if self.poi else self.title

    def one_line(self) -> str:
        extra = []
        if self.cost_cents:
            extra.append(fmt_cny(self.cost_cents))
        if self.transit_before and self.transit_before.minutes:
            extra.append(f"路上{self.transit_before.minutes}分")
        tail = f"（{'，'.join(extra)}）" if extra else ""
        return f"{self.time_text} {self.title}{tail}"


class DayPlan(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int = 1
    date: _date | None = None
    city: str = ""
    theme: str = ""
    weather: DayForecast | None = None
    risk: WeatherRisk | None = None
    slots: list[Slot] = Field(default_factory=list)
    lodging: POIRecord | None = None
    total_cost_cents: int = 0
    total_transit_minutes: int = 0
    notes: list[str] = Field(default_factory=list)

    @property
    def sight_count(self) -> int:
        return sum(1 for s in self.slots if s.type in (SlotType.SIGHT, SlotType.SHOW, SlotType.NIGHTLIFE))

    @property
    def day_label(self) -> str:
        d = self.date.strftime("%m/%d") if self.date else f"Day{self.index}"
        return f"Day {self.index} · {d}"

    def by_band(self) -> list[tuple[str, list[Slot]]]:
        """按时段聚合当天的安排，保持时间先后顺序。"""
        groups: list[tuple[str, list[Slot]]] = []
        for s in self.slots:
            if groups and groups[-1][0] == s.band:
                groups[-1][1].append(s)
            else:
                groups.append((s.band, [s]))
        return groups

    @property
    def weather_brief(self) -> str:
        if not self.weather:
            return "天气未知"
        w = self.weather
        span = ""
        if w.tmax_c is not None and w.tmin_c is not None:
            span = f" {w.tmin_c:.0f}~{w.tmax_c:.0f}℃"
        rain = f"，降水 {w.precip_mm:.0f}mm" if w.precip_mm else ""
        return f"{w.text}{span}{rain}"

    def recompute_totals(self) -> None:
        self.total_cost_cents = sum(s.cost_cents for s in self.slots)
        self.total_transit_minutes = sum(
            s.transit_before.minutes for s in self.slots if s.transit_before
        )


class IntercityLeg(BaseModel):
    """跨城段。当前用 fixture + 引导跳转 12306/航旅纵横，不做爬取。"""

    model_config = ConfigDict(extra="allow")

    mode: str = "train"
    code: str = ""
    from_city: str = ""
    to_city: str = ""
    dep_time: str = ""
    arr_time: str = ""
    cost_cents: int = 0
    note: str = ""
    source: str = "fixture"


class TripPlan(BaseModel):
    model_config = ConfigDict(extra="allow")

    trip_id: str = ""
    request: TripRequest
    days: list[DayPlan] = Field(default_factory=list)
    intercity: list[IntercityLeg] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    sources_used: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocking_advice: str = ""        # 非空即代表"不建议出行"
    narrative: str = ""              # LLM 生成的整体点评/亮点
    packing: list[str] = Field(default_factory=list)
    budget_breakdown: dict[str, int] = Field(default_factory=dict)
    scorecard: ValidationReport | None = None
    plan_version: int = 1
    created_at: datetime = Field(default_factory=datetime.now)
    extras: dict[str, Any] = Field(default_factory=dict)

    @property
    def total_cost_cents(self) -> int:
        """预算口径：分项汇总优先。

        住宿按「整程晚数」计，放在 budget_breakdown 里而不是逐日 slot，
        否则逐日累加会漏掉后几晚，导致预算校验虚高通过。
        """
        legs = sum(i.cost_cents for i in self.intercity)
        if self.budget_breakdown:
            return sum(self.budget_breakdown.values()) + legs
        return sum(d.total_cost_cents for d in self.days) + legs

    @property
    def all_slots(self) -> list[Slot]:
        return [s for d in self.days for s in d.slots]

    def slot_by_key(self, key: str) -> tuple[DayPlan, Slot] | None:
        for d in self.days:
            for s in d.slots:
                if s.key == key:
                    return d, s
        return None

    def categories_used(self) -> list[Category]:
        out = []
        for s in self.all_slots:
            if s.poi and s.poi.category not in out:
                out.append(s.poi.category)
        return out
