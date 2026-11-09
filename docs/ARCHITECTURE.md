# TripAgent 架构说明

> 面向二次开发与答辩追问。运行方式请先看 [../README.md](../README.md)（本文不重复"三分钟跑起来"）。
> 改代码前必读的姊妹篇：[SECOND_DEVELOPMENT.md](SECOND_DEVELOPMENT.md)（7 个 Recipe）、[DATA_SOURCES.md](DATA_SOURCES.md)（数据源与合规）。

代码里所有类名、字段名、配置项名都以下列文件的实际定义为准：

| 层 | 目录/文件 | 主要符号 |
|---|---|---|
| 入口 | `app.py`、`src/tripagent/cli.py` | `main()` / `build_parser()` / `tab_plan()` |
| 编排 | `src/tripagent/agents/` | `TripService` `PlanOutcome` `EntertainmentAdvisor` `enrich` |
| 规则引擎 | `src/tripagent/core/` | `intent` `weather_risk` `scoring` `planner.DayAssembler` `validator` |
| 数据适配 | `src/tripagent/providers/` | `base.Chain` `provider` `Cache` + 各数据源类 |
| 数据契约 | `src/tripagent/models/` | `TripRequest` `CityWeather` `POIRecord` `TripPlan` `ValidationReport` |
| 静态知识 | `src/tripagent/knowledge/` | `cities.CityInfo` `wmo.describe` `poi_codes.AMAP_CATEGORY` |
| 持久化 | `src/tripagent/memory/store.py` | `TripStore`（表 `trips` / `plan_versions`） |
| 产物 | `src/tripagent/export/` | `export_plan` `to_markdown` `to_html` `to_ics` |
| 配置 | `src/tripagent/config.py` | `Settings` `get_settings()` `reload_settings()` |

---

## 1. 分层图

```
                    ┌─────────────────────────────────────────────┐
   用户一句话 ─────► │  入口层   app.py (Streamlit)  │  cli.py     │  只读 TripPlan，不含业务判断
                    └───────────────┬─────────────────────────────┘
                                    │ TripService.plan_trip(text) / .ask(text)
                    ┌───────────────▼─────────────────────────────┐
                    │  编排层  agents/service.py                  │  流水线 + trace 留痕
                    │   understand→weather→assess_risk→gather     │  llm 只在 3 处出现，失败不致命
                    │   →refine→assemble→validate→narrate→export  │
                    └───┬───────────────────────────────┬─────────┘
                        │                               │
        ┌───────────────▼──────────────┐   ┌────────────▼────────────────┐
        │  规则层 core/                │   │  数据层 providers/          │
        │  intent.py     正则解析      │   │  base.py: @provider 注册表  │
        │  weather_risk.py 阈值规则    │   │  Chain.run(cap, method,     │
        │  scoring.py    打分+reasons  │   │    allow_empty, model)      │
        │  planner.py    DayAssembler  │   │  能力: forecast/alarm/poi/  │
        │  validator.py  18~19 条约束  │   │   transit/evidence/guide/llm│
        │  （纯函数，不 import httpx） │   │  Cache + Provider.cached_call│
        └───────────┬──────────────────┘   └────────────┬────────────────┘
                    │                                   │ HTTP
        ┌───────────▼──────────────────┐   ┌────────────▼────────────────┐
        │  契约层 models/ (pydantic)   │◄──│  知识层 knowledge/          │
        │  TripRequest / CityWeather / │   │  cities.py 71 城坐标+adcode │
        │  POIRecord / TripPlan /      │   │  wmo.py  WMO 码→中文+灾害   │
        │  ValidationReport            │   │  poi_codes.py 高德分类编码  │
        └───────────┬──────────────────┘   └─────────────────────────────┘
                    │
     ┌──────────────▼───────────────┐
     │  memory/store.py  SQLite     │   export/  md · html(SVG) · ics
     │  trips + plan_versions(不可变)│
     └──────────────────────────────┘
```

**为什么这样切**：三条边界各自解决一个具体故障模式。

