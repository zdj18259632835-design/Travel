"""天气与风险模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .enums import Severity


class DayForecast(BaseModel):
    """某一天的预报（半个月窗口的最小单元）。"""

    model_config = ConfigDict(extra="allow")

    date: _date
    wmo_code: int | None = None
    text: str = "未知"
    text_en: str = ""
    tmax_c: float | None = None
    tmin_c: float | None = None
    precip_mm: float = 0.0
    precip_prob: float | None = None
    wind_max_kmh: float | None = None
    humidity_max: float | None = None
    uv_index: float | None = None
    sunrise: str | None = None
    sunset: str | None = None
    source: str = "unknown"

    @property
    def is_wet(self) -> bool:
        return self.precip_mm >= 10 or (self.precip_prob or 0) >= 70

    @property
    def is_rainy_light(self) -> bool:
        return 0.1 <= self.precip_mm < 10

    @property
    def is_snow(self) -> bool:
        return (self.wmo_code or 0) in (71, 73, 75, 77, 85, 86)

    @property
    def is_hot(self) -> bool:
        return (self.tmax_c or -99) >= 35

    @property
    def is_cold(self) -> bool:
        return (self.tmin_c or 99) <= 0


class AlarmRecord(BaseModel):
    """气象预警（台风/暴雨/暴雪/寒潮/高温/大风/雷电/道路结冰等）。"""

    model_config = ConfigDict(extra="allow")

    title: str
    alarm_type: str = ""
    level: str = ""  # 蓝/黄/橙/红
    severity: Severity = Severity.WATCH
    scope: str = ""  # city / province，标明这条预警是直接命中还是省级波及
    issuer: str = ""
    issued_at: str = ""
    area: str = ""
    text: str = ""
    url: str = ""
    source: str = "unknown"

    @property
    def is_critical(self) -> bool:
        return self.level in ("橙", "红") or self.alarm_type in ("台风", "暴雨", "暴雪")


LEVEL_TO_SEVERITY = {"蓝": Severity.WATCH, "黄": Severity.WATCH, "橙": Severity.WARN, "红": Severity.DANGER}


@dataclass
class RiskFactor:
    """规则引擎的最小结论单元。用 dataclass 是为了能按位置构造，读起来更像一句话。"""

    name: str
    detail: str
    severity: Severity = Severity.WATCH
    recommend_indoor: bool = False


class WeatherRisk(BaseModel):
    """规则引擎输出：这天到底能不能玩、该怎么调整。"""

    model_config = ConfigDict(extra="allow")

    date: _date | None = None
    severity: Severity = Severity.NONE
    factors: list[RiskFactor] = Field(default_factory=list)
    advice: list[str] = Field(default_factory=list)
    prefer_indoor: bool = False
    travel_not_recommended: bool = False

    def add(self, factor: RiskFactor) -> None:
        self.factors.append(factor)
        self.severity = factor.severity if factor.severity.rank > self.severity.rank else self.severity
        self.prefer_indoor = self.prefer_indoor or factor.recommend_indoor


class CityWeather(BaseModel):
    """一座城市未来半个月的全量天气视图。"""

    model_config = ConfigDict(extra="allow")

    city: str
    city_key: str = ""
    lat: float | None = None
    lng: float | None = None
    forecast: list[DayForecast] = Field(default_factory=list)
    alarms: list[AlarmRecord] = Field(default_factory=list)
    risks: dict[str, WeatherRisk] = Field(default_factory=dict)
    overall: Severity = Severity.NONE
    notes: dict[str, Any] = Field(default_factory=dict)

    def on_day(self, d: _date) -> DayForecast | None:
        return next((f for f in self.forecast if f.date == d), None)

    def risk_on(self, d: _date) -> WeatherRisk | None:
        return self.risks.get(d.isoformat())

    @property
    def danger_dates(self) -> list[str]:
        return [k for k, v in self.risks.items() if v.severity.rank >= Severity.WARN.rank]
