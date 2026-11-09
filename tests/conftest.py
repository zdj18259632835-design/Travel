r"""测试公共夹具：全部离线、全部确定性、不碰网络。

跑法（项目根目录）：
    .venv\Scripts\python.exe -m pytest
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ["TRIPAGENT_OFFLINE"] = "1"       # 强制本地数据源，CI/断网都能跑
os.environ["DASHSCOPE_API_KEY"] = ""
os.environ["AMAP_KEY"] = ""
os.environ["QWEATHER_KEY"] = ""

from tripagent.config import reload_settings  # noqa: E402
from tripagent.knowledge.cities import get_city  # noqa: E402
from tripagent.models.enums import Category, Interest, Severity  # noqa: E402
from tripagent.models.evidence import EvidenceRecord  # noqa: E402
from tripagent.models.poi import POIRecord  # noqa: E402
from tripagent.models.request import Party, TripRequest  # noqa: E402
from tripagent.models.weather import AlarmRecord, CityWeather, DayForecast  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def settings():
    s = reload_settings()
    assert s.offline, "测试必须跑在离线模式，否则会打网络"
    return s


def make_poi(idx: int, name: str, cat: Category, lat: float, lng: float, **kw) -> POIRecord:
    base = dict(
        id=f"fx:{idx}",
        name=name,
        category=cat,
        city="成都",
        lat=lat,
        lng=lng,
        rating=4.5,
        popularity=0.8,
        stay_minutes=90,
        source="fixture",
        confidence=0.8,
    )
    base.update(kw)
    return POIRecord(**base)


@pytest.fixture
def poi_pool() -> dict[Category, list[POIRecord]]:
    """一条直线上的景点带，便于验证"不折返"。"""
    return {
        Category.SIGHT: [
            make_poi(1, "A景点", Category.SIGHT, 30.570, 104.060, ticket_cents=5000, indoor=False),
            make_poi(2, "B景点", Category.SIGHT, 30.575, 104.065, ticket_cents=3000, indoor=False),
            make_poi(3, "C博物馆", Category.SIGHT, 30.580, 104.070, ticket_cents=0, indoor=True),
            make_poi(4, "D远端景点", Category.SIGHT, 30.700, 104.300, ticket_cents=80000, indoor=False),
        ],
        Category.MUSEUM: [make_poi(5, "E馆", Category.MUSEUM, 30.572, 104.062, indoor=True, kid_friendly=True)],
        Category.FOOD: [
            make_poi(6, "F老火锅", Category.FOOD, 30.573, 104.063, avg_cost_cents=9800),
            make_poi(7, "G苍蝇馆子", Category.FOOD, 30.576, 104.066, avg_cost_cents=3500, tags=["性价比"]),
        ],
        Category.BAR: [make_poi(8, "H清吧", Category.BAR, 30.574, 104.064, avg_cost_cents=8000, requires_id=True,
                               open_from_minute=1200, open_to_minute=120, open_hours_raw="20:00-02:00")],
        Category.AMUSEMENT: [make_poi(9, "I主题乐园", Category.AMUSEMENT, 30.600, 104.100, ticket_cents=29000, stay_minutes=300)],
        Category.HOTEL: [make_poi(10, "J酒店", Category.HOTEL, 30.571, 104.061, avg_cost_cents=35000)],
    }


@pytest.fixture
def city():
    c = get_city("chengdu")
    assert c is not None
    return c


def make_weather(start: date, days: int, rain_day: int | None = None, hot: bool = False) -> CityWeather:
    forecast = []
    for i in range(days):
        d = start + timedelta(days=i)
        rain = i == rain_day
        forecast.append(
            DayForecast(
                date=d,
                wmo_code=65 if rain else (95 if hot and i == 0 else 0),
                text="大雨" if rain else ("雷暴" if hot and i == 0 else "晴"),
                tmax_c=38.0 if hot else 22.0,
                tmin_c=14.0,
                precip_mm=45.0 if rain else 0.0,
                precip_prob=90 if rain else 10,
                wind_max_kmh=20.0,
                humidity_max=70,
                source="fixture",
            )
        )
    return CityWeather(city="成都", city_key="chengdu", lat=30.57, lng=104.06, forecast=forecast)


@pytest.fixture
def request_basic() -> TripRequest:
    return TripRequest(
        raw_text="成都 3天",
        destination="成都",
        city_key="chengdu",
        adcode="510100",
        days=3,
        start_date=date.today() + timedelta(days=3),
        interests=[Interest.FOOD, Interest.NIGHTLIFE],
        nightlife=True,
        party=Party(adults=2),
    )
