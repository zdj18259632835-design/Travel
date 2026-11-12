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


# ---------------------------------------------------------------------------
# 第一道闸：这条 POI 到底是不是我们要拍照的那个东西
# ---------------------------------------------------------------------------
# 高德一次给 8 条候选，里面混着「宝塔山景区地下停车场」「游客中心」「枣园宾馆」。
# 老代码用"名字互相包含"匹配，而「宝塔山景区」确实包含在「宝塔山景区地下停车场」里，
# 于是停车场的照片进了景区相册；更糟的是车身边缘锐利、拉普拉斯方差比风景照还高，
# 排序反而把它们顶到第一张。用户看到的"就给我看几辆白车"是这么来的。
#
# 高德 typecode 前两位是稳定大类（scripts/probe_image_types.py 实测确认）：
#   11 风景名胜 / 08 体育休闲（酒吧 KTV 密室）/ 14 科教文化（剧院 博物馆）
#   05 餐饮 / 06 购物（特色街区 夜市）/ 19 地名地址（山体 水域，风景照常挂这里）
TYPE_ALLOW = {"11", "08", "14", "05", "06", "19"}
# 明确排除：停车场、政府机关、住宿（活动卡片里放酒店大堂照很怪）、汽车服务、医院、银行
TYPE_BLOCK = {"01", "02", "03", "04", "07", "09", "10", "12", "13", "15", "16", "17", "18"}


def typecode_ok(row: dict) -> bool:
    """POI 类型是否适合作为"文化娱乐展示图"的来源。

    多类型（`110210|110204`）只要有一个落在允许大类里就算通过。
    拿不到 typecode 时不判死刑——老接口与小 POI 经常缺字段，误杀比漏杀贵。
    """
    codes = str(row.get("typecode") or "").split("|")
    seen = [c.strip()[:2] for c in codes if c and c.strip()]
    if not seen:
        return True
    if any(c in TYPE_ALLOW for c in seen):
        return True
    return not any(c in TYPE_BLOCK for c in seen)


def match_rank(query: str, candidate: str) -> int:
    """同名程度：3 完全同名，2 候选以查询开头，1 互相包含，0 不算同一个地方。

    排序时先按这个再按清晰度，"宝塔山景区"就不会被"宝塔山景区地下停车场"抢走封面。
    """
    a, b = _norm(query), _norm(candidate)
    if not a or not b:
        return 0
    if a == b:
        return 3
    if b.startswith(a) or a.startswith(b):
        return 2
    if a in b or b in a:
        return 1
    n = min(len(a), len(b))
    return 1 if n >= 4 and a[:4] == b[:4] else 0


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


def scene_signals(data: bytes) -> dict:
    """内容信号：清晰度之外，这张图"像不像一张值得看的图"。

    为什么需要它：拉普拉斯方差只看对焦，停车场的白车边缘锐利，方差比阴天的风景照还高。
    这里补三条同样可复现的指标，全部 numpy + Pillow，不引入 OpenCV/torch：

      gray    低饱和亮灰占比（车身、水泥地、玻璃幕墙的共同特征）；
      skyveg  天空蓝与植被绿里较大的那个（真风景照几乎必有一个）；
      color   Hasler & Süsstrunk 的色彩丰富度（rg/yb 双通道标准差 + 均值惩罚项）。

    实测分布（scripts/probe_image_types.py，按高德 typecode 分组，n=110）：
      风景名胜 灰白中位 0.096　生活服务 0.273　住宿 0.585
      风景名胜 天空或植被中位 0.311　生活服务 0.104
    所以这两条**只用于排序与极端淘汰**，不能当主判据——阴天、夜景会让它们大幅漂移。
    """
    import io

    import numpy as np
    from PIL import Image

    im = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = im.size
    im.thumbnail((160, 160))
    a = np.asarray(im, dtype=np.float32) / 255.0
    hsv = np.asarray(im.convert("HSV"), dtype=np.float32) / 255.0
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]

    gray = float(((sat < 0.10) & (val > 0.55)).mean())
    # 天空必须有饱和度：白墙、白车、云层的饱和度和真天空差一个量级。
    # 第一版这里只卡了 hue 与亮度，结果合成的一张水泥地图 skyveg=1.0，判据当场失效——
    # 这类"看起来合理但被反例打穿"的阈值，只有拿最讨厌的样本去量才看得出来。
    sky = float(((hue > 0.52) & (hue < 0.75) & (sat >= 0.15) & (sat < 0.65) & (val > 0.55)).mean())
    veg = float(((hue > 0.18) & (hue < 0.46) & (sat > 0.22) & (val > 0.12)).mean())
    rg = a[..., 0] - a[..., 1]
    yb = 0.5 * (a[..., 0] + a[..., 1]) - a[..., 2]
    color = float(
        np.sqrt(rg.var() + yb.var()) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
    )
    return {"w": w, "h": h, "gray": round(gray, 4), "skyveg": round(max(sky, veg), 4),
            "color": round(color, 4)}


