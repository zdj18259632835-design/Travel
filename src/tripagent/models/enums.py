"""枚举与取值约定：所有模块共用一套语义，避免各处硬编码中文关键词。

约定：value 直接就是中文展示名（str Enum），因此
- 可以原样写进 data/fixtures/*.json（"category": "酒吧酒馆"）；
- 可以原样进 JSON 缓存并回读；
- 口语别名通过 _ALIAS_MAP 反查（"ktv"、"唱K" → Category.KTV）。
"""

from __future__ import annotations

from enum import Enum


class Category(str, Enum):
    """POI 大类。与高德 POI 分类编码一一对应（见 knowledge/poi_codes.py）。"""

    SIGHT = "风景名胜"
    MUSEUM = "博物馆展览馆"
    PARK = "公园广场"
    HISTORIC = "人文古迹"
    FOOD = "特色餐饮"
    CAFE = "咖啡茶饮"
    BAR = "酒吧酒馆"
    KTV = "KTV歌厅"
    CLUB = "夜店夜场"
    SHOW = "剧场演出"
    CINEMA = "影院"
    AMUSEMENT = "主题乐园"
    SPORTS = "运动休闲"
    ESCAPE = "密室与沉浸娱乐"
    SPA = "温泉洗浴"
    SHOPPING = "购物街区"
    HOTEL = "住宿"

    @property
    def label(self) -> str:
        return self.value

    @property
    def group(self) -> str:
        """归入行程时段分组：白天玩 / 吃 / 夜生活。"""
        if self in _NIGHT:
            return "night"
        if self in _FOOD:
            return "food"
        return "day"

    @property
    def inherently_indoor(self) -> bool:
        """这些类目在现实中几乎都在室内；数据源漏填时由类目兜底推导。"""
        return self in _INDOOR

    @classmethod
    def _missing_(cls, value: object) -> "Category | None":
        return _resolve(cls, value)


_NIGHT = (Category.BAR, Category.KTV, Category.CLUB, Category.SHOW, Category.CINEMA, Category.ESCAPE)
_FOOD = (Category.FOOD, Category.CAFE)
# SHOPPING 不列进来：夜市/市集是露天的，交由数据自身表达
_INDOOR = (Category.MUSEUM, Category.CAFE, Category.BAR, Category.KTV, Category.CLUB,
           Category.SHOW, Category.CINEMA, Category.ESCAPE, Category.SPA, Category.HOTEL)


class Interest(str, Enum):
    """用户兴趣标签，同时是 POI 打分维度与偏好画像的最小单元。"""

    FOOD = "美食探店"
    HISTORY = "历史人文"
    NATURE = "山水自然"
    PHOTO = "网红打卡"
    NIGHTLIFE = "夜生活"
    FAMILY = "亲子遛娃"
    THEME_PARK = "主题乐园"
    SHOWS = "展演活动"
    SHOPPING = "购物血拼"
    LOCAL_LIFE = "市井慢生活"
    OUTDOOR = "户外徒步"
    THERMAL = "温泉养生"
    ACCESSIBLE = "无障碍友好"
    PET = "携宠出行"
    BUDGET = "预算敏感"

    @property
    def label(self) -> str:
        return self.value

    @classmethod
    def _missing_(cls, value: object) -> "Interest | None":
        return _resolve(cls, value)


class SlotType(str, Enum):
    TRANSPORT = "交通移动"
    SIGHT = "游览"
    MEAL = "用餐"
    SHOW = "观看演出"
    NIGHTLIFE = "夜间娱乐"
    REST = "休息缓冲"
    CHECKIN = "入住退房"
    FREE = "自由活动"

    @property
    def label(self) -> str:
        return self.value

    @classmethod
    def _missing_(cls, value: object) -> "SlotType | None":
        return _resolve(cls, value)


class Severity(str, Enum):
    NONE = "适宜"
    WATCH = "关注"
    WARN = "警告"
    DANGER = "危险"

    @property
    def label(self) -> str:
        return self.value

    @property
    def rank(self) -> int:
        return {"NONE": 0, "WATCH": 1, "WARN": 2, "DANGER": 3}[self.name]

    @classmethod
    def _missing_(cls, value: object) -> "Severity | None":
        return _resolve(cls, value)


class Crowd(str, Enum):
    ADULT = "成人"
    KID = "儿童"
    ELDER = "老人"
    PET = "宠物"

    @property
    def label(self) -> str:
        return self.value

    @classmethod
    def _missing_(cls, value: object) -> "Crowd | None":
        return _resolve(cls, value)


class ConstraintKind(str, Enum):
    """约束分层沿用 TravelPlanner / ChinaTravel 的三分法，便于向评委解释"通过率"口径。"""

    ENV = "env"                     # 数据与环境事实
    COMMONSENSE = "commonsense"     # 常识：不折返、不重叠、时段合理
    HARD = "hard"                   # 用户硬约束：预算、人群禁忌、预约窗口、天气安全

    @property
    def label(self) -> str:
        return {"env": "环境事实", "commonsense": "常识约束", "hard": "硬约束"}[self.value]


