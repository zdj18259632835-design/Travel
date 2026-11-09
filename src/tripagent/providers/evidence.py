"""口碑/文化内容 provider。

为什么没有小红书/抖音 provider：这两家开放平台都要求企业资质 + 逐项权限审核，
个人学生号拿不到内容接口，逆向爬取违反 ToS 且随时失效，比赛演示不能依赖。
合法替代：让通义千问开启 enable_search 做「联网检索 + 口碑归纳」，
产出显式标注 source=model_summary、confidence 打折，并保留原始片段供人工核对。
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..knowledge.cities import CityInfo
from ..models.evidence import EvidenceRecord
from .base import Chain, Provider, ProviderError, provider
from .llm import extract_json

_GUIDE_PROMPT = """请为「{city}」输出一份旅行背景知识 JSON：
{{"culture":"60-120字城市文化性格","signature_foods":["特色小吃/菜，含推荐吃法"],\
"photo_spots":["网红打卡点，注明最佳时段"],"tips":["本地人习惯/排队/支付/交通提醒"],\
"safety":["夜间/人群/财物等安全提示"],"nightlife_areas":["夜间娱乐聚集街区"]}}
只写公开常识与稳定信息，不确定的写进 tips 并标注「建议现场核实」。不要输出 JSON 以外文字。"""


_UGC_LINES_PROMPT = """请针对「{city}」的「{topic}」主题，检索网友（小红书/大众点评/携程/B站/文旅局公众号等公开内容）的真实反馈，\
输出 {limit} 行，每行一条，字段用竖线 | 分隔，顺序固定：
标题|摘要(30-90字，含具体信息如排队时长/时段/价格区间/避坑点)|景点或店名|是否需预约(是或否)|预约渠道|提前几天|最佳时段|避坑(多条用中文分号；分隔)|结论(值得或一般或劝退)|置信度(0到1)
硬性要求：行内绝对不要出现竖线字符；不要出现引号；不要编号、不要表头、不要解释，只输出这些行。\
拿不准的信息就降低置信度或留空该字段（留空就写两个竖线相邻）。"""

_FIELDS = ("title", "snippet", "poi_name", "need_reservation", "reservation_channel",
           "lead_days", "best_time", "pitfalls", "verdict", "confidence")


def parse_ugc_lines(raw: str, city_name: str) -> list[EvidenceRecord]:
    """按行解析。

    为什么不用 JSON：qwen 在联网模式下会在中文摘要里写入未转义的英文引号，
    把 JSON 结构破坏掉（实测间歇性失败，且失败点每次都不一样）。
    扁平的 | 分隔格式对这类噪声天然免疫。
    """
    out: list[EvidenceRecord] = []
    for line in (raw or "").splitlines():
        text = line.strip().lstrip("-*• 0123456789.、) ")
        if "|" not in text:
            continue
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 5:
            continue
        parts += [""] * (10 - len(parts))
        title, snippet, poi_name, need, channel, lead, best, pitfalls, verdict, conf = parts[:10]
        if not title and not snippet:
            continue
        try:
            confidence = float(conf) if conf else 0.45
        except ValueError:
            confidence = 0.45
        lead_days = None
        for ch_num in ("1", "2", "3", "5", "7", "10", "14", "15", "30"):
            if ch_num in lead:
                lead_days = int(ch_num)
                break
        extracted = {
            "need_reservation": need.startswith("是"),
            "reservation_channel": channel[:40],
            "reservation_lead_days": lead_days,
            "best_time": best[:12],
            "pitfalls": [p.strip() for p in pitfalls.split("；") if p.strip()][:4],
            "verdict": verdict[:8],
        }
        if lead_days:
            extracted["stay_minutes"] = None
        out.append(
            EvidenceRecord(
                id=f"qwl:{hashlib.md5((city_name + title + snippet).encode()).hexdigest()[:12]}",
                city=city_name,
                poi_name=poi_name[:60],
                kind="model_summary",
                title=title[:120],
                snippet=snippet[:600],
                author_type="netizen",
                source="qwen_web_search",
                confidence=min(1.0, max(0.1, confidence)),
                extracted=extracted,
                tags=[t for t in (verdict[:6], best[:6]) if t],
            )
        )
    return out


@provider("evidence", "qwen_search")
class QwenSearchEvidence(Provider):
    """用大模型联网检索公开口碑，替代不可得的 UGC 直连。"""

    def available(self) -> bool:
        return bool(self.settings.dashscope_api_key) and self.settings.online

    def describe(self) -> tuple[bool, str]:
        if not self.settings.dashscope_api_key:
            return False, "未配置 DASHSCOPE_API_KEY，将使用本地精选口碑库"
        return True, "通义千问联网检索 + 口碑归纳（enable_search）"

    def collect(self, city: CityInfo, topics: list[str], limit: int = 6) -> list[EvidenceRecord]:
        llm = Chain(self.settings)
        reasons: list[str] = []

        def one(topic: str) -> tuple[list[EvidenceRecord], list[str]]:
            messages = [
                {"role": "system", "content": "你只输出规定格式的文本行，不输出 JSON、不输出解释。"},
                {"role": "user", "content": _UGC_LINES_PROMPT.format(city=city.name, topic=topic, limit=limit)},
            ]
            res = llm.run("llm", "chat", messages, web_search=True, temperature=0.3)
            raw = str(res.value or "")
            if not raw:
                return [], [f"{a.provider}:{a.error}" for a in res.attempts if not a.ok]
            rows = parse_ugc_lines(raw, city.name)
            if not rows:
                # 模型偶尔仍会回 JSON，兼容一次再放弃
                try:
                    alt = extract_json(raw)
                except Exception:
                    alt = None
                rows = _as_list(alt)
                if rows and isinstance(rows[0], dict):
                    rows = [
                        EvidenceRecord(
                            id=f"qws:{hashlib.md5(json.dumps(r, ensure_ascii=False).encode()).hexdigest()[:12]}",
                            city=city.name,
                            poi_name=str(r.get("poi_name", ""))[:60],
                            kind="model_summary",
                            title=str(r.get("title", ""))[:120],
                            snippet=str(r.get("snippet", ""))[:600],
                            url=str(r.get("url", ""))[:300],
                            source="qwen_web_search",
                            confidence=float(r.get("confidence") or 0.4),
                            extracted=r.get("extracted") or {},
                            tags=[str(t) for t in (r.get("tags") or [])][:8],
                        )
                        for r in rows[:limit]
                        if isinstance(r, dict)
                    ]
            return rows, []

        # 每个 topic 都是一次 15-90 秒的联网检索。以前串行跑 2 个 topic，
        # gather 那条并发线等于白做——整个规划的耗时就是这里翻倍。
        todo = topics[:3]
        out: list[EvidenceRecord] = []
        if todo:
            with ThreadPoolExecutor(max_workers=len(todo)) as pool:
                for rows, errs in pool.map(one, todo):
                    out.extend(rows[:limit])
                    reasons.extend(errs)
        if not out:
            raise ProviderError("联网口碑检索无结果" + (f"（{' | '.join(reasons[:2])}）" if reasons else ""))
        return out


@provider("evidence", "fixture")
class FixtureEvidence(Provider):
    """本地精选口碑库（人工整理的公开信息），保证离线可用且可复现。"""

    def collect(self, city: CityInfo, topics: list[str], limit: int = 6) -> list[EvidenceRecord]:
        doc = _load(self.settings.fixture_dir, city.key)
        rows = doc.get("evidence") or []
        out: list[EvidenceRecord] = []
        for r in rows:
            try:
                rec = EvidenceRecord.model_validate(r)
            except Exception:
                continue
            if topics:
                blob = " ".join([rec.title, rec.snippet, rec.poi_name, *rec.tags]).lower()
                if not any(t.lower() in blob for t in topics):
                    continue
            out.append(rec)
        return out[:limit] or [EvidenceRecord.model_validate(r) for r in rows[:limit] if isinstance(r, dict)]


@provider("guide", "qwen_search")
class QwenGuide(Provider):
    def available(self) -> bool:
        return bool(self.settings.dashscope_api_key) and self.settings.online

    def fetch_guide(self, city: CityInfo) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": "只输出合法 JSON，中文。"},
            {"role": "user", "content": _GUIDE_PROMPT.format(city=city.name)},
        ]
        value = Chain(self.settings).run("llm", "json_call", messages).value
        if not isinstance(value, dict):
            raise ProviderError("城市背景知识生成失败")
        value["_source"] = "qwen"
        return value


@provider("guide", "fixture")
class FixtureGuide(Provider):
    def fetch_guide(self, city: CityInfo) -> dict[str, Any]:
        doc = _load(self.settings.fixture_dir, city.key)
        guide = doc.get("guide") or {}
        if not guide:
            return {}
        guide = dict(guide)
        guide.setdefault("_source", "fixture")
        guide.setdefault("city", city.name)
        return guide


def _as_list(value: Any) -> list:
    """模型有时返回裸数组，有时包一层 {"notes": [...]}——两种都要能吃掉。

    之前只认 isinstance(list)，导致联网口碑明明有内容却被判成"无结果"，
    白白降级回本地知识库。
    """
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("notes", "items", "results", "data", "evidence", "reviews", "list"):
            if isinstance(value.get(key), list):
                return value[key]
        for v in value.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def _load(fixture_dir, city_key: str) -> dict:
    path = fixture_dir / f"{city_key}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