def score_url(url: str, timeout: float = 6.0) -> list | None:
    """下载并打分：[宽, 高, 拉普拉斯方差, 灰白占比, 天空或植被, 色彩丰富度]。

    拿不到字节时返回 None（调用方按"未知"处理，不误杀）。
    """
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; TripAgent/0.1)"})
        if r.status_code >= 400 or not r.content:
            return None
        w, h, lap = sharpness(r.content)
        sig = scene_signals(r.content)
        return [w, h, lap, sig["gray"], sig["skyveg"], sig["color"]]
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

    def _one(self, city: CityInfo, name: str, per: int = 6) -> list[str]:
        params = {
            "key": self.settings.amap_key,
            "keywords": name,
            "region": city.adcode or city.name,
            "citylimit": "true",
            "page_size": 20,
            "page_num": 1,
            "show_fields": "photos",
        }

        def fetch() -> list[str]:
            data = self.get_json("https://restapi.amap.com/v5/place/text", params)
            if not data:
                raise ProviderError("高德无响应")
            if str(data.get("status")) != "1":
                raise ProviderError(f"高德返回 {data.get('info')}({data.get('infocode')})")
            rows = [r for r in (data.get("pois") or []) if isinstance(r, dict)]
            # 先过类型，再按同名程度排序：停车场/游客中心/宾馆在第一刀就被挡掉
            cand = [(match_rank(name, str(r.get("name") or "")), typecode_ok(r), r) for r in rows]
            cand = [c for c in cand if c[0] > 0 and c[1]]
            cand.sort(key=lambda x: -x[0])
            urls: list[str] = []
            for rank, _ok, row in cand[:8]:
                urls.extend(amap_photos(row, limit=per))
            urls = list(dict.fromkeys(urls))[: max(per + 4, 12)]
            # 可达性与打分合并成一次 GET：以前 HEAD 探活 + 后续 GET 打分是两趟流量。
            # 并发批量下（8 路），拿不到字节的直接丢——这一趟本来就联网，"没下到"就是"给不了用户看"。
            scored = self.quality(urls)
            kept = [u for u in urls if scored.get(u)]
            return kept[:per] or urls[:1]      # 全军覆没时留一张，至少不是空白卡片

        # 键版本 amap→amap2：取图行数与 page_size 都变了，旧缓存里只有 3 张，
        # 混用会让人以为"放宽行数没用"。
        return self.cached_call(("images", "amap2", city.key, name, per), fetch)

    def fetch_batch(self, city: CityInfo, names: list[str], per: int = 6) -> dict[str, list[str]]:
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
        """批量打分：url -> [宽, 高, 方差, 灰白, 天空或植被, 色彩]。分数落盘缓存，同图全站只下一次。

        缓存键从 imgq 换成 imgq2：打分维度加了，旧记录只有前三项，混用会被当成"没有内容信号"。
        """
        todo = [u for u in dict.fromkeys(urls) if u]
        if not todo:
            return {}

        def scored(u: str) -> list:
            def fetch() -> list:
                got = score_url(u)
                return list(got) if got else []
            try:
                return self.cached_call(("imgq2", u), fetch) or []
            except Exception:
                return []

        # 12 路：一次预筛要下 60-100 张图，8 路实测把这段拖到 21.6s，成了整条流水线的新瓶颈
        with ThreadPoolExecutor(max_workers=min(12, len(todo))) as pool:
            results = list(pool.map(scored, todo))
        return {u: got for u, got in zip(todo, results) if got}


