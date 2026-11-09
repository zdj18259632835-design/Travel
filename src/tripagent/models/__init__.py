"""数据模型统一出口。引擎与 UI 只从这里 import，避免路径散落。"""

from __future__ import annotations

from .common import GeoPoint, cents_to_yuan, estimate_minutes, fmt_cny, hhmm_to_minute, minute_to_hhmm, to_cents
from .enums import Category, ConstraintKind, Crowd, Interest, Severity, SlotType
from .evidence import EvidenceRecord
from .poi import POIQuery, POIRecord, RouteLeg
from .plan import DayPlan, IntercityLeg, Slot, TripPlan
from .request import Party, TripRequest
from .validation import ConstraintCheck, ValidationReport
from .weather import AlarmRecord, CityWeather, DayForecast, RiskFactor, WeatherRisk

__all__ = [
    "AlarmRecord",
    "Category",
    "CityWeather",
    "ConstraintCheck",
    "ConstraintKind",
    "Crowd",
    "DayForecast",
    "DayPlan",
    "EvidenceRecord",
    "GeoPoint",
    "IntercityLeg",
    "Interest",
    "POIQuery",
    "POIRecord",
    "Party",
    "RiskFactor",
    "RouteLeg",
    "Severity",
    "Slot",
    "SlotType",
    "TripPlan",
    "TripRequest",
    "ValidationReport",
    "WeatherRisk",
    "cents_to_yuan",
    "estimate_minutes",
    "fmt_cny",
    "hhmm_to_minute",
    "minute_to_hhmm",
    "to_cents",
]
