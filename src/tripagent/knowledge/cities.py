"""城市知识库：内建常见旅游城市 + data/fixtures/cities_extra.json 可覆盖/扩充。

为什么必须自建坐标表：Open-Meteo 的中文 geocoding 会把「丽江」解析到湖南岳阳的同名村庄，
而高德需要 adcode。坐标与 adcode 是所有天气/POI/路径请求的前提，属于"脏数据源头"，
因此集中一处维护，二次开发扩表只需追加 JSON，不动代码。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_settings


@dataclass(frozen=True)
class CityInfo:
    key: str
    name: str
    adcode: str
    lat: float
    lng: float
    province: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)
    level: str = ""   # province / city / district / street（来自高德行政区划）
    path: str = ""    # 省>市>县 路径，用于给用户看"这个地名归谁管"


_BUILTIN: list[CityInfo] = [
    CityInfo("beijing", "北京", "110000", 39.90416, 116.407396, "北京", ("帝都", "bj"), ("历史", "博物馆", "夜生活")),
    CityInfo("shanghai", "上海", "310000", 31.230370, 121.473701, "上海", ("魔都", "sh"), ("购物", "夜生活", "市井")),
    CityInfo("guangzhou", "广州", "440100", 23.129110, 113.264370, "广东", ("羊城", "穗"), ("美食", "市井")),
    CityInfo("shenzhen", "深圳", "440300", 22.543090, 114.057870, "广东", ("鹏城",), ("购物", "主题乐园")),
    CityInfo("chengdu", "成都", "510100", 30.572800, 104.066800, "四川", ("蓉城", "天府"), ("美食", "夜生活", "市井", "熊猫")),
    CityInfo("chongqing", "重庆", "500000", 29.563000, 106.551600, "重庆", ("渝", "山城"), ("美食", "网红打卡", "夜生活")),
    CityInfo("hangzhou", "杭州", "330100", 30.274100, 120.155100, "浙江", ("杭城", "西湖城"), ("山水", "市井")),
    CityInfo("xian", "西安", "610100", 34.341600, 108.939800, "陕西", ("长安", "xa"), ("历史", "美食", "夜生活")),
    CityInfo("nanjing", "南京", "320100", 32.060300, 118.796900, "江苏", ("金陵", "宁"), ("历史", "博物馆")),
    CityInfo("wuhan", "武汉", "420100", 30.592800, 114.305500, "湖北", ("江城", "汉"), ("历史", "美食")),
    CityInfo("changsha", "长沙", "430100", 28.227800, 112.938800, "湖南", ("星城", "长沙"), ("美食", "夜生活", "网红打卡")),
    CityInfo("suzhou", "苏州", "320500", 31.298900, 120.585200, "江苏", ("姑苏", "园林城"), ("历史", "市井")),
    CityInfo("qingdao", "青岛", "370200", 36.067100, 120.382600, "山东", ("岛城", "qd"), ("山水", "啤酒", "夜生活")),
    CityInfo("dalian", "大连", "210200", 38.914000, 121.614700, "辽宁", ("滨城",), ("山水",)),
    CityInfo("shenyang", "沈阳", "210100", 41.805700, 123.431500, "辽宁", ("盛京",), ("历史",)),
    CityInfo("harbin", "哈尔滨", "230100", 45.803800, 126.535000, "黑龙江", ("冰城", "hrb"), ("冰雪", "异域风情")),
    CityInfo("changchun", "长春", "220100", 43.817100, 125.323500, "吉林", (), ("历史",)),
    CityInfo("tianjin", "天津", "120000", 39.125500, 117.190300, "天津", ("津门",), ("市井", "美食")),
    CityInfo("zhengzhou", "郑州", "410100", 34.746600, 113.625400, "河南", ("豫",), ("历史",)),
    CityInfo("luoyang", "洛阳", "410300", 34.618600, 112.454000, "河南", ("神都", "洛城"), ("历史", "牡丹")),
    CityInfo("kaifeng", "开封", "410200", 34.797000, 114.307500, "河南", ("汴京",), ("历史", "夜市")),
    CityInfo("taiyuan", "太原", "140100", 37.870600, 112.548900, "山西", ("并州",), ("历史",)),
    CityInfo("pingyao", "平遥", "140728", 37.205800, 112.183000, "山西", ("平遥古城",), ("历史", "古城")),
    CityInfo("jinan", "济南", "370100", 36.651200, 117.120100, "山东", ("泉城",), ("山水",)),
    CityInfo("yantai", "烟台", "370600", 37.463800, 121.447900, "山东", (), ("山水", "海滨")),
    CityInfo("weihai", "威海", "371000", 37.509000, 122.118300, "山东", (), ("海滨",)),
    CityInfo("qinhuangdao", "秦皇岛", "130300", 39.935400, 119.599600, "河北", ("北戴河",), ("海滨",)),
    CityInfo("shijiazhuang", "石家庄", "130100", 38.042800, 114.514900, "河北", ("正定",), ("历史",)),
    CityInfo("wulumuqi", "乌鲁木齐", "650100", 43.825600, 87.616800, "新疆", ("乌市",), ("美食", "异域风情")),
    CityInfo("dunhuang", "敦煌", "620982", 40.142200, 94.661900, "甘肃", ("沙州",), ("历史", "大漠")),
    CityInfo("lanzhou", "兰州", "620100", 36.061100, 103.834300, "甘肃", ("金城",), ("美食",)),
    CityInfo("yinchuan", "银川", "640100", 38.487200, 106.230900, "宁夏", (), ("大漠", "历史")),
    CityInfo("xining", "西宁", "630100", 36.617100, 101.778200, "青海", (), ("高原", "藏文化")),
    CityInfo("lhasa", "拉萨", "540100", 29.650000, 91.100000, "西藏", ("圣城",), ("高原", "藏文化")),
    CityInfo("nyingchi", "林芝", "540400", 29.654700, 94.362300, "西藏", ("尼木",), ("高原", "山水")),
    CityInfo("kunming", "昆明", "530100", 25.038900, 102.718300, "云南", ("春城",), ("山水", "美食", "花卉")),
    CityInfo("lijiang", "丽江", "530700", 26.872100, 100.229900, "云南", ("丽江古城",), ("古城", "高原", "夜生活")),
    CityInfo("dali", "大理", "532901", 25.606500, 100.267600, "云南", ("大理古城", "下关"), ("古城", "山水", "市井")),
    CityInfo("shangrila", "香格里拉", "533401", 27.821400, 99.707100, "云南", ("中甸",), ("高原", "藏文化")),
    CityInfo("tengchong", "腾冲", "530522", 25.020300, 98.497100, "云南", (), ("温泉", "历史")),
    CityInfo("guiyang", "贵阳", "520100", 26.647000, 106.630200, "贵州", ("筑城", "爽爽的贵阳"), ("美食", "山水")),
    CityInfo("zunyi", "遵义", "520300", 27.726000, 106.927600, "贵州", (), ("红色旅游",)),
    CityInfo("nanning", "南宁", "450100", 22.817000, 108.366500, "广西", ("邕城",), ("美食", "山水")),
    CityInfo("guilin", "桂林", "450300", 25.273600, 110.290000, "广西", ("漓江", "阳朔"), ("山水",)),
    CityInfo("yangshuo", "阳朔", "450332", 24.778700, 110.496400, "广西", (), ("山水", "夜生活")),
    CityInfo("beihai", "北海", "450500", 21.481600, 109.120100, "广西", ("银滩",), ("海滨",)),
    CityInfo("haikou", "海口", "460100", 20.044400, 110.199900, "海南", ("椰城",), ("海滨", "美食")),
    CityInfo("sanya", "三亚", "460200", 18.252800, 109.511700, "海南", ("鹿城",), ("海滨", "亲子", "度假")),
    CityInfo("xiamen", "厦门", "350200", 24.479800, 118.089400, "福建", ("鹭岛", "思明"), ("海滨", "市井", "文艺")),
    CityInfo("fuzhou", "福州", "350100", 26.074500, 119.296500, "福建", ("榕城",), ("历史", "美食")),
    CityInfo("quanzhou", "泉州", "350500", 24.874100, 118.675700, "福建", ("刺桐",), ("历史", "市井")),
    CityInfo("wuyi", "武夷山", "350782", 27.676600, 117.983700, "福建", ("武夷",), ("山水", "茶")),
    CityInfo("ningbo", "宁波", "330200", 29.868300, 121.544000, "浙江", ("甬城",), ("历史", "美食")),
    CityInfo("wenzhou", "温州", "330300", 28.000600, 120.699400, "浙江", ("鹿城",), ("山水", "购物")),
    CityInfo("huzhou", "湖州", "330500", 30.892700, 120.086800, "浙江", ("南浔", "莫干山"), ("山水", "市井")),
    CityInfo("wuxi", "无锡", "320200", 31.491800, 120.311900, "江苏", ("锡城",), ("山水",)),
    CityInfo("yangzhou", "扬州", "321000", 32.394700, 119.412600, "江苏", ("广陵",), ("历史", "市井")),
    CityInfo("hefei", "合肥", "340100", 31.820600, 117.227200, "安徽", ("庐州",), ("历史",)),
    CityInfo("huangshan", "黄山", "341000", 29.714600, 118.337500, "安徽", ("徽州", "屯溪", "宏村"), ("山水", "古城")),
    CityInfo("wuyuan", "婺源", "361121", 29.268700, 117.862900, "江西", (), ("山水", "花卉")),
    CityInfo("nanchang", "南昌", "360100", 28.682900, 115.857900, "江西", ("洪城",), ("历史",)),
    CityInfo("zhangjiajie", "张家界", "430800", 29.117000, 110.479000, "湖南", ("大庸",), ("山水", "户外")),
    CityInfo("fenghuang", "凤凰", "433101", 27.947200, 109.598600, "湖南", ("凤凰古城", "吉首"), ("古城", "夜生活")),
    CityInfo("enshi", "恩施", "422800", 30.272100, 109.489700, "湖北", ("恩施大峡谷",), ("山水", "少数民族")),
    CityInfo("yichang", "宜昌", "420500", 30.691900, 111.286400, "湖北", ("三峡",), ("山水",)),
    CityInfo("foshan", "佛山", "440600", 23.021800, 113.121900, "广东", ("禅城", "顺德"), ("美食", "武术文化")),
    CityInfo("zhuhai", "珠海", "440400", 22.270700, 113.576700, "广东", (), ("海滨", "亲子")),
    CityInfo("dongguan", "东莞", "441900", 23.020700, 113.751800, "广东", (), ("购物",)),
    CityInfo("chengde", "承德", "130800", 40.959700, 117.963200, "河北", ("避暑山庄",), ("历史", "山水")),
    CityInfo("hohhot", "呼和浩特", "150100", 40.842400, 111.749000, "内蒙古", ("呼市",), ("草原", "少数民族")),
    CityInfo("hulunbuir", "呼伦贝尔", "150700", 49.212200, 119.771500, "内蒙古", ("海拉尔",), ("草原", "户外")),
]

_INDEX: dict[str, CityInfo] = {}
_ALIASES: dict[str, CityInfo] = {}


def _build_index() -> None:
    for info in _BUILTIN:
        _INDEX[info.key] = info
        for token in (info.name, info.key, *info.aliases):
            _ALIASES[token.lower()] = info


_build_index()


def _load_extra() -> None:
    """人工扩充表 + 动态解析表 + 全国行政区划表，三者格式兼容。

    divisions.json 由 scripts/build_divisions.py 生成（34 省 / 416 市 / 3614 区县，
    含常用简称别名），让「阿勒泰」「泰宁」「凤凰县」这类地名离线就能命中。
    """
    base = get_settings().data_dir / "fixtures"
    for fname in ("divisions.json", "cities_extra.json", "cities_dynamic.json"):
        path: Path = base / fname
        if not path.exists():
            continue
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for row in rows if isinstance(rows, list) else []:
            try:
                info = CityInfo(
                    key=row["key"],
                    name=row["name"],
                    adcode=str(row.get("adcode", "")),
                    lat=float(row["lat"]),
                    lng=float(row["lng"]),
                    province=row.get("province", ""),
                    aliases=tuple(row.get("aliases", ())),
                    tags=tuple(row.get("tags", ())),
                    level=row.get("level", ""),
                    path=row.get("path", ""),
                )
            except Exception:
                continue
            _INDEX.setdefault(info.key, info)
            for token in (info.name, info.key, *info.aliases):
                _ALIASES.setdefault(str(token).lower(), info)


def register_dynamic(info: CityInfo) -> None:
    """把高德地理编码解析出来的城市写回索引并落盘，下次不再联网。"""
    _INDEX[info.key] = info
    for token in (info.name, info.key, *info.aliases):
        _ALIASES[str(token).lower()] = info
    path = get_settings().data_dir / "fixtures" / "cities_dynamic.json"
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        if not isinstance(existing, list):
            existing = []
        if any(isinstance(e, dict) and e.get("key") == info.key for e in existing):
            return
        existing.append(
            {
                "key": info.key,
                "name": info.name,
                "adcode": info.adcode,
                "lat": round(info.lat, 5),
                "lng": round(info.lng, 5),
                "province": info.province,
                "aliases": list(info.aliases),
                "tags": list(info.tags),
            }
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass  # 落盘失败不影响本次使用


_load_extra()
_extra_loaded = True


def all_cities() -> list[CityInfo]:
    return sorted(_INDEX.values(), key=lambda c: c.name)


def get_city(key: str) -> CityInfo | None:
    return _INDEX.get((key or "").lower())


_LEVEL_RANK = {"province": 0, "city": 1, "district": 2, "street": 3, "": 1}


def resolve_city(text: str) -> CityInfo | None:
    """从任意自由文本里认出城市/县。

    匹配优先级是「**句中最早出现** → 行政级别（省>市>县>乡镇）→ 名称更长」，
    不是单纯最长匹配。原因很实际：石嘴山市有个真地名「吃火锅乡」，
    用户说"成都 3天 爱吃火锅"时最长匹配会把目的地劫持成石嘴山。
    旅行目的地几乎总在句子开头，位置优先才是对的。
    """
    if not text:
        return None
    needle = text.strip().lower()
    exact = _ALIASES.get(needle)
    if exact:
        return exact
    best: tuple[int, int, int, CityInfo] | None = None
    for token, info in _ALIASES.items():
        if len(token) < 2:
            continue
        pos = needle.find(token)
        if pos < 0:
            continue
        rank = _LEVEL_RANK.get(getattr(info, "level", "") or "", 1)
        key = (pos, rank, -len(token))
        if best is None or key < best[:3]:
            best = (*key, info)
    return best[3] if best else None
