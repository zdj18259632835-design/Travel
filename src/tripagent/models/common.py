"""金额、地理、时间三类基础工具。

金额一律用「分」为整数存储（借鉴 TREK：浮点累加会让预算校验出现 0.01 级别抖动）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def to_cents(yuan: float | int | str | None) -> int:
    if yuan in (None, ""):
        return 0
    return int(round(float(yuan) * 100))


def cents_to_yuan(cents: int | None) -> float:
    return round((cents or 0) / 100, 2)


def fmt_cny(cents: int | None) -> str:
    cents = cents or 0
    if cents % 100 == 0:
        return f"¥{cents // 100}"
    return f"¥{cents / 100:.2f}"


def minute_to_hhmm(minute: int) -> str:
    minute = max(0, min(minute, 24 * 60))
    return f"{minute // 60:02d}:{minute % 60:02d}"


def hhmm_to_minute(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    parts = text.replace("：", ":").split(":")
    try:
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None
    return h * 60 + m


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lng: float

    def distance_m_to(self, other: "GeoPoint") -> float:
        """Haversine 直线距离（米）。有高德 key 时会换成真实路径距离。"""
        r = 6_371_000.0
        p1, p2 = math.radians(self.lat), math.radians(other.lat)
        dp = p2 - p1
        dl = math.radians(other.lng - self.lng)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(math.sqrt(a))

    def walk_minutes(self) -> int:
        return 0

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.lng:.6f},{self.lat:.6f}"


def estimate_minutes(distance_m: float, mode: str = "transit") -> int:
    """无路径 API 时的保守估算：步行 4.5km/h，公交含换乘 20km/h，驾车 25km/h。"""
    speed_kmh = {"walk": 4.5, "transit": 18.0, "driving": 25.0, "bicycling": 12.0}.get(mode, 18.0)
    base = {"walk": 0, "transit": 8, "driving": 5, "bicycling": 2}.get(mode, 8)
    return int(math.ceil(distance_m / 1000 / speed_kmh * 60)) + base


BANDS = (
    (5 * 60, "深夜"),
    (7 * 60, "清晨"),
    (9 * 60, "早上"),
    (12 * 60, "上午"),
    (14 * 60, "中午"),
    (17 * 60 + 30, "下午"),
    (19 * 60 + 30, "傍晚"),
    (23 * 60, "晚上"),
    (24 * 60 + 1, "深夜"),
)


def time_band(minute: int) -> str:
    """把精确分钟换成时段词。

    刻意不显示 09:00-11:00 这种钟点：具体到分钟的行程会让人觉得在完成任务，
    而旅游是要放松的。时段既保留了先后顺序，又留出弹性。
    """
    minute = max(0, min(int(minute), 24 * 60))
    for edge, name in BANDS:
        if minute < edge:
            return name
    return "深夜"


def precip_level(mm: float | None, prob: float | None = None, text: str = "") -> str:
    """降水量换成可读程度：数字对用户没有决策价值，"要不要带伞"才有。

    text 是兜底依据：和风等源只在文字里写"小雨"，数值字段给 0。
    以前这种情况会渲染成「小雨 15~20℃ · 无雨」，用户一眼就看穿我们在糊弄。
    """
    mm = mm or 0.0
    if mm >= 50:
        return "暴雨"
    if mm >= 25:
        return "大雨"
    if mm >= 10:
        return "中雨"
    if mm >= 0.1:
        return "小雨"
    if prob and prob >= 60:
        return "可能下雨"
    if any(k in (text or "") for k in ("雨", "雪", "雹", "阵雨", "雷")):
        return "有雨"
    return "无雨"


def temp_span(tmin: float | None, tmax: float | None) -> str:
    if tmin is None or tmax is None:
        return "气温未知"
    return f"{tmin:.0f}~{tmax:.0f}℃"
