"""诗词数据源 provider：只负责"把这个城市的联句库拿出来"，匹配规则在 core.poetry。

为什么默认用本地库而不是"联网搜古诗"：
- 联网搜要叫大模型，一次规划已经调了五六次模型，再加一次就是白等十几秒；
- 模型背的诗**会张冠李戴**（把"春风不度玉门关"记成另一首是常事），
  而这类错误用户看不出来、我们也无法自动核对；本地库每一条都是人工核对过作者与出处的；
- 本地库毫秒级、离线可用、可复现，正好符合 evidence_chain 已经采用的"先本地后联网"次序。

将来要接古诗文网之类的公开接口，就再注册一个 @provider("poetry", "gushiwen")
并把 poetry_chain 改成 "gushiwen,fixture,none"，core 与界面都不用动。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..knowledge.cities import CityInfo
from .base import Provider, provider

_CACHE: dict[str, dict] = {}


def _doc(path: Path) -> dict:
    key = str(path)
    if key not in _CACHE:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        _CACHE[key] = raw if isinstance(raw, dict) else {}
    return _CACHE[key]


def entries_for(doc: dict, city_name: str) -> list[dict]:
    """按城市名取条目，顺带处理两件事：

    1. 去掉行政后缀——高德给的是"南昌市""奉节县"，库里存的是"南昌""奉节"；
       只剥 市/县 两个后缀，剥"州""阳"这类会把苏州、揭阳一起削没。
    2. aliases——常熟属于苏州、奉节属于重庆，但用户搜的是县域名，
       不映射的话"白帝城"那条李白就拿不到。
    """
    cities: dict = doc.get("cities") or {}
    name = (city_name or "").strip()
    for suffix in ("市", "县"):
        if name.endswith(suffix) and len(name) > 2:
            name = name[: -len(suffix)]
            break
    if not name:
        return []
    target = (doc.get("aliases") or {}).get(name, name)
    rows = cities.get(target) or cities.get(name) or []
    return [r for r in rows if isinstance(r, dict)]


@provider("poetry", "fixture")
class FixturePoetry(Provider):
    def available(self) -> bool:
        return True     # 本地库，断网也要在

    def describe(self) -> tuple[bool, str]:
        doc = _doc(Path(self.settings.fixture_dir) / "poetry.json")
        cities = doc.get("cities") or {}
        total = sum(len(v) for v in cities.values() if isinstance(v, list))
        if not cities:
            return False, "本地诗词库缺失（data/fixtures/poetry.json）"
        return True, f"本地公版诗文联句库：{len(cities)} 个城市 / {total} 条，离线可用、不耗 token"

    def lookup(self, city: CityInfo) -> list[dict[str, Any]]:
        doc = _doc(Path(self.settings.fixture_dir) / "poetry.json")
        return entries_for(doc, city.name)


@provider("poetry", "none")
class NoPoetry(Provider):
    """显式空实现：把 poetry_chain 设成 "none" 就能整层关掉，界面一句诗都不出现。"""

    def available(self) -> bool:
        return True

    def lookup(self, city: CityInfo) -> list[dict[str, Any]]:
        return []