| 边界 | 切开的理由 | 不切会怎样 |
|---|---|---|
| `core/` 不 import `httpx` | 规则必须能被离线确定性单测覆盖（`tests/` 31 例全部不联网，0.11s 跑完） | 想验证"暴雨日该转室内"就得打网络，测试不可重复 |
| `providers/` 只认 `POIQuery`/返回模型 | 换一个数据源不动引擎；`config.py` 的 chain 顺序即优先级 | 换源要改 planner，改一处崩三处 |
| `models/` 是唯一对外契约 | UI/导出/存储都只读 `TripPlan`，新增展示项不用碰引擎 | 导出层去反推 provider 原始 JSON，字段漂移 |

编排层的存在意义是**留痕**：`PlanOutcome.trace` 记录每一步命中了哪个 provider、拿了几条数据，`Chain.log` 记录谁失败了、为什么。`tripagent.cli plan` 与网页页脚"数据来源与降级轨迹"直接把它打印出来。

---

## 2. 主链路：一次 `plan_trip` 的 9 步

`src/tripagent/agents/service.py:TripService.plan_trip()` 的实际顺序：

| # | 步骤 | 调用 | 产出 |
|---|---|---|---|
| 1 | 解析 | `core/intent.py:parse(text)` | `TripRequest` |
| 2 | 城市定位 | `knowledge/cities.py:get_city / resolve_city` | `city_key` `adcode` `lat/lng`（认不出直接返回 `errors`） |
| 3 | 天气 | `fetch_weather()` → `Chain.run("forecast","fetch_forecast",city,16)` + `Chain.run("alarm","fetch_alarms",city, allow_empty=True)` | `CityWeather` |
| 4 | 选出发日 | 用户没给日期时先 `weather_risk.assess(weather, 全窗口)` 取 `best_dates[0]` | `req.start_date` |
| 5 | 风险 | `weather_risk.assess(weather, window, party)` 写回 `weather.risks/overall`，返回 `blocking_advice`/`comfort`/`best_dates` | `outcome.risk` |
| 6 | 采集 | `collect_pois()`（按 `categories_for(interests)` 逐类目，最多 9 类）、`collect_evidence()`、`pick_lodging()` | `pool: dict[Category, list[POIRecord]]` |
| 7 | 提纯 | `agents/enrich.py:attach_evidence(flat, evidence)` 回填 `stay_minutes`/预约/避坑/`best_time` | 被增强的 POI 数 |
| 8 | 装配+校验 | `core/planner.py:ItineraryPlanner.plan(...)` → `core/validator.py:validate(...)` | `TripPlan` + `scorecard` |
| 9 | 文案 | 有 key：`enrich.narrate()`；无 key：`service._template_narrative()` | `plan.narrative` |

`ask()` 走另一条短链：`intent.detect_query()` → `poi.search` → `core/scoring.score()` → `evidence.collect` → `advisor._summarize()`（失败退 `_template_summary()`）。同一套打分函数，避免两套逻辑长期分叉。

---

## 3. 核心数据契约（字段级）

### 3.1 `TripRequest` — `models/request.py`

| 字段 | 类型 | 谁写入 | 谁消费 |
|---|---|---|---|
| `raw_text` | `str` | `intent.parse` | 城市兜底重认、LLM 补漏入参 |
| `destination` `city_key` `adcode` | `str` | `get_city()` | 预报、预警过滤、高德 `region` |
| `origin_city` | `str` | `parse_origin()` | `ItineraryPlanner._intercity()`；首日 `cursor` 推到 13:00 |
| `days` | `int`（默认 3） | `parse_days()` | `_window()` 长度 |
| `start_date` | `date \| None` | `parse_date()` 或第 4 步自动选 | 预报窗口、`reservation_feasible` |
| `budget_cents` | `int`（**分**） | `parse_budget()` + `set_budget_yuan()` | 预算裁剪、`budget_within`（weight 3.0） |
| `party: Party` | `adults/kids/elders/pets/accessible` | `parse_party()` | 打分、`head_count` 乘价、`kid_safe_venues` 等硬约束 |
| `interests: list[Interest]` | 15 个枚举 | `parse_interests()` | `categories_for()` 决定检索哪些类目 |
| `avoid` / `must_visit` | `list[str]` | `parse_avoid/parse_must_visit` | `scoring.score()`：命中 avoid 直接 `-1000`，命中 must_visit `+60` |
| `pace` | `relaxed/normal/packed` | `parse_pace()` | planner 的 `_PACE_STAY/_PACE_LIMIT/_PACE_BUFFER` |
| `nightlife` | `bool` | `_wants_nightlife()`（含"不要夜生活"反向） | `_fill_night()` 是否执行 |
| `lodging_area` | `str` | `parse_lodging_area()` | 住宿检索关键词 |

