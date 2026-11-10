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
from .base import Chain, Provider, ProviderError, provider
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


def sharpness(data: bytes) -> tuple[int, int, float]:
    """清晰度打分：返回 (宽, 高, 拉普拉斯方差)。

    拉普拉斯方差是"对焦清晰度"的经典无参考指标（image-focus-measure、
    pyimagesearch 的 blur detection 示例、以及大量 GitHub 项目用的都是它）：
    对灰度图做 4 邻域拉普拉斯卷积，边缘越锐利响应越大，方差就越大；
    糊图的梯度被抹平，方差会掉到个位数。

    为什么不用 OpenCV：cv2.Laplacian 在这里没有不可替代性，numpy 切片一行就是同一个核，
    而装 opencv-python 会给创空间镜像多加 60MB+。依赖要算账。

    先等比缩到长边 512 再算，两个原因：不同来源的图分辨率差几十倍，
    不缩放的方差没有可比性；以及大图全量卷积会拖慢取图这一步。
    """
    import io

    import numpy as np
    from PIL import Image

    im = Image.open(io.BytesIO(data)).convert("L")
    w, h = im.size
    if max(w, h) > 512:
        scale = 512.0 / max(w, h)
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)
    a = np.asarray(im, dtype=np.float32)
    if a.shape[0] < 3 or a.shape[1] < 3:
        return w, h, 0.0
    lap = a[2:, 1:-1] + a[:-2, 1:-1] + a[1:-1, 2:] + a[1:-1, :-2] - 4.0 * a[1:-1, 1:-1]
    return w, h, float(lap.var())


def score_url(url: str, timeout: float = 6.0) -> tuple[int, int, float] | None:
    """下载并打分；拿不到字节时返回 None（调用方按"未知"处理，不误杀）。"""
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; TripAgent/0.1)"})
        if r.status_code >= 400 or not r.content:
            return None
        return sharpness(r.content)
    except Exception:
        return None


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


    def quality(self, urls: list[str]) -> dict[str, list]:
        """批量打分：url -> [宽, 高, 拉普拉斯方差]。分数落盘缓存，同图全站只下一次。"""
        todo = [u for u in dict.fromkeys(urls) if u]
        if not todo:
            return {}

        def scored(u: str) -> list:
            def fetch() -> list:
                got = score_url(u)
                return list(got) if got else []
            try:
                return self.cached_call(("imgq", u), fetch) or []
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
            results = list(pool.map(scored, todo))
        return {u: got for u, got in zip(todo, results) if got}


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


def filter_poi_photos(chain: Chain, settings, pois, per_poi: int = 4) -> tuple[int, int]:
    """给一批 POI 筛掉糊图与小图，并把最清晰的一张排到最前。

    返回 (淘汰张数, 受影响地点数)。

    判据是两条硬事实，不是审美：
      短边 < image_min_edge      → 放大到卡片宽度必然发糊，丢；
      拉普拉斯方差 < image_min_lapvar → 梯度被抹平（失焦或压缩糊），丢。
    打分拿不到（离线、超时、防盗链）时**不判死刑**：保留原图，
    因为"我们没量到"和"图确实糊"是两件事，界面不该因为前者变成空白。
    """
    targets = [p for p in pois if getattr(p, "photos", None)]
    if not targets:
        return 0, 0
    urls = [u for p in targets for u in p.photos]
    res = chain.run("images", "quality", urls, allow_empty=True)
    scores = res.value if isinstance(res.value, dict) else {}
    if not scores:
        return 0, 0
    min_edge = settings.image_min_edge
    min_lap = settings.image_min_lapvar
    dropped = 0
    for p in targets:
        ranked: list[tuple[float, str]] = []
        for u in p.photos:
            got = scores.get(u)
            if not got:
                ranked.append((min_lap + 1, u))       # 未知：保留，排在合格图之后
                continue
            w, h, lap = got[0], got[1], got[2]
            if min(w, h) < min_edge or lap < min_lap:
                continue
            ranked.append((lap, u))
        ranked.sort(key=lambda x: -x[0])
        kept = [u for _lap, u in ranked][:per_poi]
        dropped += len(p.photos) - len(kept)
        p.photos = kept
    return dropped, len(targets)
