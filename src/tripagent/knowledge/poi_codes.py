"""类目 ↔ 高德 POI 分类编码 ↔ 兴趣标签的三向映射。

「推荐酒吧 / KTV / 游乐园」这类 LifeHack 问题，本质上就是把中文口语翻成
高德 type 码 + keywords。码表来源：高德 POI 分类编码表
（https://lbs.amap.com/api/webservice/download → Amap_poicode.zip）。
没有 key 时同一份 keywords 也用于本地 fixture 检索，保证两种模式行为一致。
"""

from __future__ import annotations

from ..models.enums import Category, Interest

# (高德 types 编码，可用 | 连接; 高德 keywords 兜底词)
AMAP_CATEGORY: dict[Category, tuple[str, str]] = {
    Category.SIGHT: ("110000|110200|110202", "风景名胜"),
    Category.PARK: ("110100|110101", "公园"),
    Category.HISTORIC: ("110200|110202", "古镇,寺庙,老街"),
    Category.MUSEUM: ("140100|140000", "博物馆,美术馆,展览馆"),
    Category.FOOD: ("050000|050100", "美食,特色餐厅"),
    Category.CAFE: ("050500", "咖啡,茶馆"),
    Category.BAR: ("080304|080300", "酒吧,小酒馆,livehouse"),
    Category.KTV: ("080302|080300", "KTV,量贩式KTV"),
    Category.CLUB: ("080303|080300", "夜店,俱乐部"),
    Category.SHOW: ("080603|080602", "剧场,音乐厅,脱口秀,演出"),
    Category.CINEMA: ("080601", "电影院"),
    Category.AMUSEMENT: ("080501|080500", "游乐园,主题乐园"),
    Category.SPORTS: ("080111|080100", "健身中心,游泳馆,攀岩"),
    Category.ESCAPE: ("080300", "密室逃脱,剧本杀,桌游"),
    Category.SPA: ("080300|070000", "温泉,洗浴,足疗"),
    Category.SHOPPING: ("060000|061000", "购物中心,特色商业街,夜市"),
    Category.HOTEL: ("100000|100100", "酒店,民宿"),
}

# 兴趣 → 想检索的类目（顺序即优先级）
INTEREST_TO_CATEGORIES: dict[Interest, tuple[Category, ...]] = {
    Interest.FOOD: (Category.FOOD, Category.CAFE, Category.SHOPPING),
    Interest.HISTORY: (Category.SIGHT, Category.MUSEUM, Category.HISTORIC),
    Interest.NATURE: (Category.SIGHT, Category.PARK),
    Interest.PHOTO: (Category.SIGHT, Category.HISTORIC, Category.SHOPPING),
    Interest.NIGHTLIFE: (Category.BAR, Category.KTV, Category.CLUB, Category.SHOW),
    Interest.FAMILY: (Category.AMUSEMENT, Category.PARK, Category.MUSEUM),
    Interest.THEME_PARK: (Category.AMUSEMENT,),
    Interest.SHOWS: (Category.SHOW, Category.MUSEUM, Category.CINEMA),
    Interest.SHOPPING: (Category.SHOPPING, Category.HISTORIC),
    Interest.LOCAL_LIFE: (Category.PARK, Category.CAFE, Category.HISTORIC, Category.FOOD),
    Interest.OUTDOOR: (Category.SIGHT, Category.SPORTS, Category.PARK),
    Interest.THERMAL: (Category.SPA, Category.SIGHT),
    Interest.ACCESSIBLE: (Category.MUSEUM, Category.PARK, Category.SIGHT),
    Interest.PET: (Category.PARK, Category.CAFE, Category.SIGHT),
    Interest.BUDGET: (Category.PARK, Category.HISTORIC, Category.FOOD),
}

# 类目 → 时段权重（夜晚类目只在夜间段被考虑，避免把酒吧排进上午）
DAY_LONG_CATEGORIES = (Category.SIGHT, Category.PARK, Category.HISTORIC, Category.MUSEUM, Category.AMUSEMENT, Category.SPORTS, Category.SHOPPING, Category.SPA)
NIGHT_CATEGORIES = (Category.BAR, Category.KTV, Category.CLUB, Category.SHOW, Category.CINEMA, Category.ESCAPE)
MEAL_CATEGORIES = (Category.FOOD, Category.CAFE)

# 中文类目别名，供自由文本解析（词 → 类目 的扁平查表）
from ..models.enums import CATEGORY_ALIASES as _CAT_GROUPS  # noqa: E402
from ..models.enums import INTEREST_ALIASES as _INT_GROUPS  # noqa: E402

CATEGORY_ALIASES: dict[str, Category] = {}
for _cat in Category:
    CATEGORY_ALIASES[_cat.value] = _cat
    for _alias in _CAT_GROUPS.get(_cat, ()):
        CATEGORY_ALIASES[_alias.lower()] = _cat
CATEGORY_ALIASES.update({
    "玩": Category.SIGHT,
    "景点": Category.SIGHT,
    "夜": Category.BAR,
    "唱": Category.KTV,
    "演出": Category.SHOW,
    "看演出": Category.SHOW,
})

INTEREST_ALIASES: dict[str, Interest] = {}
for _it in Interest:
    INTEREST_ALIASES[_it.value] = _it
    for _alias in _INT_GROUPS.get(_it, ()):
        INTEREST_ALIASES[_alias.lower()] = _it


def categories_for(interests: list[Interest]) -> list[Category]:
    out: list[Category] = []
    for it in interests:
        for cat in INTEREST_TO_CATEGORIES.get(it, ()):
            if cat not in out:
                out.append(cat)
    if not out:
        out = [Category.SIGHT, Category.FOOD, Category.HISTORIC]
    return out


def match_category(text: str) -> Category | None:
    """口语类目识别：'酒吧' → Category.BAR，'唱K' → Category.KTV。"""
    needle = (text or "").strip().lower()
    if not needle:
        return None
    if needle in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[needle]
    for alias, cat in sorted(CATEGORY_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if len(alias) >= 2 and alias in needle:
            return cat
    for alias, cat in CATEGORY_ALIASES.items():
        if len(alias) == 1 and alias in needle:
            return cat
    return None