`Party.head_count` 至少为 1；`Party.needs_care`（有娃/有长辈/无障碍）会让每日项目数 `-1`、缓冲 `+12` 分钟。

### 3.2 `CityWeather` — `models/weather.py`

```
CityWeather(city, city_key, lat, lng,
            forecast: list[DayForecast],      # 16 条（Open-Meteo 上限）
            alarms:   list[AlarmRecord],      # 已被 select_alarms 过滤到本市/本省
            risks:    dict[ISO日期, WeatherRisk],
            overall:  Severity,
            notes:    {"forecast": provider名, "alarm": provider名})
```

- `DayForecast`：`date wmo_code text text_en tmax_c tmin_c precip_mm precip_prob wind_max_kmh humidity_max sunrise sunset source`。派生属性 `is_wet`（`precip_mm>=10` 或 `precip_prob>=70`）、`is_rainy_light`、`is_snow`（WMO 71/73/75/77/85/86）、`is_hot`（`tmax_c>=35`）、`is_cold`（`tmin_c<=0`）。
- `AlarmRecord`：`title alarm_type level severity scope issuer issued_at area text url source`。`level` 是"蓝/黄/橙/红"，`LEVEL_TO_SEVERITY` 映射到 `Severity`；`scope` 取 `"city"`（地名命中）或 `"province"`（adcode 前 2 位命中）；`is_critical` = 橙/红或台风/暴雨/暴雪。
- `WeatherRisk`：`severity factors advice prefer_indoor travel_not_recommended`。`factors` 是 `RiskFactor(name, detail, severity, recommend_indoor)` 的 dataclass（用 dataclass 是为了能按位置构造，读起来像一句话）。`add()` 自动取最高等级并累积 `prefer_indoor`。
- 访问器：`on_day(d)`、`risk_on(d)`、`danger_dates`。

### 3.3 `POIRecord` — `models/poi.py`

一份模型同时装三种来源：高德真实返回（`source="amap"`）、本地 fixture（`source="fixture"`）、LLM 提纯回填。关键字段分组：

| 组 | 字段 | 备注 |
|---|---|---|
| 身份 | `id name category city district address` | `id` 高德侧是 `amap:<poi id>` |
| 坐标 | `lat lng` → `point` | 缺失时通勤退化为默认 15 分钟、SVG 路线图不画 |
| 价格 | `rating review_count avg_cost_cents ticket_cents` | **全部以「分」为整数**；`price_cents` 取 `ticket_cents or avg_cost_cents` |
| 时间 | `open_hours_raw open_from_minute open_to_minute closed_weekdays stay_minutes` | `open_to_minute < open_from_minute` 即跨零点营业 |
| 空间属性 | `indoor metro` | `indoor` 由 `_derive_indoor` 校验器按类目兜底 |
| 准入 | `need_reservation reservation_channel reservation_lead_days reservation_cutoff age_policy requires_id` | LifeHack 场景最能提升体验的一组 |
| 人群 | `kid_friendly pet_friendly accessible` | 三态：`None` = 未知，不惩罚 |
| 内容 | `tags why pitfalls photo_spots best_time popularity` | 直接进报告 |
| 溯源 | `source external_id confidence evidence_ids raw` | `raw` 只保留 `typecode/tel/photo/biz_ext/distance` |

`_derive_indoor`（`model_validator(mode="after")`）是天气避让正确性的隐性地基：`_INDOOR` 元组里的类目（博物馆/咖啡/酒吧/KTV/夜店/剧场/影院/密室/温泉/住宿）即使数据源漏填 `indoor` 也会被强制置真，否则雨天会把博物馆当户外。注意 `SHOPPING` 刻意不在表里——夜市是露天的，交给数据自身表达。

`open_covers(start, duration)` 返回 `True/False/None`，`None` 表示"营业时间未知 → 放行"，宁放不误杀。