@provider("images", "fixture")
class FixtureImages(Provider):
    """离线兜底：本地知识库里若已存过实拍图就直接用，没有就不显示。

    刻意不用占位图：假图比没图更伤信任。
    """

    def fetch_batch(self, city: CityInfo, names: list[str], per: int = 6) -> dict[str, list[str]]:
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


def content_rank(w: float, h: float, lap: float, gray: float, skyveg: float, color: float) -> float:
    """把"清楚"和"值得看"合成一个排序分。数值来自实测分布，不是随手拍的权重。"""
    import math

    return math.log(lap + 1.0) * 6.0 + skyveg * 40.0 + color * 1.5 - gray * 50.0


def screen_images(chain: Chain, settings, pois, probe: int = 3) -> tuple[int, int]:
    """装配前的轻量闸门：每个候选只探前 probe 张，出现合格图就够，全不合格则清空。

    为什么不直接复用 filter_poi_photos：那道闸门要把每个候选的 6 张全部下载打分，
    24 个候选冷启动实测 26.7s，直接把一次规划的耗时顶到 55s。
    而装配前真正要回答的问题只有一个——"这个候选有没有脸面进池子"，
    探 3 张足够判定；剩下 3 张留给装配后那批真被排上的地点去下（数量从 24 缩到 ~12）。
    """
    targets = [p for p in pois if getattr(p, "photos", None)]
    if not targets:
        return 0, 0
    urls = [u for p in targets for u in p.photos[:probe]]
    res = chain.run("images", "quality", urls, allow_empty=True)
    scores = res.value if isinstance(res.value, dict) else {}
    if not scores:
        return 0, 0
    min_edge, min_lap = settings.image_min_edge, settings.image_min_lapvar
    emptied = 0
    for p in targets:
        ok = []
        for u in p.photos[:probe]:
            got = scores.get(u)
            if not got:
                ok.append(u)                      # 没量到：先留着，装配后的全量闸门再算
                continue
            if min(got[0], got[1]) >= min_edge and got[2] >= min_lap:
                ok.append(u)
        if ok:
            p.photos = ok + [u for u in p.photos[probe:]]
        else:
            p.photos = []
            emptied += 1
    return emptied, len(targets)


def filter_poi_photos(chain: Chain, settings, pois, per_poi: int = 6) -> tuple[int, int]:
    """给一批 POI 筛掉糊图与小图，并把"最该看的一张"排到最前。

    返回 (淘汰张数, 受影响地点数)。

    **删除只依据硬事实**：短边 < image_min_edge（放大必糊）、
    拉普拉斯方差 < image_min_lapvar（梯度被抹平）。
    内容信号（灰白占比、天空/植被、色彩丰富度）**只参与排序，不参与删除**——
    这不是保守，是量出来的：scripts/probe_image_types.py 在 358 张真实图上扫描阈值，
    灰白线取 0.40 时误杀 7/204 张真景点图，却只多拦住 6/106 张无聊图。
    为了删掉几张停车场照片而丢掉阴天的真风景照，性价比不成立。

    真正拦住"几辆白车"的是上一刀：POI 类型闸（typecode_ok）+ 同名程度（match_rank）。
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
            gray, skyveg, color = (got + [0.0, 1.0, 0.0])[3:6]
            if min(w, h) < min_edge or lap < min_lap:
                continue
            ranked.append((content_rank(w, h, lap, gray, skyveg, color), u))
        ranked.sort(key=lambda x: -x[0])
        kept = [u for _score, u in ranked][:per_poi]
        dropped += len(p.photos) - len(kept)
        p.photos = kept
    return dropped, len(targets)
