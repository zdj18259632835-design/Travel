"""并发取数：把一次规划里所有互不依赖的网络请求同时发出去。

为什么必须并发（而不是继续串行）：
一次规划的 6 类外部调用彼此没有依赖——天气预报、气象预警、按类目的 POI 检索、
网友口碑、文旅娱乐项目、住宿候选，全都只依赖「城市 + 诉求」。
串行时它们的耗时是**相加**的：阿勒泰实测 239 秒；并发后是**取最大值**。
而且这些调用 95% 的时间在等网络（高德 1-2 秒、大模型联网 15-90 秒），
CPU 几乎空转，用线程池就能直接吃掉这块空闲。

仍然保持串行的部分（真依赖，不该并发）：
  口碑提纯 → 需要 POI 已到手；住宿口碑特征打分 → 需要住宿候选已到手；
  装配 → 需要全部候选；校验 → 需要装配结果；推荐语/点评 → 需要校验结果。
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from ..knowledge.cities import CityInfo
from ..knowledge.poi_codes import NIGHT_CATEGORIES, categories_for
from ..core.lodging import pick_candidates
from ..models.enums import Category, Interest
from ..models.evidence import EvidenceRecord
from ..models.poi import POIQuery, POIRecord
from ..models.request import TripRequest
from ..models.weather import DayForecast
from ..providers.base import Chain, ChainResult


@dataclass
class Bundle:
    """一次规划需要的全部外部数据，取回即冻结。"""

    forecast: ChainResult = field(default_factory=ChainResult)
    alarms: ChainResult = field(default_factory=ChainResult)
    pois: dict[Category, ChainResult] = field(default_factory=dict)
    hotels: ChainResult = field(default_factory=ChainResult)
    evidence: ChainResult = field(default_factory=ChainResult)
    culture: ChainResult = field(default_factory=ChainResult)
    lodging: ChainResult = field(default_factory=ChainResult)
    elapsed_s: float = 0.0
    tasks: int = 0
    stage_s: dict[str, float] = field(default_factory=dict)

    def slowest(self, n: int = 3) -> str:
        """最晚完成的几路（时刻相对发车，不是各自时长）。

        并发之后"总耗时"没有意义，最后回来的那一路才是这段的瓶颈。
        """
        top = sorted(self.stage_s.items(), key=lambda kv: -kv[1])[:n]
        return " ｜ ".join(f"{k} {v:.1f}s" for k, v in top)

    def trace(self) -> list[str]:
        out = [f"并发取数 {self.tasks} 路，总耗时 {self.elapsed_s:.1f}s"]
        out.append(f"  预报:{self.forecast.source} {len(self.forecast.value or [])} 天")
        out.append(f"  预警:{self.alarms.source} {len(self.alarms.value or [])} 条")
        for cat, res in self.pois.items():
            out.append(f"  POI {cat.value}：{res.source} {len(res.value or [])} 条")
        out.append(f"  住宿候选:{self.hotels.source} {len(self.hotels.value or [])} 条")
        out.append(f"  口碑:{self.evidence.source} {len(self.evidence.value or [])} 条")
        out.append(f"  文旅项目:{self.culture.source} {len(self.culture.value or [])} 条")
        out.append(f"  住宿口碑特征:{self.lodging.source} {len(self.lodging.value or {}) if isinstance(self.lodging.value, dict) else 0} 家")
        out.append(f"  最晚完成：{self.slowest()}")
        return out


def poi_categories(req: TripRequest) -> list[Category]:
    """按兴趣决定要检索哪些类目（决定并发任务数，也决定高德配额消耗）。"""
    cats = categories_for(req.interests or [Interest.PHOTO, Interest.FOOD, Interest.HISTORY])
    for must in (Category.SIGHT, Category.FOOD):
        if must not in cats:
            cats.insert(0, must)
    if req.nightlife and not any(c in NIGHT_CATEGORIES for c in cats):
        cats += [Category.BAR, Category.SHOW]
    return cats[:9]


def evidence_topics(req: TripRequest) -> list[str]:
    topics = [f"{req.destination} 必去景点 预约 门票 避坑"]
    if req.has_interest(Interest.FOOD) or not req.interests:
        topics.append(f"{req.destination} 特色美食 人均 排队")
    if req.nightlife or req.has_interest(Interest.NIGHTLIFE) or req.has_interest(Interest.PHOTO):
        topics.append(f"{req.destination} 夜生活 酒吧 KTV 网红打卡 营业时间 评价")
    return topics[:2]  # 联网归纳单次 15-90 秒，2 个 topic 是延迟与信息的平衡点


def gather(
    chain: Chain,
    city: CityInfo,
    req: TripRequest,
    nights: int,
    web_evidence: bool = True,
    max_workers: int = 8,
    horizon: int = 16,
) -> Bundle:
    import time

    t0 = time.time()
    b = Bundle()
    cats = poi_categories(req)
    jobs: dict[str, Any] = {
        "forecast": lambda: chain.run("forecast", "fetch_forecast", city, horizon, model=DayForecast),
        "alarms": lambda: chain.run("alarm", "fetch_alarms", city, allow_empty=True),
        "hotels": lambda: chain.run(
            "poi", "search", city,
            POIQuery(city=city.name, category=Category.HOTEL,
                     keywords=[req.lodging_area] if req.lodging_area else [], limit=16),
            model=POIRecord,
        ),
        "culture": lambda: chain.run("culture", "collect", city, req.audience or "", 10, model=POIRecord),
    }
    if web_evidence:
        jobs["evidence"] = lambda: chain.run(
            "evidence", "collect", city, evidence_topics(req), limit=10, model=EvidenceRecord
        )
    for cat in cats:
        jobs[f"poi:{cat.value}"] = (
            lambda c=cat: chain.run(
                "poi", "search", city,
                POIQuery(city=city.name, category=c, limit=22 if c in (Category.SIGHT, Category.FOOD) else 14),
                model=POIRecord,
            )
        )

    with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs) + 1)) as pool:
        futures = {pool.submit(fn): key for key, fn in jobs.items()}
        b.tasks = len(futures)
        pending = set(futures)
        while pending:
            # 用 wait 而不是 as_completed：后者把可迭代对象在调用时就固化了，
            # 而这里需要在住宿候选回来后**追加**一个依赖它的任务。
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                key = futures[fut]
                b.stage_s[key] = round(time.time() - t0, 1)
                try:
                    res = fut.result()
                except Exception as exc:  # 单路失败不能拖垮整次规划
                    from ..providers.base import Attempt

                    res = ChainResult(
                        value=None,
                        source="error",
                        attempts=[Attempt("gather", key, False, error=f"{type(exc).__name__}: {str(exc)[:120]}")],
                    )
                if key == "forecast":
                    b.forecast = res
                elif key == "alarms":
                    b.alarms = res
                elif key == "evidence":
                    b.evidence = res
                elif key == "culture":
                    b.culture = res
                elif key.startswith("poi:"):
                    b.pois[Category(key.split(":", 1)[1])] = res
                elif key == "hotels":
                    b.hotels = res
                    # 住宿口碑打分只依赖候选名单，所以候选一回来就发出去，
                    # 让它和剩下的 POI/口碑检索重叠，而不是排在装配前面干等
                    cands, _cap, _warn = pick_candidates(list(res.value or []), req, nights)
                    if cands:
                        nxt = pool.submit(
                            lambda rows=cands: chain.run("lodging", "score", city.name, rows)
                        )
                        futures[nxt] = "lodging"
                        pending.add(nxt)
                        b.tasks += 1
                elif key == "lodging":
                    b.lodging = res

    b.elapsed_s = time.time() - t0
    return b
