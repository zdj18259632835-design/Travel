# TripAgent 二次开发指南

> 7 个可照做的 Recipe：每个给出**改哪个文件、加什么代码、跑哪条验证命令**。动手前先扫一眼 [ARCHITECTURE.md](ARCHITECTURE.md) 的分层与数据契约；快速跑起来见 [../README.md](../README.md)。

命令都在项目根 `D:\APP_develop\qoder-agent-develop\travel-agent` 下执行，一律用项目内解释器。**回归三连**（下文用它指代收尾验证）：

```bat
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m tripagent.cli doctor
.venv\Scripts\python.exe -m tripagent.cli plan "成都 3天 11月20日出发 人均预算1500 爱吃火锅 想体验夜生活" --offline
```

## Recipe 1 · 新增一个数据源 provider

以心知天气（Seniverse）预报源为例，不动任何引擎代码。

**① `src/tripagent/config.py` 加配置项**（密钥区块 `qweather_host` 之后），并把新源放进链——**顺序即优先级**：

```python
    seniverse_key: str = Field(default="", alias="SENIVERSE_KEY")
    forecast_chain: str = "qweather,open_meteo,seniverse,fixture"
```

> 心知免费档只有 3 天预报（见 [DATA_SOURCES.md](DATA_SOURCES.md) §2.5），所以放 `open_meteo`（16 天）**之后**当备胎。不改源码也能调序：`.env` 或环境变量 `FORECAST_CHAIN=open_meteo,seniverse,fixture`（链字段**没有** `TRIPAGENT_` 前缀）。

**② 追加到 `src/tripagent/providers/weather.py` 末尾**。四个必做点：装饰器注册、`available()` 自检、`describe()` 供 doctor、`cached_call()` 走磁盘缓存。

```python
@provider("forecast", "seniverse")
class Seniverse(Provider):
    """心知天气 v3 每日预报。免费档仅 3 天，作为 open_meteo 的备胎。"""

    URL = "https://api.seniverse.com/v3/weather/daily.json"

    def available(self) -> bool:
        return bool(self.settings.seniverse_key) and self.settings.online

    def describe(self) -> tuple[bool, str]:
        if not self.settings.seniverse_key:
            return False, "未配置 SENIVERSE_KEY"
        return True, "心知天气每日预报（额度极小，仅备用源）"

    def fetch_forecast(self, city: CityInfo, days: int = 16) -> list[DayForecast]:
        params = {"location": f"{city.lat:.4f},{city.lng:.4f}", "start": "today",
                  "days": min(int(days), 3), "key": self.settings.seniverse_key, "language": "zh-Hans"}

        def fetch() -> list[DayForecast]:
            rows = ((self.get_json(self.URL, params) or {}).get("results") or [{}])[0].get("daily") or []
            out = [DayForecast(date=d, source="seniverse", text=r.get("text_day", "") or "未知",
                               text_en=r.get("code", ""), tmax_c=_safe_float(r.get("temperature_max")),
                               tmin_c=_safe_float(r.get("temperature_min")),
                               precip_mm=_safe_float(r.get("precipitation_max")) or 0.0,
                               wind_max_kmh=_safe_float(r.get("wind_speed_max")))
                   for r in rows for d in [_parse_date(r.get("date", ""))] if d]
            if not out:
                raise ProviderError("心知天气未返回 daily")
            return out

        return self.cached_call(("forecast", "seniverse", city.key, days), fetch, DayForecast)
```

复用的现成工具（别重写）：`_parse_date` / `_safe_float`（本文件）、`Provider.get_json`（**已自动注入 `User-Agent`**，CMA 必需）、`Provider.cached_call`（pydantic 模型能原样序列化进 `.cache` 并读回）。**方法名必须是 `fetch_forecast`** —— `Chain.run()` 按字符串找方法，写错不报错，只在 trace 留一条 `未实现 fetch_forecast`。

**③ 若是新建模块文件**（例如"大麦网演出" `providers/shows.py`），必须在 `providers/__init__.py` 补 `from . import shows as _shows  # noqa: F401`，否则装饰器根本不执行；新能力还要在 `config.py` 加一条链 `show_chain: str = "damai,fixture"`，调用处写 `Chain.run("show", "search", city, query, model=...)`，要被排程才需要碰 `core/`。

**④ 验证**

```bat
.venv\Scripts\python.exe scripts\probe_apis.py 成都
.venv\Scripts\python.exe -m tripagent.cli doctor
```

`probe_apis.py` 只探 `forecast / alarm / guide / llm` 四条链（POI/transit 要 key，没探）。看每个 attempt：未配 key 应出现 `✗ … 不可用（缺 key 或离线）` 并继续降级；额度用尽应出现 `HTTP 4xx` 并继续降级，而不是整链失败。往 `tests/test_providers.py` 补一条（`conftest.py` 已强制离线并清空 key）：

```python
def test_seniverse_needs_key(city):
    chain = Chain(get_settings())
    inst = chain._instances.get("forecast:seniverse")
    assert "seniverse" not in [n for n, _ in chain.providers_for("forecast")] or not inst.available()
```

