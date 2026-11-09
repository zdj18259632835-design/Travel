"""文旅娱乐项目采集：让推荐不再只有"景点 + 餐厅"。

给一个市/县/乡镇，抓当地热门的演出、非遗体验、夜游、节庆、亲子营地、
沉浸式娱乐等项目，并带上「适合谁」与证据。

输出格式刻意用竖线分隔的文本行而不是 JSON：实测 qwen 开 enable_search 后
会在中文里写入未转义引号，把 JSON 打断，且失败点每次都不一样（口碑模块踩过同样的坑）。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..knowledge.cities import CityInfo
from ..models.enums import Category
from ..models.poi import POIRecord
from .poi import detect_closed
from .base import Chain, Provider, ProviderError, provider

_PROMPT = """请列出「{city}」当前（近一年内）真正可去的热门文旅与娱乐项目，{count} 条，每行一条，\
字段用竖线 | 分隔，顺序固定：
项目名|类目|一句话亮点(30-60字，写具体信息：场次/时长/价格区间/需要提前多久订/适合谁)|\
类目建议(演出或非遗体验或夜游或节庆或亲子营地或主题乐园或运动休闲或人文古迹)|人均人民币数字或unknown|\
适合人群(亲子;情侣;长辈;朋友;独行;商务，用分号分隔)|最佳时段|需要提前预约(是或否)|预约渠道|避坑要点|置信度0到1
硬性要求：行内不要出现竖线；不要出现引号；不要编号与表头；只输出这些行。\
乡镇/县城没有独立大项目时，就写它所属州市范围内当天往返可达的项目，并在亮点里注明车程。"""

_CATEGORY_MAP = {
    "演出": Category.SHOW, "非遗体验": Category.HISTORIC, "非遗": Category.HISTORIC,
    "夜游": Category.SIGHT, "节庆": Category.SHOW, "亲子营地": Category.AMUSEMENT,
    "主题乐园": Category.AMUSEMENT, "运动休闲": Category.SPORTS, "人文古迹": Category.HISTORIC,
}


def parse_rows(raw: str, city: CityInfo) -> list[POIRecord]:
    out: list[POIRecord] = []
    for line in (raw or "").splitlines():
        text = line.strip().lstrip("-*•　 ")
        if text.count("|") < 6:
            continue
        parts = [p.strip() for p in text.split("|")]
        name = parts[0]
        if not name or len(name) < 2:
            continue
        if detect_closed(name, *parts):
            continue
        cat = _CATEGORY_MAP.get(parts[3] if len(parts) > 3 else "", Category.SIGHT)
        price = None
        for token in (parts[4] if len(parts) > 4 else "").split("-"):
            num = re.search(r"\d{1,5}", token)
            if num:
                price = int(num.group(0))
                break
        crowd = [c for c in re.split(r"[;；,，]", parts[5] if len(parts) > 5 else "") if c]
        why = parts[2] if len(parts) > 2 else ""
        pitfalls = [p for p in re.split(r"[;；]", parts[9] if len(parts) > 9 else "") if p]
        try:
            conf = float(parts[10]) if len(parts) > 10 and parts[10] else 0.45
        except ValueError:
            conf = 0.45
        out.append(
            POIRecord(
                id=f"cul:{hashlib.md5((city.key + name).encode()).hexdigest()[:12]}",
                name=name[:60],
                category=cat,
                city=city.name,
                avg_cost_cents=None if price is None else price * 100,
                stay_minutes=120 if cat in (Category.AMUSEMENT, Category.SPORTS) else 90,
                indoor=cat in (Category.SHOW,),
                need_reservation=(parts[7] if len(parts) > 7 else "").startswith("是"),
                reservation_channel=(parts[8] if len(parts) > 8 else "")[:40],
                best_time=(parts[6] if len(parts) > 6 else "")[:12],
                why=why[:300],
                pitfalls=pitfalls[:3],
                tags=["文旅项目", "网友/媒体归纳", *crowd[:4]],
                popularity=0.6 + 0.3 * conf,
                source="qwen_web_search",
                confidence=min(1.0, max(0.15, conf)),
                kid_friendly=("亲子" in " ".join(crowd)) or None,
                accessible=("长辈" in " ".join(crowd)) or None,
                raw={"crowd": crowd, "reserve": parts[7] if len(parts) > 7 else ""},
            )
        )
    return out


@provider("culture", "qwen_search")
class QwenCulture(Provider):
    def available(self) -> bool:
        return bool(self.settings.dashscope_api_key) and self.settings.online

    def describe(self) -> tuple[bool, str]:
        if not self.settings.dashscope_api_key:
            return False, "未配置 DASHSCOPE_API_KEY，文旅项目只读本地知识库"
        return True, "通义千问联网检索当地热门文旅娱乐项目（演出/非遗/夜游/节庆/营地）"

    def collect(self, city: CityInfo, audience: str = "", count: int = 10) -> list[POIRecord]:
        ask = f"同行人群是「{audience}」，请优先给出适合该人群的项目。" if audience else ""
        messages = [
            {"role": "system", "content": "你只输出规定格式的文本行，不输出 JSON、不输出解释。"},
            {"role": "user", "content": _PROMPT.format(city=city.name, count=count) + ask},
        ]
        res = Chain(self.settings).run("llm", "chat", messages, web_search=True, temperature=0.3)
        raw = str(res.value or "")
        rows = parse_rows(raw, city)
        if not rows:
            reason = " | ".join(a.error for a in res.attempts if not a.ok)[:160]
            raise ProviderError(f"文旅项目采集无结果（{reason or '输出格式不符'}）")
        return rows


@provider("culture", "fixture")
class FixtureCulture(Provider):
    """离线兜底：读 data/fixtures/<city>.json 里的 culture 段（没有就返回空）。"""

    def collect(self, city: CityInfo, audience: str = "", count: int = 10) -> list[POIRecord]:
        path = Path(self.settings.fixture_dir) / f"{city.key}.json"
        if not path.exists():
            return []
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        rows = doc.get("culture") or []
        out: list[POIRecord] = []
        for r in rows[:count]:
            try:
                rec = POIRecord.model_validate(r)
            except Exception:
                continue
            rec.source = "fixture"
            out.append(rec)
        if audience:
            out.sort(key=lambda p: 0 if audience in " ".join(p.tags) else 1)
        return out
