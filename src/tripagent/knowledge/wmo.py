"""WMO 天气码 → 中文语义。

Open-Meteo / CMA 数值接口只给数字码，中文文案必须自己映射，
同时映射「灾害属性」，供天气风险规则引擎判断该不该建议改期或转室内。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WMOInfo:
    zh: str
    en: str
    icon: str
    wet: bool = False
    storm: bool = False
    snow: bool = False
    fog: bool = False


_TABLE: dict[int, WMOInfo] = {
    0: WMOInfo("晴", "Clear", "☀️"),
    1: WMOInfo("基本晴朗", "Mainly Clear", "🌤️"),
    2: WMOInfo("局部多云", "Partly Cloudy", "⛅"),
    3: WMOInfo("阴", "Overcast", "☁️"),
    45: WMOInfo("雾", "Fog", "🌫️", fog=True),
    48: WMOInfo("雾凇雾", "Depositing Rime Fog", "🌫️", fog=True),
    51: WMOInfo("小毛毛雨", "Light Drizzle", "🌦️", wet=True),
    53: WMOInfo("中毛毛雨", "Drizzle", "🌦️", wet=True),
    55: WMOInfo("大毛毛雨", "Dense Drizzle", "🌧️", wet=True),
    56: WMOInfo("冻毛毛雨", "Freezing Drizzle", "🌧️", wet=True),
    57: WMOInfo("强冻毛毛雨", "Heavy Freezing Drizzle", "🌧️", wet=True),
    61: WMOInfo("小雨", "Light Rain", "🌦️", wet=True),
    63: WMOInfo("中雨", "Rain", "🌧️", wet=True),
    65: WMOInfo("大雨", "Heavy Rain", "🌧️", wet=True),
    66: WMOInfo("冻雨", "Freezing Rain", "🧊", wet=True),
    67: WMOInfo("强冻雨", "Heavy Freezing Rain", "🧊", wet=True),
    71: WMOInfo("小雪", "Light Snow", "🌨️", wet=True, snow=True),
    73: WMOInfo("中雪", "Snow", "❄️", wet=True, snow=True),
    75: WMOInfo("大雪", "Heavy Snow", "❄️", wet=True, snow=True),
    77: WMOInfo("雪粒", "Snow Grains", "🌨️", snow=True),
    80: WMOInfo("小阵雨", "Light Showers", "🌦️", wet=True),
    81: WMOInfo("中阵雨", "Showers", "🌧️", wet=True),
    82: WMOInfo("强阵雨", "Heavy Showers", "🌧️", wet=True),
    85: WMOInfo("小阵雪", "Light Snow Showers", "🌨️", wet=True, snow=True),
    86: WMOInfo("大阵雪", "Heavy Snow Showers", "❄️", wet=True, snow=True),
    95: WMOInfo("雷暴", "Thunderstorm", "⛈️", wet=True, storm=True),
    96: WMOInfo("雷暴伴小冰雹", "Thunderstorm with Hail", "⛈️", wet=True, storm=True),
    99: WMOInfo("雷暴伴大冰雹", "Severe Thunderstorm", "⛈️", wet=True, storm=True),
}

_UNKNOWN = WMOInfo("未知", "Unknown", "🌡️")


def describe(code: int | None) -> WMOInfo:
    if code is None:
        return _UNKNOWN
    return _TABLE.get(int(code), _UNKNOWN)


def known_codes() -> list[int]:
    return sorted(_TABLE)