## Recipe 2 · 新增一条行程约束

**位置**：`src/tripagent/core/validator.py`。一条约束 = 一行调用：

```python
def _add(report, cid, kind, name, passed, detail="", hint="", weight=1.0, related=None)
```

参数约定：`cid` 是稳定英文蛇形 id（测试按它断言、**不要改名**），`name` 才是中文展示文案；`kind` 取 `ConstraintKind.ENV / COMMONSENSE / HARD`（`models/enums.py`）；`passed` 在数据缺失时应判**通过**并在 `detail` 里说明（参考 `open_covers()` 返回 `None` 就放行）；`detail`/`hint` 只在未通过时被导出层打印；`related` 放关联 POI id 便于定位；**`weight` 只影响 `ValidationReport.overall`（加权通过率），不影响 `pass_rate()`（按条数算）**。

三分类语义与现有条数（合计 **18** 条，括注 weight）：

| kind | 含义 | 条数 | 代表（weight） |
|---|---|---|---|
| `ENV` | 数据与环境事实 | 4 | `city_resolved`(2.0) `geo_coverage`(1.0，覆盖率<0.7 判失败) `poi_source` `weather_coverage` |
| `COMMONSENSE` | 常识：不折返/不重叠/时段合理 | 8 | `no_time_overlap`(1.5) `route_efficiency`(1.5) `open_hours_respected`(1.5) `no_duplicate_poi`(1.2) `walk_leg_within_limit`(1.0) |
| `HARD` | 用户硬约束 | 6 + 1 条件项 | `budget_within`(3.0) `weather_adapted`(2.5) `kid_safe_venues`(2.0) `reservation_feasible`(1.5) ＋条件项 `nightlife_compliance`(1.0) |

只有 `HARD` 失败会让 `verdict()` 短路成"存在硬约束冲突，需要调整"（`hard_failed`）。

### 示例 A · 单段步行时长上限（已实现，当作写约束的标准范本照抄）

`validator._check_commonsense()` 里的 `walk_leg_within_limit` 消费了 `config.py` 的 `max_walk_minutes_per_leg=35`：

```python
            tb = s.transit_before
            if tb and tb.mode == "walk" and tb.minutes > st.max_walk_minutes_per_leg:
                long_walk.append(f"Day{d.index} 去「{s.title}」需步行 {tb.minutes} 分")
    _add(r, "walk_leg_within_limit", ConstraintKind.COMMONSENSE,
         f"单段步行 ≤{st.max_walk_minutes_per_leg} 分钟", not long_walk,
         "；".join(long_walk[:3]), "改选同街区候选，或把该段改成地铁/打车")
```

要点三条，加任何新约束都照这个套路：**① 在日/槽循环里只收集证据字符串，不在循环里 `_add`**（保证记分卡顺序稳定）；**② 阈值从 `Settings` 读，不要写死数字**（用户可在 `.env` 覆盖）；**③ `fix_hint` 必须是能执行的下一步**，而不是"请调整"。

> 通勤段在 `Slot.transit_before`（`RouteLeg.mode/minutes/distance_m/cost_cents/source`）；`mode` 由 `planner.DayAssembler._route()` 决定：直线 ≤1200m 走 `walk`，否则 `transit`。

### 示例 B · 必须含一次本地老字号 → 放 `hard`

在 `_check_hard()` 末尾（**先判 `plan.days` 非空**，否则空行程误判失败）：

```python
    if plan.days:
        hit = [s.title for _, s in _all_slots(plan) if s.poi and "老字号" in " ".join(s.poi.tags)]
        _add(r, "local_heritage_meal", ConstraintKind.HARD, "至少一家本地老字号", bool(hit),
             "" if hit else "整份行程没有命中「老字号」标签的店",
             "在 fixture 里为老店补 tags:[\"老字号\"]，或配置 AMAP_KEY 搜「老字号」", weight=1.5)
```

### 影响与验证

分母 +1 → 历史 `pass_rate()` 变小、`verdict()` 的 95%/80% 档线更易跌破；加完约束要同步更新 `README.md`、`docs/ARCHITECTURE.md` §4.9 与 `docs/COMPETITION.md` 里的条数口径（当前：env 4 / commonsense 8 / hard 6+1 条件项）。

```bat
.venv\Scripts\python.exe -m pytest tests/test_engine.py -q
.venv\Scripts\python.exe -m tripagent.cli plan "成都 3天 11月10日出发 爱吃火锅" --offline
```

CLI 末尾记分卡表里应出现新行。补断言到 `tests/test_engine.py`（`_chain` / `make_weather` / `validator` / `ItineraryPlanner` 该文件已导入）：

```python
def test_new_constraints_are_reported(poi_pool, city, request_basic):
    chain = _chain()
    w = make_weather(request_basic.start_date, request_basic.days)
    weather_risk.assess(w, [f.date for f in w.forecast])
    plan = ItineraryPlanner(city, request_basic, w, chain, get_settings()).plan(poi_pool)
    ids = {c.id for c in validator.validate(plan, chain, get_settings()).checks}
    assert {"walk_load_limit", "local_heritage_meal"} <= ids
```