### 3.4 `TripPlan(DayPlan → Slot)` — `models/plan.py`

```
TripPlan(trip_id, request, days[], intercity[], evidence[], sources_used[],
         warnings[], blocking_advice, narrative, packing[],
         budget_breakdown{name: 分}, scorecard, plan_version, created_at, extras)
  └ DayPlan(index, date, city, theme, weather, risk, lodging, slots[],
            total_cost_cents, total_transit_minutes, notes[])
     └ Slot(seq, type: SlotType, title, start_minute, end_minute, poi,
            cost_cents, transit_before: RouteLeg, indoor, notes[],
            reservation_hint, evidence_ids[], flexible)
```

- `Slot.notes` 就是 `scoring.Scored.reasons` 的落地，"为什么是它"由此可答。
- `SlotType` 8 个值：`交通移动 / 游览 / 用餐 / 观看演出 / 夜间娱乐 / 休息缓冲 / 入住退房 / 自由活动`。
- `flexible=True` 标记可被天气规则整体替换的段（`SIGHT` 与 `NIGHTLIFE`）。
- `TripPlan.total_cost_cents`（property）：`budget_breakdown` 非空时以它为准再加 `intercity`，否则退回逐日累加。
- `DayPlan.sight_count` 只统计 `SIGHT/SHOW/NIGHTLIFE`；`weather_brief` 拼"天气 + 温差 + 降水"。

---

## 4. 关键设计决策与理由

### 4.1 规则引擎先行，LLM 只做补漏 / 归纳 / 文案

`core/intent.py` 全程用正则与别名表解析（`parse_days` `parse_budget` `parse_date` `parse_party` `parse_interests` `parse_pace` `parse_must_visit` `parse_avoid` `parse_lodging_area`），`tests/test_intent.py` 里 10 条断言钉死行为。LLM 只在三处出场：

| 出场点 | 位置 | 失败后果 |
|---|---|---|
| 口碑归纳 | `providers/evidence.py:QwenSearchEvidence.collect` → `Chain.run("llm","chat", ..., web_search=True)` → `parse_ugc_lines()` | 退到 `FixtureEvidence` 本地口碑库 |

> **为什么口碑用「竖线分隔的文本行」而不是 JSON**：实测 qwen 开启 `enable_search` 后，会在中文摘要里写入未转义的英文引号，把 JSON 结构打断，且**失败点每次都不一样**（同一段提示词，一次能解析、一次不能）。改成扁平的 `字段|字段|…` 按行格式后，同一条链路从"83 秒后无结果"变成"15 秒返回 6 条"。`extract_json()` 仍保留（供 `guide`/`narrate` 等非联网调用使用），并额外容忍围栏、尾逗号、包一层对象、被截断四种缺陷。
| 行程文案 | `agents/enrich.py:narrate()` | 退到 `service._template_narrative()`（纯事实汇总，不编故事） |
| 问答总结 | `agents/advisor.py:EntertainmentAdvisor._summarize()` | 退到 `_template_summary()` |

理由不是"LLM 不好"，而是四个工程约束：**可预测**（同一句话同一日期结果稳定，`FixtureWeather._stable_rng` 甚至用 sha256 做确定性伪随机）；**可单测**（31 例 0.11 秒，无网络无 key）；**零 key 可用**（现场演示的保险丝）；**成本可控**（每次规划最多 3 个 topic + 1 次 narrate）。

补充：`enrich.llm_fill_request()`（把 LLM 抽取的字段补进 `TripRequest`）已在 `plan_trip` 里接线，触发条件是「有 `DASHSCOPE_API_KEY` 且规则解析留下了缺口（城市/兴趣/日期/预算任一为空）」；补出来的字段名写进 `PlanOutcome.filled_by_llm`，CLI 与 UI 都能看到"哪几项是模型补的"。没有 key 时这一步整体跳过，规则解析独立成立。

### 4.2 金额一律用「分」的整数

`models/common.py:to_cents/fmt_cny/cents_to_yuan`，字段名统一带 `_cents` 后缀（`budget_cents` `avg_cost_cents` `ticket_cents` `cost_cents` `total_cost_cents` `transit_before.cost_cents`）。理由：`budget_within` 是 `weight=3.0` 的硬约束，若用 `float` 元累加几十项，会出现 `2999.9999999 > 3000` 这类假阴性；整数累加恒等可证。展示层用 `fmt_cny()`（整元不带小数，`¥128`；非整元 `¥128.50`）。

