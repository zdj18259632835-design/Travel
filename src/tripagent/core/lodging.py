"""住宿推荐模型：分人群的多特征加权打分 + 可在线学习的权重。

为什么不是"接一个携程 API 就完事"：携程/同程/美团/去哪儿的开放接口都要求企业资质与
对公合同，个人开发者拿不到房价与点评。所以特征来源只能是：
  结构化部分（高德）：名称、坐标、评分、人均（常缺失）、到市中心距离、最近地铁
  口碑部分（通义千问 enable_search）：安静/卫生/服务/特色文化/安全 六维打分 + 置信度
因此本模块的定位是「带先验的加权线性打分 + 用户反馈在线纠偏」，
而不是假装用真实成交价监督训练出来的排序模型——冷启动权重是人工先验，
只有用户明确点过"选它/不选它"之后，权重才会被真实偏好推动。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..models.poi import POIRecord

FEATURES = ("price", "rating", "reviews", "quiet", "hygiene", "service", "culture", "transport", "safety")


class LodgingPersona(str, Enum):
    BALANCED = "综合"
    VALUE = "性价比"
    QUIET = "安静好眠"
    FAMILY = "亲子友好"
    BUSINESS = "商务交通"
    CULTURE = "特色文化"

    @property
    def label(self) -> str:
        return self.value

    @property
    def hint(self) -> str:
        return _HINTS[self]


_HINTS = {
    LodgingPersona.BALANCED: "各项均衡，评分与位置优先",
    LodgingPersona.VALUE: "同样预算住得更好，压低价格权重",
    LodgingPersona.QUIET: "睡眠浅、怕吵，优先安静与卫生",
    LodgingPersona.FAMILY: "带娃：房型空间、周边吃饭与通勤便利",
    LodgingPersona.BUSINESS: "通勤与稳定：近地铁/高铁、服务响应快",
    LodgingPersona.CULTURE: "民宿特色、在地文化体验优先",
}

# 冷启动先验：和为 1 的人工权重，之后由 feedback 微调
_PRIOR: dict[LodgingPersona, dict[str, float]] = {
    LodgingPersona.BALANCED: {"price": .12, "rating": .26, "reviews": .10, "quiet": .12, "hygiene": .14,
                              "service": .10, "culture": .05, "transport": .08, "safety": .03},
    LodgingPersona.VALUE: {"price": .34, "rating": .18, "reviews": .08, "quiet": .08, "hygiene": .10,
                           "service": .06, "culture": .04, "transport": .09, "safety": .03},
    LodgingPersona.QUIET: {"price": .10, "rating": .16, "reviews": .06, "quiet": .30, "hygiene": .20,
                           "service": .07, "culture": .03, "transport": .05, "safety": .03},
    LodgingPersona.FAMILY: {"price": .14, "rating": .18, "reviews": .08, "quiet": .10, "hygiene": .18,
                            "service": .12, "culture": .03, "transport": .12, "safety": .05},
    LodgingPersona.BUSINESS: {"price": .10, "rating": .16, "reviews": .08, "quiet": .12, "hygiene": .14,
                              "service": .16, "culture": .02, "transport": .18, "safety": .04},
    LodgingPersona.CULTURE: {"price": .12, "rating": .16, "reviews": .06, "quiet": .08, "hygiene": .10,
                             "service": .08, "culture": .30, "transport": .06, "safety": .04},
}


@dataclass
class LodgingFeatures:
    """归一化到 0-1 的特征向量。缺失一律给 0.5（中性），并计入置信度惩罚。"""

    values: dict[str, float] = field(default_factory=lambda: {f: 0.5 for f in FEATURES})
    known: set[str] = field(default_factory=set)
    note: dict[str, str] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        return len(self.known) / len(FEATURES)

    def get(self, name: str) -> float:
        return float(self.values.get(name, 0.5))


def extract_structural(poi: POIRecord, center_distance_m: float | None, max_price_cents: int | None) -> LodgingFeatures:
    """能从高德结构化字段直接算出来的部分。"""
    f = LodgingFeatures()
    if poi.rating:
        f.values["rating"] = max(0.0, min(1.0, (poi.rating - 3.0) / 2.0))
        f.known.add("rating")
    if poi.review_count:
        f.values["reviews"] = min(1.0, math.log10(max(1, poi.review_count)) / 5.0)
        f.known.add("reviews")
    price = poi.avg_cost_cents or poi.ticket_cents
    if price and price > 0:
        if max_price_cents and max_price_cents > 0:
            # 越接近上限越吃亏，但便宜不等于好：给一个下限，避免模型一味推最差的
            f.values["price"] = max(0.0, min(1.0, 1.0 - (price / max_price_cents) * 0.85))
        else:
            f.values["price"] = 0.5
        f.known.add("price")
        f.note["price"] = f"人均 ¥{price / 100:.0f}"
    else:
        f.note["price"] = "价格待确认"
    if center_distance_m is not None:
        # 3km 内满分，15km 外 0 分
        f.values["transport"] = max(0.0, min(1.0, 1.0 - (center_distance_m / 1000 - 3.0) / 12.0))
        f.known.add("transport")
        f.note["transport"] = f"距中心 {center_distance_m / 1000:.1f}km"
    if poi.metro:
        f.values["transport"] = max(f.values["transport"], 0.8)
        f.known.add("transport")
        f.note["transport"] = (f.note.get("transport", "") + f" · 地铁{poi.metro}").strip(" ·")
    if poi.kid_friendly is not None:
        f.values["quiet"] = 0.75 if poi.kid_friendly else 0.5
    return f


def apply_review_scores(f: LodgingFeatures, scores: dict[str, float], evidence: str = "") -> None:
    """把大模型归纳出来的 0-5 口碑分写进特征。"""
    for key in ("quiet", "hygiene", "service", "culture", "safety"):
        v = scores.get(key)
        if isinstance(v, (int, float)) and 0 <= v <= 5:
            f.values[key] = max(0.0, min(1.0, v / 5.0))
            f.known.add(key)
    if evidence:
        f.note["reviews_evidence"] = evidence[:160]


class LodgingRanker:
    """加权打分 + 反馈在线学习（感知机式更新，够用且可解释）。"""

    def __init__(self, model_path: Path | None = None, lr: float = 0.06) -> None:
        self.path = model_path
        self.lr = lr
        self.weights = {p: dict(w) for p, w in _PRIOR.items()}
        self.samples = {p: 0 for p in LodgingPersona}
        self.trained_at = ""
        if model_path and model_path.exists():
            try:
                raw = json.loads(model_path.read_text(encoding="utf-8"))
                for persona_name, table in (raw.get("weights") or {}).items():
                    persona = LodgingPersona(persona_name)
                    for k, v in table.items():
                        if k in self.weights[persona] and isinstance(v, (int, float)):
                            self.weights[persona][k] = float(v)
                self.samples = {LodgingPersona(k): int(v) for k, v in (raw.get("samples") or {}).items()} \
                    if raw.get("samples") else self.samples
                self.trained_at = str(raw.get("trained_at", ""))
            except Exception:
                pass  # 模型文件坏了就退回先验，不影响推荐

    def score(self, f: LodgingFeatures, persona: LodgingPersona) -> tuple[float, list[tuple[str, float, float]]]:
        w = self.weights[persona]
        parts = [(feat, w[feat], w[feat] * f.get(feat)) for feat in FEATURES]
        total = sum(p for _, _, p in parts)
        # 数据缺失惩罚：覆盖率每低 10%，总分扣 4%，防止"信息少的酒店"虚高
        penalty = (1.0 - f.coverage) * 0.4
        return total * (1.0 - penalty), parts

    def rank(self, pairs: list[tuple[POIRecord, LodgingFeatures]], persona: LodgingPersona
             ) -> list[tuple[POIRecord, LodgingFeatures, float, list[tuple[str, float, float]]]]:
        out = [(poi, f, *self.score(f, persona)) for poi, f in pairs]
        out.sort(key=lambda x: -x[2])
        return out

    def learn(self, persona: LodgingPersona, f: LodgingFeatures, chosen: bool) -> None:
        """chosen=True 表示用户实际选了这家；False 表示看了但没选。"""
        w = self.weights[persona]
        target = 1.0 if chosen else 0.0
        pred = self.score(f, persona)[0]
        err = target - min(1.0, pred)
        if abs(err) < 1e-6:
            return
        for feat in FEATURES:
            w[feat] = max(0.01, w[feat] + self.lr * err * f.get(feat))
        s = sum(w.values())
        for feat in FEATURES:  # 归一化，保持"权重和为 1"的可解释性
            w[feat] /= s
        self.samples[persona] = self.samples.get(persona, 0) + 1
        self._save()

    def _save(self) -> None:
        if not self.path:
            return
        from datetime import datetime

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "weights": {p.value: {k: round(v, 4) for k, v in w.items()} for p, w in self.weights.items()},
                "samples": {p.value: n for p, n in self.samples.items()},
                "trained_at": datetime.now().isoformat(timespec="seconds"),
                "note": "先验权重来自人工设定，仅当用户给出选/不选反馈后才被推动；不是用真实成交价训练的。",
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    def weights_table(self, persona: LodgingPersona) -> list[tuple[str, float]]:
        w = self.weights[persona]
        return sorted(((f, w[f]) for f in FEATURES), key=lambda kv: -kv[1])


FEATURE_LABEL = {
    "price": "价格合适度", "rating": "评分", "reviews": "评价量", "quiet": "安静",
    "hygiene": "卫生", "service": "服务", "culture": "特色文化", "transport": "交通", "safety": "安全",
}


_PERSONA_RULES: list[tuple[LodgingPersona, str]] = [
    (LodgingPersona.QUIET, r"安静|睡眠|怕吵|休息好|睡得好|不吵|浅眠"),
    (LodgingPersona.VALUE, r"性价比|穷游|省钱|平价|便宜|实惠|学生"),
    (LodgingPersona.FAMILY, r"亲子|带[小孩娃宝]|儿童|民宿大床|家庭房|遛娃|宝宝"),
    (LodgingPersona.BUSINESS, r"出差|公务|会议|培训|办事|考察|高铁站附近"),
    (LodgingPersona.CULTURE, r"民宿|特色|文化|设计|古镇|院落|老宅|非遗|在地"),
]


def persona_from_text(text: str) -> tuple[LodgingPersona | None, str]:
    """从用户原话推断住宿偏好人群；推不出来返回 None，让界面去问。

    顺序即优先级：先"安静"再"性价比"，因为"想住得安静点"这类表达
    往往同时带有"不想花太多"，但用户对睡眠的诉求更硬。
    """
    low = (text or "").lower()
    for persona, pattern in _PERSONA_RULES:
        m = re.search(pattern, low)
        if m:
            return persona, f"原话「{m.group(0)}」"
    return None, ""


def pick_candidates(rows, req, nights: int):
    """从住宿候选里挑出"值得打分"的那几家，并算出单晚上限与要提醒用户的话。

    抽成纯函数是为了让 agents/gather.py 能在并发取数阶段就把口碑打分发出去：
    打分本身是一次 15-90 秒的联网归纳，留在主线程里等于白并发。
    返回 (候选列表, 单间每晚预算上限分, 警告文案)。
    """
    p = req.party
    kept = [
        h for h in rows
        if not (p.pets and h.pet_friendly is False)
        and not (p.accessible and h.accessible is False)
    ] or list(rows)

    cap = req.lodging_max_nightly_cents
    if not cap and req.budget_cents and nights:
        cap = int(req.budget_cents * 0.32 / (nights * p.room_count))
    warning = ""
    fit = [h for h in kept if 0 < (h.price_cents or 0) <= cap] if cap else kept
    if cap and not fit:
        # 两种情况必须分开说：「有价但都超预算」和「压根没有价格数据」，
        # 后者说"已改推最便宜的"是误导——没有任何一家有价格可比。
        priced = sorted([h for h in kept if (h.price_cents or 0) > 0], key=lambda h: h.price_cents)
        fit = priced[:5] or kept[:5]
        if priced:
            warning = (
                f"预算 ¥{req.budget_yuan:.0f} 对应约 ¥{cap / 100:.0f}/间/晚（{p.room_count} 间 × {nights} 晚），"
                f"「{req.destination}」没有落在该价位的住宿；已在有价格的候选里改推最便宜的，"
                f"建议提高预算、缩短晚数或换住宿商圈。"
            )
        else:
            warning = (
                f"「{req.destination}」的住宿数据没有可用价格（高德 business.cost 缺失），"
                f"预算校验只覆盖门票与餐饮；住宿请按 ¥{cap / 100:.0f}/间/晚 以内自行确认。"
            )
    return fit[:6], cap, warning