## Recipe 3 · 新增一个娱乐类目

以「露营地」为例（高德码 `080504`，已核实）。「电竞馆」的差别写在括注里。**这类改动横跨 6 个文件，漏一个就表现成"检索得到但排不进行程"或"天气引擎把它当户外"。**

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `models/enums.py` | `Category` 成员 + `_NIGHT`/`_INDOOR` + `CATEGORY_ALIASES` 别名 |
| 2 | `knowledge/poi_codes.py` | `AMAP_CATEGORY` 编码与兜底 keywords + 时段分组（`DAY_LONG_CATEGORIES`/`NIGHT_CATEGORIES`）+ `INTEREST_TO_CATEGORIES` |
| 3 | `providers/poi.py` | `_default_stay`；`_category_from_typecode` 的前缀坑（见下） |
| 4 | `core/planner.py` | `_SLOT_TYPE`、`_POOL_BIAS`、`_theme()` 标签 |
| 5+6 | `core/validator.py` / `agents/advisor.py` | `_NIGHT_CATS` 夜间实名清单 / `_append_compliance_notes` 准入提示 |

**① `models/enums.py`**

```python
    CAMP = "露营地"          # str Enum：value 就是中文展示名，可直接写进 fixtures
    # ESPORTS = "电竞馆"     电竞馆要同时进 _NIGHT 和 _INDOOR；露营地两个都不进

CATEGORY_ALIASES = { ...
    Category.CAMP: ("露营", "营地", "帐篷", "camping"),
    Category.ESPORTS: ("电竞", "电竞馆", "网咖", "开黑"), }
```

`Category.group`（`night/food/day`）由 `_NIGHT`/`_FOOD` 推出，`inherently_indoor` 由 `_INDOOR` 推出，`POIRecord._derive_indoor` 据此兜底置真 `indoor`。露营地不在 `_INDOOR` → 恶劣天气日被 `scoring.score()` 扣 22 分、被 `_allow_outdoor()` 限制，正是想要的行为。`Category("露营地")`、`Category("露营")` 都能反查出来（`_missing_` → `_resolve` 支持展示名/成员名/别名）。

**② `knowledge/poi_codes.py`**

```python
    Category.CAMP: ("080504|080500", "露营地,营地,野外露营"),
    Category.ESPORTS: ("080300", "电竞馆,电竞酒店,网咖,桌游"),
```

并把它加进 `DAY_LONG_CATEGORIES`（白天档）或 `NIGHT_CATEGORIES`（夜间档）——**不进任何一个就等于永远排不进去**。`INTEREST_TO_CATEGORIES[Interest.OUTDOOR]` 加 `Category.CAMP`、`INTEREST_ALIASES` 已含"露营"。本文件末尾的扁平 `CATEGORY_ALIASES` 是 import 时由 ① 自动展开的，不用手工加。

> 二元组第二个元素是**中文兜底 keywords**；`types` 用 `|` 连接多个编码。没 key 时同一份 keywords 也被 `FixturePOI._match()` 用来筛本地数据，保证两种模式行为一致。`080304` 酒吧 / `080302` KTV / `080303` 迪厅 / `080501` 游乐场 / `080504` 露营地 / `080601` 影院 / `080603` 剧场都来自官方码表；**电竞馆没有独立公开小类，加它之前先用 `Amap_poicode.zip` 核实**，本项目对未核实的编码一律靠 keywords 兜底而不是猜码。

**③ `providers/poi.py`** — 停留时长 + 一个真实的坑

```python
    Category.CAMP: 300,      # _default_stay 表里加
    Category.ESPORTS: 180,
```

`_category_from_typecode()` 的第一轮循环把 6 位码**截成 4 位**比对（`t.split("|")[0][:4]`），并按 `AMAP_CATEGORY` 字典顺序先到先得。实测 `_category_from_typecode("080504")` 现在返回 `主题乐园`——因为 `080504` 与 `AMUSEMENT` 的 `080501|080500` 前 4 位同为 `0805`，而 `AMUSEMENT` 在前。**不要把 `CAMP` 挪到前面**，那只会让真游乐场被误判成露营地。正确修法是**先按完整 6 位精确匹配，再退 4 位**（在原函数开头插一轮）：

```python
    for cat, (types, _) in AMAP_CATEGORY.items():          # 新增：先精确到 6 位
        if any(t == code for t in types.split("|")):       #      080504≠080501
            return cat
    # 下面原有的 4 位前缀循环与 table 兜底保持不动
```

> 它只在 `query.category` 为空时生效（`cat = category or _category_from_typecode(code)`），走 `plan` 主链路时类目总显式传入，所以这个 bug 平时不暴露，一做"综合搜索"就错分。补测（`tests/test_providers.py`）：

```python
def test_typecode_080504_is_camp_not_amusement(city):
    row = {"id": "X1", "name": "西岭雪山营地", "location": "103.1,30.6", "typecode": "080504"}
    assert _amap_to_poi(row, city, None).category is Category.CAMP
    assert _amap_to_poi({**row, "id": "X2", "typecode": "080501"}, city, None).category is Category.AMUSEMENT
```