### 4.3 降级链 + `allow_empty`

`Chain.run(capability, method, *args, model=..., allow_empty=False)` 默认把"空结果"当失败继续降级。但**"某城市当前无预警"本身是有效答案**：若不特判，`cma_alarm` 返回空列表后会继续走到 `FixtureWeather.fetch_alarms()`，它会按月份池 `_alarm_pool_by_month()` 以 12% 概率**随机造一条预警**——于是成都明明天气很好，报告却说"海上大风预警，取消滨海行程"。因此 `service.fetch_weather()` 里 alarm 这一路显式写了 `allow_empty=True`，第一个不抛异常的 provider 直接胜出。

同理，`provider.available()`（缺 key / 离线）让链跳过整源，`Chain.providers_for()` 对未注册名记 `"未注册的 provider 名"`，构造异常记 `"初始化失败 ..."`——三种失败在 `doctor` 里长得一样但原因不同，这正是可观测性的目的。

### 4.4 预警必须按 adcode 过滤到本省

`providers/weather.py:select_alarms(rows, city, source)`：`CMAAlarm` 拉的是**全国**实时预警列表（`{"msg":"success","data":[{id, headline, title, description, effective, type}]}`，`id` 前 6 位是县级 adcode），`NMCAlarm` 同理（`{"data":{"page":{"count":...,"list":[{alertid,title,issuetime,url,pic}]}}}`）。过滤逻辑两档：

- `in_city`：`city.name` 出现在标题+正文里 → `scope="city"`；
- `in_province`：`rid[:2] == city.adcode[:2]`（省级前缀）→ `scope="province"`。

排序 `(a.scope != "city", -a.severity.rank)`，截断前 10 条。不用文本匹配而用 adcode，是因为地名有歧义而 adcode 没有；只取前 2 位而非 4 位，是因为省级预警（如全省寒潮） legitimately 影响该省任一城市的行程。**不做这件事的代价**：把海南的"海上雷雨大风"当成成都的预警写进结论，天气模块的可信度当场归零。`tests/test_providers.py:test_alarm_filter_keeps_only_target_city_scope` 与 `test_select_alarms_returns_empty_for_unrelated_city` 就是在钉这个行为。

另一处保守设计：`weather_risk.assess()` 对**提取不到生效日期**的预警按"窗口期整体生效"处理（宁可保守也不要漏掉台风），能提取到日期的才精确落日到 `alarms_by_day`。

### 4.5 住宿费用进 `budget_breakdown`，不进逐日 `Slot`

`DayAssembler._append_checkin()` 造的入住 `Slot` 显式 `cost_cents=0`，注释写明原因；真正的房费在 `planner._breakdown()` 里按整程晚数一次性算：

```python
groups[f"住宿（{nights} 晚）"] = lodging.price_cents * head_count * nights   # nights = max(1, len(days)-1)
```

如果把房费摊进每天的 slot，`DayPlan.recompute_totals()` 的逐日累加只会覆盖"有住宿记录的那几天"，跨日边界漏算，而 `TripPlan.total_cost_cents` 又优先看 `budget_breakdown` —— 两套口径会给出不同总额，预算校验（`weight=3.0`）可能虚假通过。现在的口径是**唯一真源在 `budget_breakdown`**，逐日 `total_cost_cents` 只表示"这天玩+吃+通勤的现金流"，两者语义不同但总额自洽。

### 4.6 恶劣天气日"最多保留 1 项户外"，而不是全禁

`DayAssembler` 实例字段 `outdoor_used` + `_allow_outdoor()`：

```python
return not (self.risk and self.risk.prefer_indoor and self.outdoor_used >= 1)
```

`validator._check_hard()` 的 `weather_adapted`（`weight=2.5`）也按同一口径判定：风险日户外项 `len(outdoor) >= 2` 才算未通过。

