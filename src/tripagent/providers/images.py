"""活动实拍图：让"推荐"看得见，而不只是一段文字。

为什么图片取高德而不是小红书/抖音：
- 小红书、抖音、大众点评对个人开发者都没有内容开放接口，爬图同时踩 ToS 与版权两条线；
- 高德 POI 图是我们已授权接口返回的公开商户/景点图，合法、稳定，而且和推荐的是同一个地点，
  不会拿一张网图糊弄用户；
- 文旅项目（演出/非遗/夜游）在高德通常也有对应 POI，按名字回查即可拿到同一批图。

图片 URL 会做一次可达性校验：拿不到 200 的直接丢掉。
界面用的是 <img> 标签，破图比没图更难看，而且会让用户怀疑整份方案的可信度。
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from ..knowledge.cities import CityInfo
from .base import Provider, ProviderError, provider
from .poi import amap_photos


def _norm(name: str) -> str:
    return re.sub(r"[\s《》()（）·・\-—_|、,，.。]", "", name or "").lower()


def name_match(query: str, candidate: str) -> bool:
    """名字是否指同一个地方。

    宽松匹配会把"XX 酒吧"配到"XX 酒吧街"的图上，用户看得出来；
    所以只允许互相包含，或前缀重合 4 字以上。
    """
    a, b = _norm(query), _norm(candidate)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    n = min(len(a), len(b))
    return n >= 4 and a[: min(4, n)] == b[: min(4, n)]


def keep_alive(urls: list[str], timeout: float = 3.0) -> list[str]:
    """并发 HEAD 探活，只丢掉明确拿不到的（4xx/5xx）。

    网络异常不算失败：浏览器有缓存、有代理、有时机，服务端一次超时不该替它做决定。
    """
    if not urls:
        return []

    def check(u: str) -> bool:
        try:
            r = httpx.head(u, timeout=timeout, follow_redirects=True,
                           headers={"User-Agent": "Mozilla/5.0 (compatible; TripAgent/0.1)"})
            return r.status_code < 400
        except Exception:
            return True

    with ThreadPoolExecutor(max_workers=min(6, len(urls))) as pool:
        return [u for u, ok in zip(urls, pool.map(check, urls)) if ok]


@provider("images", "amap_search")
class AMapImages(Provider):
    def available(self) -> bool:
        return bool(self.settings.amap_key) and self.settings.online

    def describe(self) -> tuple[bool, str]:
        if not self.settings.amap_key:
            return False, "未配置 AMAP_KEY，界面不显示实拍图"
        return True, "高德 POI 公开实拍图（按地点名回查，可达性校验后展示）"

    def _one(self, city: CityInfo, name: str, per: int) -> list[str]:
        params = {
            "key": self.settings.amap_key,
            "keywords": name,
            "region": city.adcode or city.name,
            "citylimit": "true",
            "page_size": 6,
            "page_num": 1,
            "show_fields": "photos",
        }

        def fetch() -> list[str]:
            data = self.get_json("https://restapi.amap.com/v5/place/text", params)
            if not data:
                raise ProviderError("高德无响应")
            if str(data.get("status")) != "1":
                raise ProviderError(f"高德返回 {data.get('info')}({data.get('infocode')})")
            urls: list[str] = []
            rows = data.get("pois") or []
            hits = [r for r in rows if isinstance(r, dict) and name_match(name, str(r.get("name") or ""))]
            for row in (hits or rows)[:3]:
                urls.extend(amap_photos(row, limit=per))
            return keep_alive(list(dict.fromkeys(urls))[:per])

        return self.cached_call(("images", "amap", city.key, name, per), fetch)

    def fetch_batch(self, city: CityInfo, names: list[str], per: int = 4) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        todo = [n for n in dict.fromkeys(names) if n and n.strip()]
        if not todo:
            return out
        with ThreadPoolExecutor(max_workers=min(6, len(todo))) as pool:
            futures = {pool.submit(self._one, city, n, per): n for n in todo}
            for fut, name in futures.items():
                try:
                    got = fut.result()
                except Exception:
                    got = []  # 单个地点没图不该影响整份行程
                if got:
                    out[name] = got
        return out


@provider("images", "fixture")
class FixtureImages(Provider):
    """离线兜底：本地知识库里若已存过实拍图就直接用，没有就不显示。

    刻意不用占位图：假图比没图更伤信任。
    """

    def fetch_batch(self, city: CityInfo, names: list[str], per: int = 4) -> dict[str, list[str]]:
        path = Path(self.settings.fixture_dir) / f"{city.key}.json"
        if not path.exists():
            return {}
        try:
            doc: Any = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        pool_rows = (doc.get("pois") or []) + (doc.get("culture") or [])
        index: dict[str, list[str]] = {}
        for r in pool_rows:
            if isinstance(r, dict) and r.get("name") and r.get("photos"):
                index[_norm(str(r["name"]))] = [u for u in r["photos"] if isinstance(u, str)][:per]
        out: dict[str, list[str]] = {}
        for name in names:
            got = index.get(_norm(name))
            if not got:
                got = next((v for k, v in index.items() if name_match(name, k)), None)
            if got:
                out[name] = got
        return out