**④ `core/planner.py`**：`_SLOT_TYPE[Category.ESPORTS] = SlotType.NIGHTLIFE`（露营留默认 `SIGHT`）；`_POOL_BIAS` 加 `Category.CAMP: 0.95`（营地远，值得占一天但别抢锚点）；`_theme()` 的 label 字典**以 `Category` 成员为键**，要写 `Category.CAMP: "户外露营"`，否则落回 `"综合游览"`。

**⑤⑥ 准入**：电竞馆有实名与未成年限制，必须登记进 `validator._NIGHT_CATS`（现为 `(BAR, CLUB, KTV, ESCAPE)`），否则 `nightlife_compliance` 不为它触发身份证提醒；`advisor._append_compliance_notes()` 的类目清单同步。露营地无准入限制，这两处不动。

**验证**

```bat
.venv\Scripts\python.exe -m tripagent.cli ask "成都周边有哪些露营地 可以扎帐篷"
.venv\Scripts\python.exe -m tripagent.cli plan "成都 3天 露营 加一场夜生活 预算1200" --offline
.venv\Scripts\python.exe -m pytest -q
```

新类目在**没 fixture 也没 key** 时检索不到（`FixturePOI._match` 按 `category` 精筛）属正常，`plan` 的空池提示会指向 `data/fixtures/<city>.json`。

## Recipe 4 · 新增一个城市

两件事：**坐标进 `cities_extra.json`，内容进 `<key>.json`**。以重庆为例（坐标已在 `knowledge/cities.py` 内建，但无 fixture）。

### 4.1 `data/fixtures/cities_extra.json`（默认不存在，新建即可）

```json
[{ "key": "chongqing", "name": "重庆", "adcode": "500000",
   "lat": 29.563, "lng": 106.5516, "province": "重庆",
   "aliases": ["渝", "山城"], "tags": ["美食", "网红打卡", "夜生活"] }]
```

只有 `key/name/lat/lng` 必需（`knowledge/cities.py:_load_extra()`）。`key` 决定 `FixturePOI` 去读哪个 `data/fixtures/<key>.json`，也是缓存 key 的一部分；`adcode` 是高德 `region` 与预警省级过滤的依据；`lat/lng` 供预报、`RouteLeg.estimate` 与 SVG 路线图。**必须重启进程**：`_load_extra()` 在模块 import 时只跑一次，Streamlit 里改完不重启不生效。

### 4.2 `data/fixtures/chongqing.json`：顶层 4 个 key

`city`、`guide`（`FixtureGuide` 读）、`pois`（`FixturePOI` 读）、`evidence`（`FixtureEvidence` 读）。模型都是 `extra="allow"`，多余字段不报错，但**拼错的字段会被静默忽略**——这是"我的数据怎么没生效"的头号原因。

### 4.3 `POIRecord` 完整字段模板（名称逐一对应 `models/poi.py`；金额一律**分**）

```json
{
  "id": "fx:cq-hongya", "name": "洪崖洞民俗风貌区", "category": "风景名胜",
  "city": "重庆", "district": "渝中区", "address": "嘉陵江滨江路88号",
  "lat": 29.5630, "lng": 106.5770,
  "rating": 4.6, "review_count": 180000, "avg_cost_cents": 0, "ticket_cents": 0,
  "open_hours_raw": "11:00-23:00（夜景20:00后最佳）",
  "open_from_minute": 660, "open_to_minute": 1380, "closed_weekdays": [],
  "stay_minutes": 120, "indoor": false, "metro": "1号线小什字站",
  "need_reservation": false, "reservation_channel": "", "reservation_lead_days": null,
  "reservation_cutoff": "", "age_policy": "", "requires_id": false,
  "kid_friendly": true, "pet_friendly": null, "accessible": null,
  "tags": ["夜景", "网红打卡", "巴渝建筑"],
  "why": "11楼出门是江岸、1楼出门还是江岸的立体建筑，亮灯后才是正确打开方式",
  "pitfalls": ["内部商业街同质化，不建议为购物进去", "千厮门大桥桥头机位更好"],
  "photo_spots": ["千厮门大桥桥面", "对岸江北嘴江滩"],
  "best_time": "夜晚", "popularity": 0.95,
  "source": "fixture", "external_id": "", "confidence": 0.8,
  "evidence_ids": ["ev:cq-001"], "raw": {}
}
```

**5 条必须注意**：

