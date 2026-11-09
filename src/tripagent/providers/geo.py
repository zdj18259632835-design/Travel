"""动态城市解析：让「全国任意县市」都能被认出来。

为什么必须做：knowledge/cities.py 的内建坐标表只有 70 多个热门旅游城市，
用户输入「泉州」「潮州」「恩施」之外的冷门县市时，旧逻辑直接报
「城市不在坐标表里」——这对一个号称全国可用的产品是致命体验断点。

主用高德地理编码（/v3/geocode/geo），它同时给出 center 坐标与 adcode，
正好是 Open-Meteo 与高德 POI 两个下游都需要的输入；结果落盘复用，
同一座城市只查一次。
"""

from __future__ import annotations

from ..knowledge.cities import CityInfo, register_dynamic, resolve_city
from .base import Chain, Provider, ProviderError, provider


def resolve_any(name: str, chain: Chain) -> CityInfo | None:
    """静态表 → 高德地理编码 → 落盘复用。

    返回的 CityInfo 一定带 lat/lng/adcode，这是 Open-Meteo（要坐标）与
    高德 POI（要 adcode）两个下游共同的前提。
    """
    needle = (name or "").strip()
    if not needle:
        return None
    hit = resolve_city(needle)
    if hit:
        return hit
    res = chain.run("city", "lookup", needle)
    info = res.value if isinstance(res.value, CityInfo) else None
    if info:
        register_dynamic(info)
    return info


@provider("city", "static")
class StaticCity(Provider):
    """内建表 + data/fixtures/cities_extra.json + 已缓存的动态城市。"""

    def lookup(self, name: str) -> CityInfo | None:
        return resolve_city(name)

    def describe(self) -> tuple[bool, str]:
        return True, "内建坐标表（70+ 热门城市）与本地扩充表"


@provider("city", "amap")
class AMapCity(Provider):
    def available(self) -> bool:
        return bool(self.settings.amap_key) and self.settings.online

    def describe(self) -> tuple[bool, str]:
        if not self.settings.amap_key:
            return False, "未配置 AMAP_KEY，无法动态解析冷门城市"
        return True, "高德地理编码：全国任意市/县/区 → 坐标 + adcode"

    def lookup(self, name: str) -> CityInfo:
        clean = (name or "").strip().rstrip("市区县城")
        if len(clean) < 2:
            raise ProviderError(f"城市名太短：{name!r}")
        data = self.get_json(
            "https://restapi.amap.com/v3/geocode/geo",
            {"key": self.settings.amap_key, "address": clean},
        )
        if str((data or {}).get("status")) != "1":
            raise ProviderError(f"高德地理编码 {data.get('info')}({data.get('infocode')})")
        rows = (data or {}).get("geocodes") or []
        if not rows:
            raise ProviderError(f"地理编码无结果：{name}")
        row = rows[0]
        center = str(row.get("location") or "")
        if "," not in center:
            raise ProviderError(f"地理编码未返回坐标：{name}")
        lng_s, lat_s = center.split(",")[:2]
        adcode = str(row.get("adcode") or "")
        cname = str(row.get("city") or row.get("district") or clean)
        if isinstance(cname, list) or not cname:
            cname = clean
        return CityInfo(
            key=f"c{adcode}" if adcode else f"c{cname}",
            name=str(cname).rstrip("市"),
            adcode=adcode,
            lat=float(lat_s),
            lng=float(lng_s),
            province=str(row.get("province") or "") if isinstance(row.get("province"), str) else "",
            aliases=(str(clean),),
            tags=("动态解析",),
        )