为什么不归零：① 一整天空洞比"少一个户外"更伤体验，尤其冬季冰雪类目的地本身就是户外产品；② 现实中"小到中雨"是可用天气，把 `WATCH` 级也禁光会误伤；③ 规则要能被解释——"保留风险最低的 1 项户外"比"完全禁止"更贴近本地领队的实际决策。`_advice_for()` 会同时输出"室内优先：把博物馆/展馆/商场/剧场排进下午，户外只留降水概率低的上午"，把取舍讲清楚。`tests/test_engine.py:test_rainy_day_gets_indoor_swaps` 断言"户外 ≤1 且至少 1 个室内且 `weather_adapted` 通过"。

### 4.7 风险日优先分配室内候选

`ItineraryPlanner.plan()` 不按日期顺序装配，而是先排优先级：

```python
priority = sorted(range(len(window)), key=lambda i: -self._risk_rank(window[i]))
```

即风险等级高的那一天先跑 `DayAssembler`。原因：室内候选（博物馆/剧场/商场）通常**数量少于**户外候选；若按 Day1→Day3 顺序，好天气日会先把室内名额耗尽，轮到雨天时池子里只剩户外，`prefer_indoor` 就无从执行。所有 assembler 共享同一个 `used: set[str]` 与递减的 `budget_left`，所以先到先得不会重复占用。

配套的 `_pick(ranked, allow_outdoor)` 在 `prefer_indoor` 时做**两遍扫描**（先只看在室内的，再看全集），并过滤 `value <= -100` 与超价候选。

### 4.8 导出用 SVG 示意图，而不是高德 JS / Leaflet 瓦片

`export/html.py:_day_svg()` 把当天有坐标的 `Slot.poi.point` 按 `min/max` 经纬度归一化到 340×150 视口，画 `polyline` + 编号 `circle`（夜场橙 `#e8590c`、室内蓝 `#1971c2`、户外绿 `#2f9e44`）。选它的原因：

| 方案 | 问题 |
|---|---|
| 高德 JS API | 需要另一个 **Web 端 JS Key**（与本项目已申请的 Web 服务 Key 不是同一平台），且要浏览器联网加载 SDK |
| Leaflet + OSM 瓦片 | 境内网络常拉不到瓦片，演示现场直接灰屏 |
| 内嵌静态地图图片 | 要额外存储与签名 |
| **本方案（纯 SVG）** | 零依赖、零 key、离线可渲染，`build_context()` 产出即写死在单文件 HTML 里，还能 `@media print` 打印成 PDF |

代价也要讲明白：**它是"顺序/折返示意图"，不是地图**——不表达真实道路与比例尺。所以 `validator` 的 `geo_coverage` 约束（覆盖率 <0.7 判未通过）会把"坐标缺失导致图退化"这件事显式报出来。真地图属于 [ROADMAP.md](ROADMAP.md) 的 P1（高德 Key + JS API 到位后再做）。

`to_html()` 里 SVG 用 `| safe` 输出，因为它是我们自己按坐标生成的可信内容；其余字段走 Jinja2 `select_autoescape`。

### 4.9 `ValidationReport`：把"排得合理吗"变成可展示的数

`core/validator.py:validate()` 分三个私有函数产出 **18 条常规约束 + 1 条条件约束**（有夜间娱乐安排时才出现 `nightlife_compliance`），`ConstraintKind` 三分类沿用 TravelPlanner / ChinaTravel 的公开定义（`env` 环境事实 4 条、`commonsense` 常识 8 条、`hard` 用户硬约束 6 条 + 1 条件项）：

| 维度 | 条数 | 条目（`id`） |
|---|---|---|
| `env` | 4 | `city_resolved`(w2.0) `geo_coverage` `poi_source` `weather_coverage` |
| `commonsense` | 8 | `no_time_overlap`(w1.5) `within_day_window` `no_duplicate_poi`(w1.2) `route_efficiency`(w1.5) `open_hours_respected`(w1.5) `every_day_has_plan` `meal_time_reasonable` `walk_leg_within_limit` |
| `hard` | 6(+1) | `budget_within`(w3.0) `kid_safe_venues`(w2.0) `pet_allowed`(w1.5) `accessible_only`(w1.5) `weather_adapted`(w2.5) `reservation_feasible`(w1.5) ＋条件项 `nightlife_compliance` |