1. **`lat/lng` 要给**。缺了它 `geo_coverage`（阈值 0.7）不过、该站不进 SVG；`test_providers.py:test_fixture_poi_search_filters_by_category_and_keyword` 明确断言"fixture 必须有坐标"。
2. **金额单位是分**：人均 128 元 → `12800`；门票 55 元 → `5500`；免费写 `0` 或省略（`price_cents` 取 `ticket_cents or avg_cost_cents or 0`）。
3. **跨零点营业**用 `open_from_minute > open_to_minute` 表示：20:00 开、次日 02:00 关 → `1200 / 120`。分钟数 = 时\*60+分，**次日时间照凌晨原值写，不要加 1440**。判定在 `open_covers()`（`hi<=lo` → `start>=lo or end<=hi`）与 `planner._fit_open_hours()`。
4. **`category` 用中文展示名**（`"酒吧酒馆"`，不是 `"BAR"`），也可写别名 `"清吧"`；合法值见 `Category` 的 17 个成员；`indoor` 漏填时由 `_derive_indoor` 按类目兜底。
5. `closed_weekdays` 目前**只被模型接收、引擎尚未消费**（周一闭馆靠 `guide.tips` 文字提醒）——要真生效就按 Recipe 2 加一条 `commonsense` 约束（`d.date.weekday()` 对比之，注意 `0=周一`）。

### 4.4 `guide` 与 `EvidenceRecord` 模板

`guide`（`FixtureGuide` 原样返回并补 `_source`/`city`，键名同 `evidence.py:_GUIDE_PROMPT`）：

```json
"guide": { "culture": "60-120字城市性格", "signature_foods": ["菜+推荐吃法"],
           "photo_spots": ["打卡点+最佳时段"], "tips": ["排队/支付/交通"],
           "safety": ["夜间/人群/财物"], "nightlife_areas": ["夜间街区"] }
```

`EvidenceRecord`（字段名对应 `models/evidence.py`）：

```json
{
  "id": "ev:cq-001", "city": "重庆", "poi_name": "洪崖洞民俗风貌区",
  "kind": "note", "title": "夜景别买票，免费开放但限流",
  "snippet": "工作日19点前人少，20点后限流；从1楼千厮门大桥下上电梯最快",
  "url": "", "author_type": "netizen", "published_at": "2026-09-18",
  "source": "fixture", "confidence": 0.7,
  "extracted": { "stay_minutes": 120, "need_reservation": false, "reservation_channel": "",
                 "reservation_lead_days": null, "reservation_cutoff": "", "best_time": "夜晚",
                 "pitfalls": ["20点后限流"], "photo_spots": ["千厮门大桥桥面"], "verdict": "值得" },
  "tags": ["夜景", "限流", "免费"]
}
```

`kind` ∈ `note|review|promo|official|model_summary|fixture`；`source` 是自由串（本地 `"fixture"`，LLM 联网 `"qwen_web_search"`）。回填规则在 `agents/enrich.py:_apply()`：`stay_minutes` 只接受 **15–600**；`need_reservation` 必须为 `true` 才生效；`verdict` 含"劝退"→`popularity-0.3`、含"值得"→`+0.15`；`pitfalls` 每条截 60 字、最多 6 条。

**`poi_name` 必须与 `poi.name` 严格一致**。`attach_evidence()` 先按 `poi_name` 建索引，`_match_hits()` 的判定其实是 `k == name or k in name or name in k`（完全相等**或**互为子串），所以子串也能挂上——但这正是风险：`"洪崖洞"` 会同时命中 `"洪崖洞民俗风貌区"` 和 `"洪崖洞码头"`，把 A 店的排队时长写进 B 店。命中后：`poi.evidence_ids` 追加证据 id（trace 打印"口碑提纯：N/M"）、`why` 仅在原文少于 20 字时被 `snippet` 覆盖、`tags` 合并去重上限 10。

**验证**

```bat
.venv\Scripts\python.exe -m tripagent.cli cities
.venv\Scripts\python.exe scripts\probe_apis.py 重庆
.venv\Scripts\python.exe -m tripagent.cli plan "重庆 2天 11月20日 网红打卡 爱吃火锅 预算800" --offline
.venv\Scripts\python.exe -m pytest -q
```

`--offline` 只保留 `LOCAL_PROVIDERS = {"fixture","estimated","offline"}`，能干净验证新城市数据本身。检查 `output\重庆_2天_*.md`：POI 应出现在"逐日安排"，"为什么是它"来自 `why`，"依据"来自 `scoring` 的 `reasons`。

## Recipe 5 · 新增一个导出格式

`src/tripagent/export/__init__.py` 有**两处登记**，加格式时都要改（漏一处就表现为"命令不报错但文件没生成"，因为不认识的 key 被 `continue` 静默跳过）：

```python
_EXT = {"md": ".md", "html": ".html", "ics": ".ics", "json": ".json"}
text = {"md": to_markdown, "html": to_html, "ics": to_ics, "json": to_json}[fmt](plan)
```

### 5.1 示例：JSON 产物

新建 `src/tripagent/export/json_export.py`（**别命名成 `json.py`**，会和标准库相对导入打架）：

```python
"""机器可读产物：Qoder Sites 前端与回归基线都读它。"""
from __future__ import annotations
from ..models.plan import TripPlan

def to_json(plan: TripPlan) -> str:      # pydantic v2：与 TripStore 落库用的是同一份序列化
    return plan.model_dump_json(indent=2)
```

再补 `export/__init__.py` 的 import、上面两处登记、以及 `__all__`。验证：

```bat
.venv\Scripts\python.exe -m tripagent.cli plan "西安 2天 历史人文 预算600" --out md,json
```

