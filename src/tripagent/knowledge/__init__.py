"""静态知识表：城市坐标/adcode、WMO 天气码、高德 POI 分类编码、类目与兴趣映射。"""

from __future__ import annotations

from .cities import CityInfo, all_cities, get_city, resolve_city
from .poi_codes import (
    AMAP_CATEGORY,
    DAY_LONG_CATEGORIES,
    INTEREST_TO_CATEGORIES,
    MEAL_CATEGORIES,
    NIGHT_CATEGORIES,
    categories_for,
    match_category,
)
from .wmo import describe as describe_wmo

__all__ = [
    "AMAP_CATEGORY",
    "CityInfo",
    "DAY_LONG_CATEGORIES",
    "INTEREST_TO_CATEGORIES",
    "MEAL_CATEGORIES",
    "NIGHT_CATEGORIES",
    "all_cities",
    "categories_for",
    "describe_wmo",
    "get_city",
    "match_category",
    "resolve_city",
]
