"""POI provider：高德真实检索 + 本地城市知识库。

酒吧/KTV/游乐园/剧场这类「生活娱乐」查询全部落到高德 POI 分类编码（见 knowledge/poi_codes.py），
这是学生项目能拿到的唯一稳定、合法、带评分与营业时间的结构化来源；
小红书/抖音/大众点评对个人开发者均无公开内容接口，因此不做爬取，改用 LLM 联网归纳（evidence.py）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..knowledge.cities import CityInfo
from ..knowledge.poi_codes import AMAP_CATEGORY
from ..models.enums import Category
from ..models.poi import POIQuery, POIRecord
from .base import Provider, ProviderError, provider


def _sig(params: dict[str, Any], secret: str) -> str:
    """高德「数字签名」：参数按 key 升序拼接 + 安全密钥 取 MD5（未开启签名校验时忽略）。"""
    import urllib.parse

    items = sorted((k, v) for k, v in params.items() if v not in (None, ""))
    raw = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in items) + secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


@provider("poi", "amap")
class AMapPOI(Provider):
    def available(self) -> bool:
        return bool(self.settings.amap_key)

    def describe(self) -> tuple[bool, str]:
        if not self.settings.amap_key:
            return False, "未配置 AMAP_KEY（Web 服务 Key）"
        return True, "高德 v5 关键字/周边搜索（含评分、人均、营业时间）"

    def search(self, city: CityInfo, query: POIQuery) -> list[POIRecord]:
        types, kw_defaults = AMAP_CATEGORY.get(query.category or Category.SIGHT, ("110000", ""))
        keywords = ",".join(query.keywords) if query.keywords else kw_defaults
        params: dict[str, Any] = {
            "key": self.settings.amap_key,
            "types": types,
            "region": city.adcode or city.name,
            "citylimit": "true",
            "page_size": min(max(query.limit, 1), 25),
            "page_num": 1,
            "show_fields": "business,children,photos",
        }
        if query.category in (Category.FOOD, Category.BAR, Category.KTV, Category.AMUSEMENT, Category.SHOW):
            # 口碑优先：评分高的先出，避免把连锁便利店排进"必吃榜"
            params["sort_rule"] = 0
        if keywords:
            params["keywords"] = keywords
        if query.near is not None and query.radius_m > 0:
            params["location"] = f"{query.near.lng:.6f},{query.near.lat:.6f}"

        endpoint = "/v5/place/around" if "location" in params else "/v5/place/text"
        if self.settings.amap_secret:
            params["sig"] = _sig({k: v for k, v in params.items() if k != "sig"}, self.settings.amap_secret)

        def fetch() -> list[POIRecord]:
            data = self.get_json(f"https://restapi.amap.com{endpoint}", params)
            if not data:
                raise ProviderError("高德无响应")
            if str(data.get("status")) != "1":
                raise ProviderError(f"高德返回 {data.get('info')}({data.get('infocode')})")
            rows = data.get("pois") or []
            return [rec for rec in (_amap_to_poi(r, city, query.category) for r in rows) if rec and not rec.closed]

        key_parts = ("poi", "amap", "v2", city.key, params.get("types"), params.get("keywords"), params.get("location"), params["page_size"])
        return self.cached_call(key_parts, fetch, POIRecord)


CLOSED_MARKERS = ("暂停营业", "停业", "歇业", "装修中", "关闭", "暂不开放", "暂停开放", "已搬迁")


def amap_photos(row: dict, limit: int = 4) -> list[str]:
    """从高德返回体里取公开实拍图 URL。

    为什么用高德图而不是小红书/抖音：后两家对个人开发者没有内容接口，
    爬图既违反 ToS 又有侵权风险；高德 POI 图是"我们已授权的接口"返回的公开商户/景点图，
    合法、稳定，且和这条推荐是同一个地点，不会拿风景照糊弄用户。
    """
    out: list[str] = []
    for p in row.get("photos") or []:
        if isinstance(p, dict):
            url = str(p.get("url") or "").strip()
            if url.startswith("http"):
                out.append(url.replace("http://", "https://", 1))
    single = row.get("photo")
    if isinstance(single, str) and single.startswith("http"):
        out.append(single.replace("http://", "https://", 1))
    seen: set[str] = set()
    deduped = [u for u in out if not (u in seen or seen.add(u))]
    return deduped[:limit]


def detect_closed(*texts: str) -> str:
    """返回命中的停业标记词，没命中返回空串。

    高德会把「阿勒泰冰雪主题乐园(暂停营业)」这种状态直接写进 name，
    不识别就会排出一条根本去不了的行程，而且推荐语还会正经地写"上午十点入园"。
    """
    blob = " ".join(x for x in texts if x)
    for marker in CLOSED_MARKERS:
        if marker in blob:
            return marker
    return ""


def _amap_to_poi(row: dict, city: CityInfo, category: Category | None) -> POIRecord | None:
    name = (row.get("name") or "").strip()
    if not name:
        return None
    biz = row.get("business") or row.get("biz_info") or {}
    if not isinstance(biz, dict):
        biz = {}
    loc = row.get("location") or ""
    lat = lng = None
    if "," in loc:
        try:
            lng_s, lat_s = loc.split(",")[:2]
            lng, lat = float(lng_s), float(lat_s)
        except ValueError:
            pass
    code = str(row.get("typecode") or "")
    cat = category or _category_from_typecode(code)
    opentime = str(biz.get("opentime") or biz.get("open_time") or row.get("opentime_week") or "")
    rating = _to_float(biz.get("rating") or row.get("rating"))
    cost = _to_float(biz.get("cost") or biz.get("avg_price"))
    tag_text = " ".join(
        str(v) for v in (biz.get("tag"), biz.get("keytag"), row.get("type")) if isinstance(v, str)
    )
    rec = POIRecord(
        id=f"amap:{row.get('id') or hashlib.md5(name.encode()).hexdigest()[:12]}",
        name=name,
        category=cat,
        city=city.name,
        district=str(row.get("adname") or ""),
        address=str(row.get("address") if isinstance(row.get("address"), str) else ""),
        lat=lat,
        lng=lng,
        rating=rating,
        avg_cost_cents=None if cost is None else int(round(cost * 100)),
        open_hours_raw=opentime,
        indoor=cat in (Category.MUSEUM, Category.CINEMA, Category.KTV, Category.ESCAPE, Category.SHOPPING, Category.SPA, Category.CAFE),
        stay_minutes=_default_stay(cat),
        tags=_split_tags(tag_text),
        why=_clean_why(biz.get("tag")),
        photos=amap_photos(row),
        popularity=0.5 if rating is None else min(1.0, rating / 5.0),
        source="amap",
        external_id=str(row.get("id") or ""),
        confidence=0.85,
        raw={k: v for k, v in row.items() if k in ("typecode", "tel", "photo", "biz_ext", "distance")},
    )
    rec.closed = bool(detect_closed(name, str(row.get("type") or ""), str(biz.get("tag") or "")))
    _apply_open_hours(rec, opentime)
    return rec


@provider("poi", "fixture")
class FixturePOI(Provider):
    """离线/无 key 模式的数据源，同时也是 AMap 失败时的兜底。"""

    def search(self, city: CityInfo, query: POIQuery) -> list[POIRecord]:
        doc = _load_city_doc(self.settings.fixture_dir, city.key)
        if not doc:
            return []
        rows = doc.get("pois") or []
        out: list[POIRecord] = []
        for r in rows:
            try:
                rec = POIRecord.model_validate(r)
            except Exception:
                continue
            if not _match(rec, query):
                continue
            if rec.closed or detect_closed(rec.name, rec.why, *rec.tags):
                continue
            out.append(rec)
        if query.min_rating is not None:
            filtered = [p for p in out if (p.rating or 0) >= query.min_rating]
            out = filtered or out
        out.sort(key=lambda p: (-(p.rating or p.popularity * 5), -p.popularity))
        return out[: max(query.limit, 1)]


def _load_city_doc(fixture_dir, city_key: str) -> dict:
    path = fixture_dir / f"{city_key}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _match(rec: POIRecord, query: POIQuery) -> bool:
    if query.category and rec.category != query.category:
        return False
    if query.tags:
        blob = " ".join([rec.name, *rec.tags, rec.why, rec.best_time]).lower()
        if not any(t.lower() in blob for t in query.tags):
            return False
    if query.keywords:
        blob = " ".join([rec.name, *rec.tags, rec.category.label, rec.why]).lower()
        if not any(k.lower() in blob for k in query.keywords):
            return False
    return True


def _category_from_typecode(code: str) -> Category:
    """按高德 POI 编码归类，最长前缀优先。

    不能直接用 AMAP_CATEGORY 反查：080302(KTV) 与 080304(酒吧) 共享 0803 前缀，
    粗粒度匹配会把 KTV 认成酒吧、把 080504 露营地认成主题乐园。
    """
    if not code:
        return Category.SIGHT
    code = code.split("|")[0].strip()
    for size in (6, 4, 2):
        hit = _TYPECODE_PREFIX.get(code[:size])
        if hit:
            return hit
    return Category.SIGHT


# 高德 POI 分类编码 → 本项目类目（编码表：https://lbs.amap.com/api/webservice/download）
_TYPECODE_PREFIX: dict[str, Category] = {
    "110100": Category.PARK, "110101": Category.PARK, "110102": Category.PARK,
    "110201": Category.HISTORIC, "110202": Category.SIGHT, "110203": Category.HISTORIC, "110204": Category.HISTORIC,
    "110205": Category.HISTORIC, "110209": Category.HISTORIC,
    "110000": Category.SIGHT, "110200": Category.SIGHT, "1102": Category.SIGHT, "1101": Category.PARK, "11": Category.SIGHT,
    "050500": Category.CAFE, "0505": Category.CAFE,
    "050100": Category.FOOD, "050200": Category.FOOD, "050300": Category.FOOD, "050400": Category.FOOD,
    "050000": Category.FOOD, "05": Category.FOOD,
    "080302": Category.KTV, "080303": Category.CLUB, "080304": Category.BAR, "080308": Category.ESCAPE,
    "080301": Category.ESCAPE, "0803": Category.ESCAPE,
    "080501": Category.AMUSEMENT, "080502": Category.AMUSEMENT, "080504": Category.SPORTS, "0805": Category.AMUSEMENT,
    "080601": Category.CINEMA, "080602": Category.SHOW, "080603": Category.SHOW, "0806": Category.SHOW,
    "080111": Category.SPORTS, "080112": Category.SPORTS, "080113": Category.SPORTS, "0801": Category.SPORTS,
    "080401": Category.SPA, "0804": Category.SPA, "0802": Category.SPORTS, "08": Category.SPORTS,
    "140100": Category.MUSEUM, "140200": Category.MUSEUM, "140300": Category.MUSEUM, "140000": Category.MUSEUM, "14": Category.MUSEUM,
    "071600": Category.SPA, "0716": Category.SPA,
    "061000": Category.SHOPPING, "060100": Category.SHOPPING, "060000": Category.SHOPPING, "06": Category.SHOPPING,
    "100000": Category.HOTEL, "100100": Category.HOTEL, "10": Category.HOTEL,
}


def _default_stay(cat: Category) -> int:
    return {
        Category.AMUSEMENT: 240,
        Category.SIGHT: 120,
        Category.MUSEUM: 105,
        Category.HISTORIC: 90,
        Category.PARK: 60,
        Category.FOOD: 75,
        Category.CAFE: 45,
        Category.BAR: 90,
        Category.KTV: 150,
        Category.CLUB: 120,
        Category.SHOW: 120,
        Category.CINEMA: 130,
        Category.ESCAPE: 150,
        Category.SPA: 180,
        Category.SHOPPING: 90,
        Category.SPORTS: 90,
        Category.HOTEL: 30,
    }.get(cat, 90)


def _to_float(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _split_tags(text: str) -> list[str]:
    parts = [p.strip() for p in text.replace("，", ",").replace("|", ",").split(",")]
    return [p for p in parts if p and len(p) <= 16][:8]


def _clean_why(tag: Any) -> str:
    if isinstance(tag, list):
        return "、".join(str(t) for t in tag[:3])
    return str(tag) if isinstance(tag, str) else ""


def _apply_open_hours(rec: POIRecord, raw: str) -> None:
    """把 '10:00-02:00' / '周一至周日 09:00-17:00' 解析成分钟区间；解析不出来就留空（不误杀）。"""
    from ..models.common import hhmm_to_minute

    if not raw:
        return
    import re

    m = re.search(r"(\d{1,2}[:：]\d{2})\s*[-~至]\s*(\d{1,2}[:：]\d{2})", raw)
    if not m:
        return
    lo, hi = hhmm_to_minute(m.group(1)), hhmm_to_minute(m.group(2))
    if lo is None or hi is None:
        return
    rec.open_from_minute, rec.open_to_minute = lo, hi