CLI 逐行打印 `json → D:\...\output\西安_2天_1105_2130.json`；顺手把 `cli.py build_parser()` 里 `--out` 的 help 改成 `（md/html/ics/json，none 关闭）`。

**别忘了 `app.py`**：下载按钮是写死的三元组 `for col, fmt in zip((c1, c2, c3), ("md","html","ics"))`，`mimes`/`labels` 也只按这三个 key 建。加新格式要同步开第四列、补两个字典项，否则网页上静默没有下载入口。

### 5.2 PDF 与 HTML 版块

**PDF 走浏览器打印，不加依赖**：`templates/report.html.j2` 已内置 `@media print { … .day { break-inside:avoid; } }`，所以流程是 `--out html` → 浏览器 → `Ctrl+P` → 另存为 PDF（`.day` 保证一天不被分页切断）。**不要引入 `weasyprint`/`reportlab`**（GTK/中文字体依赖会破坏"零 key、断网可演示"）；真要无人值守导出，排到 [ROADMAP.md](ROADMAP.md) P2-5。

**给 HTML 加版块**：改模板不够，要在 `export/html.py:build_context()` 里喂数据，例如每天加"风险因素"：`"risk_factors": "、".join(x.name for x in d.risk.factors) if d.risk else ""`。模板里 `{{ day.risk_factors }}` 自动转义（`select_autoescape`）；只有自家按坐标生成的 SVG 走 `| safe`（`to_html()` 把 `svg` 改名 `svg_raw`）。**任何含用户/模型文本的新字段都不要加 `| safe`。**

## Recipe 6 · 新增一个 Streamlit 页签

页签在 `app.py` 的 `main()` 创建：把 `t1..t5` 扩成 6 个并加 `with t6: tab_map(svc)`。要点：**每个页签每次交互都会被无条件执行**（Streamlit 整脚本重跑），所以新页签第一件事是"没结果就早退"，且不要在里面打网络。当前结果在 `st.session_state["outcome"]`（`PlanOutcome`，`.plan` 可能为 `None`）。

### 6.1 示例：路线图页与预算页（复用导出层，别在 UI 里重算归一化）

```python
def tab_map(svc: TripService) -> None:
    from tripagent.export.html import build_context
    st.markdown("### 每日路线示意")
    outcome = st.session_state.get("outcome")
    if not outcome or not outcome.plan:
        st.info("先在「行程规划」页生成一份行程。")
        return
    ctx = build_context(outcome.plan)
    labels = [d["date"] or f"Day{d['index']}" for d in ctx["days"]]
    day = ctx["days"][labels.index(st.selectbox("选择日期", labels))]
    sc = outcome.plan.scorecard
    chk = next((c for c in (sc.checks if sc else []) if c.id == "geo_coverage"), None)
    if chk and not chk.passed:
        st.warning(f"坐标覆盖不足：{chk.detail}（{chk.fix_hint}）")
    st.markdown(day["svg"] or "这天少于 2 个有坐标的地点，画不出路线。", unsafe_allow_html=True)
    st.caption("示意图按经纬度归一化，只表达先后与折返，不是真实道路地图。")


def tab_budget(svc: TripService) -> None:
    outcome = st.session_state.get("outcome")
    if not outcome or not outcome.plan:
        st.info("先在「行程规划」页生成一份行程。")
        return
    plan = outcome.plan
    total = max(1, plan.total_cost_cents)
    st.dataframe(pd.DataFrame([{"分项": k, "金额": f"¥{v/100:.0f}", "占比": f"{v/total:.0%}"}
                               for k, v in plan.budget_breakdown.items()]),
                 hide_index=True, use_container_width=True)
    used = plan.total_cost_cents / plan.request.budget_cents if plan.request.budget_cents else 0
    st.progress(min(1.0, used), text=f"已用 ¥{plan.total_cost_cents/100:.0f} / ¥{plan.request.budget_yuan:.0f}")
```

> 金额展示统一走 `models.common.fmt_cny(cents)`（`tripagent.models` 已导出），别自己 `/100` 拼字符串，否则和报告口径不一致。

**验证**

```bat
.venv\Scripts\python.exe -m streamlit run app.py --server.headless true
```

手工回归四条：① 不生成行程直接切新页签 → 只有提示、无异常；② 生成成都行程 → 新页签有内容；③ 侧栏切「离线演示模式」→ 新页签仍可用；④ 「历史」页"重新载入这份行程"→ 新页签读到 `session_state`。

## Recipe 7 · 接入 Qoder Sites 部署

### 7.1 两个决定方案的平台事实

| 事实 | 后果 |
|---|---|
| 站点产物是**静态文件**，`webDirectory` 里必须有 `index.html` | Streamlit / uvicorn / 任何 Python 进程**都不能**直接部署 |
| 服务端能力只有**一个** `app` Edge Function：入口 `functionDirectory/index.ts`、运行时 Deno（`Deno.serve(handler)`）、浏览器同源路径 `/functions/v1/app`；打包器不装依赖、不解析 TS 路径别名、不转换 Node 专用模块 | 没有 Python 运行时；引擎要么预生成，要么移植成 TS |