未写 weight 的默认 `1.0`。两种口径同时给出：`pass_rate()`（条数通过率，`verdict()` 用它分档：≥95% "方案可直接执行"、≥80% "基本合理"）与 `overall`（**按 weight 加权**的通过率），`hard_failed` 单独短路成"存在硬约束冲突，需要调整"。用学界口径而不是自造指标，是为了回答"凭什么说你排得合理"。

注意 `budget_within` 的分支：无预算时写入一条"（未设置，跳过）"的**通过**记录，而不是缺席——保证记分卡维度恒定，UI 不会因缺项塌陷。

---

## 5. 与开源同类项目的差异

下表是**定位说明**，不是跑分对比：本项目未在 TravelPlanner/ChinaTravel 数据集上做定量评测（见 [ROADMAP.md](ROADMAP.md) P1 的"评测集"项）。借鉴点在各文件 docstring 里都有署名。

| 项目 | 它的做法 | 本项目借鉴了什么 | 刻意没做什么 / 为什么 |
|---|---|---|---|
| **TripStar** | 多智能体流水线 + 小红书等内容抓取，UGC → 结构化 | `models/evidence.py` 的 `EvidenceRecord.extracted`（`stay_minutes / need_reservation / pitfalls / photo_spots / best_time / verdict`）就是它的"提纯"步骤，落在 `agents/enrich.py:attach_evidence()` | 不做多智能体协作、不爬小红书。多个 LLM agent 互相调用会把不可控性从数据层转移到推理层，而本项目的卖点是**可校验**；小红书开放平台对个人不开放内容接口（详见 [DATA_SOURCES.md](DATA_SOURCES.md) §4），逆向违反 ToS 且随时失效，比赛演示不能压在上面 |
| **TravelPlanner** | 把行程约束分 env/commonsense/hard 并算通过率 | 直接沿用三分法（`models/enums.py:ConstraintKind`），记分卡 `ValidationReport.scorecard()` 输出四个维度 | 不做它的可执行 SQL 数据库校验。它的基准是"虚构城市 + 固定航班/酒店表"，我们要对**真实中文城市**与实时天气，数据面完全不同 |
| **ChinaTravel** | 中文场景 + DSL 表达约束 | 借它的约束分类直觉来解释"通过率"口径 | 不引入 DSL。18 条约束用 Python `_add(report, cid, kind, name, passed, detail, hint, weight)` 直接写死，加一条约束 = 加一行调用（见 [SECOND_DEVELOPMENT.md](SECOND_DEVELOPMENT.md) Recipe 2）；为此写解释器不划算 |
| **TREK** | 分记账的预算处理 + MCP 工具门控 | `models/common.py` 明确署名借鉴——金额全链路整数「分」 | 不做 MCP 工具暴露。工具门控解决的是"agent 该调哪个 API"，而本项目的 provider 选择由 `config.chain()` 静态决定，可复现性更强；未来接 Qoder Sites 时才把能力包成 HTTP（Recipe 7） |
| **zhilv-yuntu** | RAG + ChromaDB 做攻略问答 | 认同"推荐要能溯源"这件事，落到 `evidence_ids` + `source` + `confidence` 三字段 | 不上向量库。单城市 POI 量级只有几十条，别名表 + 子串匹配（`poi.FixturePOI._match`、`enrich._match_hits`）已够，多一个 ChromaDB 就多一个部署依赖与失效点；真要扩到全国 UGC 再引入 |

一句话概括差异：**别人在做"更像人的攻略生成器"，本项目在做"能被逐条打分的排程器"**。天气是决策者而非装饰（`blocking_advice` 能否决出行）、每条推荐有依据（`Slot.notes` = `Scored.reasons`）、每条约束有结果（`ValidationReport`）、每次数据降级有痕迹（`Chain.log`）。

---

## 6. 配置与开关速查（`src/tripagent/config.py`）

优先级：**代码默认值 < `.env` < 真实环境变量**（`pydantic-settings`）。

