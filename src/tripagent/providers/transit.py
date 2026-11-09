"""路径/通勤 provider：高德真实路径 + 无 key 时的保守估算。

夜间娱乐的「最后一班车能不能回酒店」是本项目的差异化校验点，
因此这里除了耗时还保留首末班与换乘信息，供 core.rules 判断可达性。
"""

from __future__ import annotations

from typing import Any

from ..knowledge.cities import CityInfo
from ..models.common import GeoPoint
from ..models.poi import RouteLeg
from .base import Provider, ProviderError, provider


@provider("transit", "amap")
class AMapTransit(Provider):
    def available(self) -> bool:
        return bool(self.settings.amap_key)

    def describe(self) -> tuple[bool, str]:
        if not self.settings.amap_key:
            return False, "未配置 AMAP_KEY，将使用直线距离估算通勤时间"
        return True, "高德 v5 步行/公交/驾车路径（真实耗时与票价）"

    def route(self, city: CityInfo, origin: GeoPoint, dest: GeoPoint, mode: str = "transit") -> RouteLeg:
        if mode not in ("walk", "transit", "driving", "bicycling"):
            mode = "transit"
        o = f"{origin.lng:.6f},{origin.lat:.6f}"
        d = f"{dest.lng:.6f},{dest.lat:.6f}"
        api_mode = {"walk": "walking", "transit": "transit/integrated", "driving": "driving", "bicycling": "bicycling"}[mode]
        params: dict[str, Any] = {"key": self.settings.amap_key}
        if mode == "transit":
            params.update({"origin": o, "destination": d, "city1": city.adcode, "city2": city.adcode, "strategy": "0"})
        else:
            params.update({"origin": o, "destination": d})

        def fetch() -> list[RouteLeg]:
            data = self.get_json(f"https://restapi.amap.com/v5/direction/{api_mode}", params)
            if str((data or {}).get("status")) != "1":
                raise ProviderError(f"高德路径 {data.get('info')}({data.get('infocode')})")
            return [_parse_amap_route(data, mode)]

        rows = self.cached_call(
            ("transit", "amap", mode, round(origin.lat, 4), round(origin.lng, 4), round(dest.lat, 4), round(dest.lng, 4)),
            fetch,
            RouteLeg,
        )
        return rows[0] if isinstance(rows, list) and rows else RouteLeg.estimate(origin, dest, mode)


def _parse_amap_route(data: dict, mode: str) -> RouteLeg:
    route = data.get("route") or {}
    if mode == "transit":
        transits = route.get("transit") or {}
        rows = transits.get("transits") if isinstance(transits, dict) else None
        first = (rows or [{}])[0]
        minutes = _to_minutes(first.get("minutes") or first.get("duration"))
        dist = _to_int(first.get("distance"))
        cost = _to_yuan_cents(first.get("fare") or first.get("cost"))
        lines = []
        for seg in first.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            for key in ("bus", "railway"):
                block = seg.get(key) or {}
                for item in (block.get("buslines") or block.get("railwaylines") or []):
                    if isinstance(item, dict) and item.get("name"):
                        lines.append(str(item["name"]))
        return RouteLeg(
            mode="transit",
            minutes=minutes or 25,
            distance_m=dist,
            cost_cents=cost,
            summary="换乘" + " → ".join(lines[:3]) if lines else str(route.get("transit", ""))[:60] or "公交换乘",
            source="amap",
        )
    paths = route.get("paths") or [{}]
    first = paths[0] if isinstance(paths, list) and paths else {}
    cost_block = first.get("cost") or {}
    minutes = _to_minutes(cost_block.get("duration") or first.get("duration"))
    dist = _to_int(cost_block.get("distance") or first.get("distance"))
    return RouteLeg(
        mode=mode,
        minutes=minutes or 20,
        distance_m=dist,
        cost_cents=_to_yuan_cents(cost_block.get("tolls") or 0),
        summary=str(first.get("steps") and f"{len(first['steps'])} 段") or mode,
        source="amap",
    )


def _to_minutes(v: Any) -> int:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    if f <= 0:
        return 0
    # 高德不同版本分别返回分钟或秒，用数量级区分
    return int(f / 60) if f > 480 else int(round(f))


def _to_int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _to_yuan_cents(v: Any) -> int:
    try:
        return int(round(float(v) * 100))
    except (TypeError, ValueError):
        return 0


@provider("transit", "estimated")
class EstimatedTransit(Provider):
    def describe(self) -> tuple[bool, str]:
        return True, "直线距离估算（步行 4.5km/h、公交含换乘 18km/h）"

    def route(self, city: CityInfo, origin: GeoPoint, dest: GeoPoint, mode: str = "transit") -> list[RouteLeg] | RouteLeg:
        return RouteLeg.estimate(origin, dest, mode)