| 方案 | 做法 | 取舍 | 档 |
|---|---|---|---|
| **A 预生成（推荐）** | Python 本地/CI 跑演示诉求 → `output/` 的 HTML + Recipe 5 的 JSON 拷进 `dist/`；Function 只做免 key 的天气只读端点 | 引擎零移植、零漂移，发布即可演示；代价是行程不能在线现算 | P0 |
| **B 引擎移植 TS** | `core/` 的 5 个模块用 TS 重写进 `functions/`，Python 退为数据治理与 golden JSON 回归 | 在线真规划；两份实现会漂移，必须双端同测 | P1 |
| **C 外部 Python 服务 + 跨域调用** | 引擎放别的宿主 | **不满足"部署到 Qoder Sites"的硬性要求**，且引入 CORS/可用性风险 → 不做 | — |

### 7.2 方案 A 的目录与代码

```
travel-agent\site\               ← Sites 原始工程（新增，引擎仍在 src/ 与 output/）
  dist\                          ← webDirectory：index.html · trips\*.html · data\*.json · share\*.html
  functions\index.ts             ← 只做 Deno.serve(handler)
  functions\handler.ts            ← 业务逻辑，相对导入写显式扩展名
  dev\function-local.ts           ← 本地起服务用，不进发布包
```

`functions/handler.ts`（只读转发 Open-Meteo，免 key）：

```ts
const OM = "https://api.open-meteo.com/v1/forecast";
const CITY: Record<string, { lat: number; lng: number; name: string }> = {   // 白名单，不透传任意上游
  chengdu: { lat: 30.5728, lng: 104.0668, name: "成都" }, harbin: { lat: 45.8038, lng: 126.535, name: "哈尔滨" },
};

export async function handler(req: Request): Promise<Response> {
  const url = new URL(req.url);
  // 网关会把逻辑名 app 换成上游名并保留后缀 —— 不要假设 pathname 里还含 "app"
  if (!url.pathname.endsWith("/weather")) return Response.json({ error: "not_found" }, { status: 404 });
  if (req.method !== "GET") return Response.json({ error: "method_not_allowed" }, { status: 405 });
  const city = CITY[url.searchParams.get("city") ?? ""];
  if (!city) return Response.json({ error: "unknown_city" }, { status: 400 });
  const ctrl = new AbortController(); const timer = setTimeout(() => ctrl.abort(), 6000);  // 超时必须带
  try {
    const q = new URLSearchParams({ latitude: city.lat.toFixed(4), longitude: city.lng.toFixed(4),
      daily: "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
      timezone: "Asia/Shanghai", forecast_days: "16" });
    const up = await fetch(`${OM}?${q}`, { signal: ctrl.signal });
    if (!up.ok) return Response.json({ error: "upstream_error", status: up.status }, { status: 502 });
    const d = await up.json();
    return Response.json({ ok: true, city: city.name, days: d?.daily?.time ?? [], wmo: d?.daily?.weather_code ?? [] });
  } catch {
    return Response.json({ error: "service_not_configured" }, { status: 503 });
  } finally { clearTimeout(timer); }
}
```

`functions/index.ts` 只两行：`import { handler } from "./handler.ts"; Deno.serve(handler);`。前端调 `fetch('/functions/v1/app/weather?city=chengdu', { credentials: 'same-origin' })` 并校验 `json.ok === true`。WMO 码→中文请把 `knowledge/wmo.py:_TABLE` 导出成 `dist/data/wmo.json` 复用，**别在 TS 里手抄一遍**（两处会漂）。

### 7.3 Secrets（要 key 时才用）

`AMAP_KEY` / `DASHSCOPE_API_KEY` 是**应用 Secret**，只在函数里 `Deno.env.get("AMAP_KEY")` 读，缺失就回 `service_not_configured`。四条规矩：① 在 `prepare_site.secretNames` 声明代码真正依赖的名字（≤100 个；`[A-Z_][A-Z0-9_]{0,127}`；`QODER_`/`SUPABASE_`/`POSTGRES_` 前缀与 `DATABASE_URL` 是平台保留，不能用）；② **值由作者在 Portal 的 Sites Settings 里填**，不进源码/`.env`/命令行/前端产物，函数绝不打印环境变量或回传上游原始异常；③ Functions 后端能力需用户授权、只在准备 function 时申请，准备完要等它**就绪**并确认 Secret **同步成功**再发布依赖它的版本（静态站不会自动分配后端）；④ 被在用版本引用的 Secret 名可能删不掉——先发一个不再引用它的新版本。

### 7.4 本地验证 → 发布 → 公开访问

```bat
.venv\Scripts\python.exe -m tripagent.cli plan "成都 3天 11月20日 爱吃火锅 想体验夜生活 预算1500" --out md,html,ics,json
.venv\Scripts\python.exe -m http.server 4173 --bind 127.0.0.1 --directory site\dist
deno check site\functions\index.ts
deno run --allow-net=127.0.0.1:8000 site\dev\function-local.ts
curl --fail "http://127.0.0.1:8000/functions/v1/app/weather?city=chengdu"
```