| 配置项 | 别名 | 默认 | 作用 |
|---|---|---|---|
| `dashscope_api_key` | `DASHSCOPE_API_KEY` | 空 | 空则 llm/evidence/guide 的 `qwen_search` 全部 `available()=False` |
| `amap_key` / `amap_secret` | `AMAP_KEY` / `AMAP_SECRET` | 空 | 空则 poi/transit 退到 `fixture` / `estimated` |
| `qweather_key` + `qweather_host` | `QWEATHER_KEY`/`QWEATHER_HOST` | 空 | **两者都要**才可用（`describe()` 会分开提示缺哪个） |
| `llm_model` | `TRIPAGENT_LLM_MODEL` | `qwen-plus` | `DashScope.chat()` 的 model 参数 |
| `offline` | `TRIPAGENT_OFFLINE` | `False` | 真时 `chain()` 只保留 `LOCAL_PROVIDERS = {"fixture","estimated","offline"}` |
| `cache_ttl_hours` | `TRIPAGENT_CACHE_TTL_HOURS` | `6.0` | 设 `0` 即禁用读缓存（`Cache.get` 直接返回 None） |
| `http_timeout` | `TRIPAGENT_HTTP_TIMEOUT` | `12.0` | `Cache` 之外，LLM 用 `http_timeout*3` |
| `forecast_chain` | — | `qweather,open_meteo,fixture` | 7 条链都可以只用环境变量覆盖，**不必改代码** |
| `alarm_chain` | — | `qweather,cma_alarm,nmc_alarm,fixture` | |
| `poi_chain` / `transit_chain` | — | `amap,fixture` / `amap,estimated` | |
| `evidence_chain` / `guide_chain` | — | `fixture,qwen_search` | 本地库优先（毫秒级、可复现）；只有本地没这座城市时才联网归纳（约 15-90 秒）。想要"永远最新"就把顺序换成 `qwen_search,fixture` |
| `llm_chain` | — | `dashscope` | |
| `day_start_minute` / `day_end_minute` / `night_end_minute` | — | `510` / `1290` / `1440` | 08:30 / 21:30 / 24:00，`within_day_window` 用它判定 |
| `max_sights_per_day` / `max_meals_per_day` | — | `3` / `3` | `_day_limit()` 上限 = `max_sights_per_day + 1` |
| `max_walk_minutes_per_leg` | — | `35` | 被 `validator.walk_leg_within_limit` 消费：单段步行超过该分钟数判未通过（与"通勤占比>45%"的 `route_efficiency` 互补） |
| `data_dir`/`cache_dir`/`fixture_dir`/`output_dir` | — | `data` / `.cache` / `data/fixtures` / `output` | `PROJECT_ROOT` 由向上找 `pyproject.toml` 决定 |

`get_settings()` 带 `lru_cache(maxsize=1)`；改完 `.env` 要用 `reload_settings()`（`app.py` 的 `_service()` 每次调用它，所以网页里刷新即生效）。**注意** `knowledge/cities.py` 的 `_load_extra()` 在 import 时只跑一次，新增 `cities_extra.json` 后必须重启进程。

---

## 7. 测试地图

`pyproject.toml` 里 `testpaths=["tests"]`、`addopts="-q"`。跑法见 README。`tests/conftest.py` 在 import 前就设了 `TRIPAGENT_OFFLINE=1` 并清空三个 key，所以**不可能**打到网络。

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `tests/test_intent.py` | 10 | 中文数字天数、日期不当天数（"11月20日"≠20天）、相对日期与节假日顺延、人均预算×人数、人群识别、夜生活反向、must_visit/avoid、`detect_query` |
| `tests/test_providers.py` | 11 | 离线必落 fixture、`select_alarms` 只留本市/本省、`parse_alarm` 四例、`_amap_to_poi` 元→分与跨零点、无 key 不尝试高德、`cached_call` 模型回读往返、`estimated` 通勤、枚举别名反查、`is_critical` |
| `tests/test_engine.py` | 10 | 大雨日 `prefer_indoor`、红色预警出 `blocking_advice` + `best_dates`、舒适指数排序、打分室内胜出与 avoid 剔除、每天必有安排、预算上限、雨天户外≤1、记分卡四维度、带娃排酒吧必判失败 |

`conftest.py` 的 `poi_pool` fixture 是一条直线上的 10 个合成 POI（专门方便验证"不折返"），新增约束/打分规则时优先复用它而不是真 fixture，可保持测试确定性。