# --------------------------------------------------------------------------
# 口语别名：用户不会说"酒吧酒馆"，他说"小酒馆 / livehouse / 清吧"
# --------------------------------------------------------------------------
CATEGORY_ALIASES: dict[Category, tuple[str, ...]] = {
    Category.SIGHT: ("景点", "景区", "名胜", "tourist", "好玩的地方", "必去"),
    Category.MUSEUM: ("博物馆", "展馆", "美术馆", "展览", "科技馆"),
    Category.PARK: ("公园", "广场", "绿地", "湿地公园"),
    Category.HISTORIC: ("古迹", "古镇", "古街", "寺庙", "老街", "城墙", "胡同"),
    Category.FOOD: ("美食", "餐厅", "小吃", "苍蝇馆子", "必吃", "饭店", "吃"),
    Category.CAFE: ("咖啡", "茶馆", "奶茶", "下午茶"),
    Category.BAR: ("酒吧", "小酒馆", "清吧", "livehouse", "livhouse", "精酿", "酒馆"),
    Category.KTV: ("ktv", "歌厅", "量贩", "唱k", "卡拉ok"),
    Category.CLUB: ("夜店", "club", "蹦迪", "disco", "嗨吧"),
    Category.SHOW: ("演出", "话剧", "脱口秀", "音乐会", "剧场", "演唱会", "相声", "戏曲", "音乐节"),
    Category.CINEMA: ("电影院", "影院", "movie", "看电影"),
    Category.AMUSEMENT: ("游乐园", "主题乐园", "游乐场", "迪士尼", "环球影城", "欢乐谷", "长隆", "乐园"),
    Category.SPORTS: ("健身房", "游泳", "攀岩", "滑雪", "漂流", "骑马", "蹦极"),
    Category.ESCAPE: ("密室", "剧本杀", "桌游", "escape", "沉浸式"),
    Category.SPA: ("温泉", "洗浴", "足疗", "汤泉", "汗蒸"),
    Category.SHOPPING: ("商圈", "购物中心", "夜市", "市集", "免税", "逛街", "买"),
    Category.HOTEL: ("酒店", "民宿", "客栈", "住宿", "青旅"),
}

INTEREST_ALIASES: dict[Interest, tuple[str, ...]] = {
    Interest.FOOD: ("吃", "探店", "火锅", "烧烤", "小吃", "美食家"),
    Interest.HISTORY: ("历史", "人文", "古迹", "博物馆", "文化", "非遗", "古城"),
    Interest.NATURE: ("自然", "山水", "山", "湖", "海", "森林", "草原", "风景", "公园", "冰雪", "冰雕", "冰灯", "雪景", "雾凇"),
    Interest.PHOTO: ("网红", "打卡", "拍照", "摄影", "出片", "ins风", "小视频"),
    Interest.NIGHTLIFE: ("夜生活", "酒吧", "夜店", "蹦迪", "ktv", "夜市", "凌晨", "livehouse"),
    Interest.FAMILY: ("亲子", "带娃", "小孩", "小朋友", "遛娃", "儿童"),
    Interest.THEME_PARK: ("乐园", "游乐园", "迪士尼", "环球影城", "欢乐谷", "长隆"),
    Interest.SHOWS: ("演出", "话剧", "脱口秀", "演唱会", "音乐节", "展览", "live"),
    Interest.SHOPPING: ("购物", "商圈", "免税", "逛街", "买"),
    Interest.LOCAL_LIFE: ("慢生活", "市井", "本地", "巷子", "茶馆", "休闲", "烟火气"),
    Interest.OUTDOOR: ("徒步", "露营", "骑行", "漂流", "滑雪", "攀岩", "滑冰", "雪地", "温泉滑雪"),
    Interest.THERMAL: ("温泉", "泡汤", "养生", "洗浴"),
    Interest.ACCESSIBLE: ("无障碍", "轮椅", "行动不便", "老人友好"),
    Interest.PET: ("宠物", "带狗", "带猫", "遛狗"),
    Interest.BUDGET: ("穷游", "省钱", "性价比", "平价", "便宜"),
}


def aliases_of(enum_cls: type, member: Enum) -> tuple[str, ...]:
    table = CATEGORY_ALIASES if enum_cls is Category else INTEREST_ALIASES if enum_cls is Interest else {}
    return table.get(member, ())  # type: ignore[arg-type]


def _resolve(cls: type, value: object) -> Enum | None:
    """枚举反查：接受展示名、成员名（大小写不敏感）或口语别名。"""
    if not isinstance(value, str):
        return None
    needle = value.strip()
    table = CATEGORY_ALIASES if cls is Category else INTEREST_ALIASES if cls is Interest else {}
    for member in cls:  # type: ignore[attr-defined]
        if member.value == needle or member.name.lower() == needle.lower():  # type: ignore[attr-defined]
            return member
    low = needle.lower()
    for member, alias in table.items():
        if low in [a.lower() for a in alias]:
            return member
    return None