`4173` 通了只代表"能取到文件"，视觉与交互仍要人眼过一遍。**没有 `deno` 就记录"Deno 校验待补"——前端 TS 编译和 Node 跑通都不能证明 Edge Function 可用。**

发布走 `prepare_site → get_publish_status → publish_site`，必须记住的约束：**`webDirectory` 不能是项目根**（用 `site/dist`；`.env*`/`.git`/`node_modules`/`_qoder` 会被打包器排除，也别手工建 `functions/_qoder`）；**体积**单文件 ≤50 MiB、Function 原始内容 ≤50 MiB、Worker 侧产物 ≤50 MiB，别把整个 `output/` 历史塞进去；**目录索引被禁用**，链接要写真实文件路径 `/trips/成都_3天.html`；**SPA** 需要 `prepare_site.spa: true` 否则深链刷新 404，本项目用多 `.html` 更省事；**命名空间**避开 `/api`（它**不是**本平台默认函数入口）、`/functions`、`/storage`、`/rest/v1`、`/__qoder_auth`、`/__qoder_internal`、`/internal`、`/supabase`；**新站点默认私有**，大赛要求公开访问，发布后要用 `update_access_policy`（带 `confirmPublic: true`，先 `get_site` 读 `access_mode`/`lifecycle`/`hasPublishedContent`）改公开，且 **Desktop 发布预览只渲染冻结静态文件、不执行 Functions**，后端必须发布后用真实 URL 复验；**401** 才引导 `/__qoder_auth/start?return_path=...`，403 是无权限，503 是依赖临时故障。

发布后自测：站点根 200；`/functions/v1/app/weather?city=chengdu` 有 `days`；非白名单城市 400；页面与 JS 里搜不到 `AMAP_KEY`/`DASHSCOPE_API_KEY` 字面值；DevTools Network 无 key 泄露。

## 改造时的红线清单

**稳定契约（改 = 破坏兼容，需同步全部调用方与测试）**：

- `src/tripagent/models/` 全部 —— `TripPlan` 是 UI/导出/存储的唯一对外契约；字段改名会让 `data/tripagent.db` 里的历史 `plan_json` 读不回来（`TripStore.load` 静默返回 `None`，你只会看到"找不到该行程"）。
- `ValidationReport` 的 18 个 `cid` —— 测试按 id 断言、文档按 id 解释；`name` 可改文案，`id` 不改。
- `Chain.run(..., allow_empty=...)` 语义 —— 空预警是有效答案；改掉默认会让 fixture 把"无预警"覆盖成"随机造一条预警"。
- `select_alarms()` 的 adcode 过滤 —— 去掉就回到"海南的海上大风当成成都的预警"；`tests/test_providers.py` 两条用例钉着它。
- `TripPlan.total_cost_cents` 口径 —— `budget_breakdown` 优先 + `intercity`；改成逐日累加会漏后几晚房费，预算约束虚假通过。
- `POIRecord._derive_indoor` 与 `_INDOOR` —— `indoor` 漏填时唯一兜底；删了天气避让就误判。
- 金额一律「分」 —— 混进 `float` 元会在 `budget_within`(3.0) 上出现 0.01 级抖动。
- `Category` 的中文 `value` —— fixtures、`.cache`、JSON 产物存的都是中文值，改 value = 改数据格式。

**可以随便加（低风险）**：`providers/**` 新 `@provider` 类与新模块文件（记得在 `providers/__init__.py` 加 import）· `data/fixtures/*.json` 新城市与新条目 · `data/fixtures/cities_extra.json`（改后重启进程）· `core/validator.py` 新 `_add(...)` 约束 · `scripts/` 探活与批量脚本 · `app.py` 新 `tab_*` · `export/` 新 `to_*` · `tests/` 新用例。

**谨慎改（有跨文件耦合）**：`AMAP_CATEGORY` 条目顺序（影响 4 位前缀匹配结果）· `core/scoring.py` 的加减分值（与 `planner._pick()` 的 `value <= -100` 阈值、`_POOL_BIAS` 互相校准）· `config.py` 链顺序（首位即主源，额度小的源放前面会先吃光配额）· `export/html.py:build_context()`（与 `report.html.j2` 字段名一一对应，改一边就空白）。

**测试怎么补**：① 新 provider/解析器 → `tests/test_providers.py`，喂真实抓回的样本 dict（照文件里的 `CMA_ROWS` 常量），不打网络；② 新约束 → `tests/test_engine.py`，用 `conftest.poi_pool` 造一份"故意违规"的 plan，断言 `.passed is False` 且 `fix_hint` 非空；③ 新意图/类目 → `tests/test_intent.py`，一行一句 `assert intent.parse(...)`，别名用 `Category("清吧") is Category.BAR`；④ 新 fixture 城市 → 一条 `chain.run("poi","search", city, POIQuery(...), model=POIRecord)` 断言"每条都有 `lat/lng` 且 `category` 精确匹配"；⑤ 收尾跑**回归三连**，基线 **31 passed**，数字变了要说明原因。
