"""住宿口碑特征打分：把高德没给的结构化字段（安静/卫生/服务/特色/安全）补出来。

唯一合规可行的来源是大模型联网归纳（不爬美团/点评/携程）。输出显式标
source=qwen_web_search，并带置信度；拿不到就退回中性 0.5，绝不编具体分数。
"""

from __future__ import annotations

import json
import re

from ..core.lodging import LodgingFeatures, apply_review_scores
from ..models.poi import POIRecord
from .base import Chain, Provider, ProviderError, provider

_PROMPT = """下面是「{city}」的候选住宿（JSON 数组）。请对每一家，依据公开可查的网友评价\
（美团/携程/去哪儿/小红书/抖音等平台的点评与笔记内容）给出 0-5 的整数或一位小数打分：
{{"name":"必须与输入完全一致","quiet":安静程度,"hygiene":卫生,"service":服务,\
"culture":"在地文化/设计特色","safety":"周边安全与夜间_return 步行","price_hint":"每晚大致价格区间（人民币，不确定写 unknown）",\
"evidence":"40字以内的评价依据要点","confidence":0.0-1.0}}
只输出 JSON 数组；查不到可靠信息的就把该项省略，不要编造。
候选：{rows}"""


def _scores_from_raw(raw: str) -> list[dict]:
    from .llm import extract_json

    data = extract_json(raw)
    if isinstance(data, dict):
        for key in ("hotels", "items", "results", "list", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []


@provider("lodging", "qwen_search")
class QwenLodgingFeatures(Provider):
    def available(self) -> bool:
        return bool(self.settings.dashscope_api_key) and self.settings.online

    def describe(self) -> tuple[bool, str]:
        if not self.settings.dashscope_api_key:
            return False, "未配置 DASHSCOPE_API_KEY，住宿只按结构化字段打分"
        return True, "通义千问联网归纳住宿六维口碑（安静/卫生/服务/特色/安全/价格区间）"

    def score(self, city_name: str, hotels: list[POIRecord]) -> dict[str, LodgingFeatures]:
        if not hotels:
            return {}
        rows = [{"name": h.name, "district": h.district, "rating": h.rating,
                 "price_yuan": round((h.avg_cost_cents or 0) / 100) or None} for h in hotels[:8]]
        messages = [
            {"role": "system", "content": "你是住宿口碑分析师，只输出合法 JSON。"},
            {"role": "user", "content": _PROMPT.format(city=city_name, rows=json.dumps(rows, ensure_ascii=False))},
        ]
        res = Chain(self.settings).run("llm", "chat", messages, web_search=True, temperature=0.2)
        raw = str(res.value or "")
        if not raw:
            raise ProviderError("住宿口碑归纳无结果")
        out: dict[str, LodgingFeatures] = {}
        for item in _scores_from_raw(raw):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            feats = LodgingFeatures()
            scores = {k: item.get(k) for k in ("quiet", "hygiene", "service", "culture", "safety")}
            apply_review_scores(feats, scores, str(item.get("evidence") or ""))
            hint = str(item.get("price_hint") or "")
            nums = [int(n) for n in re.findall(r"\d{2,5}", hint)]
            if nums:
                feats.values["price"] = 0.5
                feats.note["price_hint"] = f"网友提及约 ¥{min(nums)}-{max(nums)}"
            feats.note["confidence"] = str(item.get("confidence") or 0.4)
            feats.note["source"] = "qwen_web_search"
            out[name] = feats
        if not out:
            raise ProviderError("住宿口碑 JSON 解析为空")
        return out


@provider("lodging", "none")
class NoLodgingFeatures(Provider):
    """离线兜底：全部中性值，让模型只按结构化字段排序。"""

    def score(self, city_name: str, hotels: list[POIRecord]) -> dict[str, LodgingFeatures]:
        return {h.name: LodgingFeatures() for h in hotels}
