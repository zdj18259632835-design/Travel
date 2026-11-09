# TripAgent 数据源清单、配额与合规

> 事实核对时间：**2026-11-05 实测**（本项目运行环境：中国大陆直连、个人学生账号）。
> 结论会随平台政策变化，改之前先跑 `scripts/probe_apis.py` 复现，不要只看本文。
> 相关代码：`src/tripagent/providers/`（适配层）、`src/tripagent/config.py`（降级链）、`src/tripagent/knowledge/`（自维护的三张表）。

---

## 1. 数据源总览

| 能力 | 数据源 | 需要 Key | 配额/额度 | 失效风险 | 在本项目里的角色 |
|---|---|---|---|---|---|
| 数值预报 | **Open-Meteo** `api.open-meteo.com/v1/forecast` | 否 | 免 key，非商业用法无硬门槛 | 低 | **主数值源**（16 天，`providers/weather.py:OpenMeteo`，`forecast_chain` 第二位） |
| 数值预报 | 和风天气 `/v7/weather/15d` | 是 | 后付费 0~5 万次/月 | 中（额度与 Host 变更） | 可选升级源（`QWeather`，`forecast_chain` 首位，填了 key 才生效） |
| 数值预报 | 心知天气 | 是 | 免费档**只给 3 天** | 高（额度太小） | **未接**：3 天覆盖不了"半个月"诉求，接也只能当备胎 |
| 数值预报 | 高德天气 `/v3/weather/weatherInfo` | 是 | 随 Web 服务 Key | 低 | **未接**：只有 3~4 天，不够 |
| 数值预报 | 中国天气网 `data/sk/`、`data/cityinfo/` | 否 | — | **已停更** | **不用** |
| 实时预警 | **中国气象局** `weather.cma.cn/api/alarm` | 否（**必须带 User-Agent**，否则 403） | 无 | 低 | **主预警源**（`CMAAlarm`，`alarm_chain` 第二位） |
| 实时预警 | **中央气象台** `nmc.cn/rest/findAlarm` | 否 | 无 | 低 | 预警补充源（`NMCAlarm`，第三位；标题自带类型+级别） |
| 实时预警 | 和风天气 `/v7/warning/now` | 是 | 同上 | 中 | 有 key 时的首位预警源 |
| 实时预警 | 台风 `typhoon.nmc.cn/weatherservice/typhoon/jsons/list_default` | 否（JSONP） | 无 | 低 | 暂未接入，P1 台风路径用 |
| 实时预警 | 12379.cn（国家预警信息发布中心） | — | — | **本环境不可达** | 不用 |
| POI | **高德开放平台** `/v5/place/text`、`/v5/place/around` | 是（Web 服务 Key） | 见 §2.2 | 低 | **唯一需要 Key 的升级项**：任意城市实时 POI（评分/人均/营业时间） |
| POI | 本地 `data/fixtures/*.json` | 否 | — | 无 | 离线兜底（`FixturePOI`），当前已含成都/西安/长沙/杭州 |
| 通勤/路径 | 高德 `/v5/direction/{walking,driving,bicycling,transit/integrated}` | 是 | 认证个人逆地理编码 30 万次/月、QPS 200 | 低 | `AMapTransit`，`transit_chain` 首位 |
| 通勤/路径 | Haversine 直线估算 | 否 | — | 无 | `EstimatedTransit`（步行 4.5km/h、公交含换乘 18km/h） |
| 口碑 UGC | **通义千问 `enable_search`** | 是（DashScope） | 新人每模型约 100 万 token/90 天 | 中（模型政策） | 合法替代爬取的**联网检索+归纳**（`QwenSearchEvidence`，输出标 `source=qwen_web_search`） |
| 口碑 UGC | 小红书 / 抖音 / B站 / 微博 开放平台 | 是 | 需企业资质等 | — | **不接**，理由见 §4 |
| 城市背景 | `guide_chain` = `fixture,qwen_search` | 是/否 | — | — | 城市性格/特色小吃/安全提示 |
| 大模型 | 阿里百炼 DashScope（OpenAI 兼容） | 是 | 见 §2.4 | 中 | `providers/llm.py:DashScope`，`llm_chain` 唯一成员 |
| 大模型 | 智谱 `open.bigmodel.cn` / 硅基流动 `api.siliconflow.cn/v1` | 是 | GLM-4.x-Flash 有免费档 | 中 | 备选，尚未注册 provider |
| 汇率 | `open.er-api.com/v6/latest/CNY`、`api.frankfurter.dev/v1/latest?base=CNY&symbols=USD` | 否 | 无 | 低 | 免 key 可用，出境游客单价换算才需要（P2） |
| 车次 | `search.12306.cn/search/v1/train/search?keyword=G1&date=20261112` | 否 | 200 免鉴权 | 中 | **只查车次**；无票价无余票；余票接口要登录 cookie + 风控，**不写进 demo** |
| 行政区划 | 高德 `/v3/config/district`、`/v3/geocode/geo` | 是 | — | 低 | adcode 自维护在 `knowledge/cities.py`，未动态查 |
| 百科/底图 | `zh.wikipedia.org`、`nominatim.openstreetmap.org`、`overpass-api.de`(406) | 否 | — | **大陆直连不可达** | **不能作为依赖** |

**结论先行（§3 会展开）：只有高德 1 个 Key 就能把 POI 升级到全国任意城市实时数据，其余全部零密钥可跑。**

---

## 2. 每个源的申请步骤（点到按钮级别）

### 2.1 Open-Meteo — 零步骤

不需要注册、不需要 key。本项目请求：

```
https://api.open-meteo.com/v1/forecast?latitude=30.5728&longitude=104.0668
  &daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,
          precipitation_probability_max,wind_speed_10m_max,relative_humidity_2m_max,
          sunrise,sunset
  &timezone=Asia/Shanghai&forecast_days=16
```

参数拼装见 `providers/weather.py:OpenMeteo.fetch_forecast()`，`days` 被 `max(1, min(int(days), 16))` 夹在 16 以内（**接口上限就是 16 天**，这也是 README 里"预报只有 16 天"的根因）。

**两个必须知道的坑**：

1. 只返回 **WMO 数字码**（`weather_code`），没有中文。中文文案与灾害属性由 `knowledge/wmo.py:_TABLE` → `describe(code)` 得到（`WMOInfo(zh, en, icon, wet, storm, snow, fog)`）。新增码要同时改这张表，否则 `text` 变 `"未知"`，`weather_risk` 少一个判据。
2. **不要用它的中文 geocoding**。实测「丽江」会被解析到湖南岳阳的同名村庄，坐标直接错 1000 公里。所以本项目自建坐标表 `knowledge/cities.py:_BUILTIN`（71 个城市，含 `key/name/adcode/lat/lng/province/aliases/tags`），扩展点是 `data/fixtures/cities_extra.json`。

### 2.2 高德开放平台 Web 服务（**本项目唯一的推荐 Key**）

1. 打开 `https://console.amap.com/` → 登录（可用支付宝/淘宝账号）；
2. 左侧 **控制台 → 应用管理 → 我的应用 → 创建新应用**，填应用名与描述（描述随便写，不影响审核）；
3. 在应用卡片上点 **"添加 Key"**（"Key 名称"自取）；
4. **关键一步**：表单里 "Key 服务平台" 必须选 **`Web 服务`**。选成 `Android 平台`/`iOS 平台`/`JS API` 拿到的 key 打 `restapi.amap.com` 会一直报平台不匹配（高德 `info` 里带 `PLAT_NOMATCH` 字样；具体 `infocode` 以实际返回为准，别把某个码写死进代码——本项目只把 `data["info"]` 和 `data["infocode"]` 原样拼进 `ProviderError` 文本）；
5. 提交后进入 **配额与计费 → 实名认证 → 个人实名认证**：填 **身份证号码 + 人脸核验**，审核通常几分钟；未认证的 key 配额极低且部分接口不可用；
6. 复制 key，写进 `.env`：

```bat
copy .env.example api_keys\tripagent.env
notepad .env
```

```
AMAP_KEY=你的32位key
# 只有你在控制台"安全密钥"里开启了数字签名校验才需要填，否则留空
AMAP_SECRET=
```

本项目用到的端点（都已在代码里）：

| 端点 | 用途 | 代码位置 |
|---|---|---|
| `/v5/place/text` | 关键字搜 POI（带 `region`=adcode、`citylimit=true`） | `providers/poi.py:AMapPOI.search` |
| `/v5/place/around` | 周边搜（有 `near` 时自动切这个） | 同上 |
| `/v3/geocode/geo` | 地名→坐标（备用） | 未在链路中调用 |
| `/v5/direction/walking|driving|bicycling|transit/integrated` | 路径规划 | `providers/transit.py:AMapTransit.route` |
| `/v3/config/district` | 行政区划→adcode | 未在链路中调用 |
| `/v3/weather/weatherInfo` | 天气 | **不用**（只有 3~4 天） |

**必带的参数**：`show_fields=business,children`。不带 `show_fields=business` 就**拿不到 `rating`（评分）、`cost`（人均）、`opentime`（营业时间）**，本项目所有"生活娱乐"字段都从这里来（`poi.py:_amap_to_poi` 读 `row["business"]`，金额从元换成分：`int(round(cost * 100))`）。

**配额**（认证个人账号，示例值）：逆地理编码 30 万次/月、QPS 200；**超限只降级不扣费**（返回 `infocode` 非 `19xx` 的错误码，`AMapPOI.fetch()` 抛 `ProviderError(f"高德返回 {info}({infocode})")`，`Chain` 自动降级到 `fixture`）。

**可选数字签名**：若开启校验，需 `sig = MD5(参数按 key 升序拼接 + 安全密钥)`，本项目已实现于 `providers/poi.py:_sig()`（大写 MD5）。文档：`http://lbs.amap.com/faq/quota-key/key/41181/`。

**官方 MCP Server（省掉自己写适配层）**：`https://lbs.amap.com/api/mcp-server/gettingstarted`，SSE 端点：

```
https://mcp.amap.com/sse?key=<你的Web服务key>
```

在 Qoder 里加这个 MCP 后，可以直接让助手查 POI/路径，用来**验证 fixture 字段是否与线上一致**；生产链路仍走本项目的 `providers/poi.py`（可缓存、可降级、可控 QPS）。

**POI 分类编码表**：`https://lbs.amap.com/api/webservice/download` → 直链 `https://a.amap.com/lbs/static/code_resource/Amap_poicode.zip`。本项目落地的关键编码（同步在 `knowledge/poi_codes.py:AMAP_CATEGORY`）：

| 编码 | 含义 | 编码 | 含义 |
|---|---|---|---|
| `110000` | 风景名胜 | `080504` | 露营地 |
| `110100` | 公园广场 | `080600` | 影剧院 |
| `110200` | 风景名胜 | `080601` | 电影院 |
| `050000` | 餐饮 | `080602` | 音乐厅 |
| `050500` | 咖啡厅 | `080603` | 剧场 |
| `080000` | 体育休闲 | `100000` | 住宿 |
| `080300` | 娱乐场所 | `060000` | 购物 |
| `080302` | KTV | `080303` | 迪厅 |
| `080304` | 酒吧 | `080501` | 游乐场 |
| `140100` | 博物馆（**此码需再验证**，本项目同时给 keywords `"博物馆,美术馆,展览馆"` 兜底） | | |

`AMAP_CATEGORY` 每条是 `(types, keywords)` 二元组：`types` 用 `|` 连接多个编码，`keywords` 是**中文兜底检索词**——没 key 时同一份 keywords 也被 `FixturePOI._match()` 用来筛本地数据，保证两种模式行为一致。

### 2.3 预警两源 — 零步骤，但有一个硬要求

**中国气象局**（`CMAAlarm`）：

```
GET https://weather.cma.cn/api/alarm
Headers: User-Agent: Mozilla/5.0 (compatible; TripAgent/0.1; ...)   ← 不带就 403
```

响应结构：

```json
{"msg":"success","code":0,"data":[
  {"id":"23022941600000_20261105","headline":"…","title":"…","description":"…",
   "effective":"2026-11-05 20:35","type":"p0000003"}
]}
```

`id` **前 6 位是县级 adcode**——本项目用它做地域过滤（`providers/weather.py:select_alarms()`），比按地名文本匹配可靠得多。

**中央气象台**（`NMCAlarm`）：

```
GET https://www.nmc.cn/rest/findAlarm?pageNo=1&pageSize=200&signaltype=&signallevel=&province=&city=&county=
```

响应结构：

```json
{"data":{"page":{"count":454,"list":[
  {"alertid":"…","title":"…","issuetime":"…","url":"/publish/…","pic":"…"}
]}}}
```

`url` 是相对路径，`NMCAlarm.fetch()` 里给它补上 `https://www.nmc.cn` 前缀。

**台风**（JSONP，未来接）：`https://typhoon.nmc.cn/weatherservice/typhoon/jsons/list_default`。

**12379.cn**：本环境不可达，不要作为依赖。

> `User-Agent` 由 `providers/base.py:Provider.get_json()` 统一注入（值来自 `Settings.http_user_agent`），自己新写预警源不要绕过它。

### 2.4 通义千问（阿里百炼 DashScope）

1. 打开 `https://bailian.console.aliyun.com/` → 用**阿里云账号**登录（需完成阿里云实名认证）；
2. 首次进入按提示**开通百炼模型服务**（新用户各模型有免费额度）；
3. 左侧/API-KEY 管理 → **创建 API-KEY**，复制 `sk-` 开头的值；
4. 写进 `.env`：

```
DASHSCOPE_API_KEY=sk-xxxx
TRIPAGENT_LLM_MODEL=qwen-plus
```

代码里的通道（`providers/llm.py`）：

```python
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"   # OpenAI 兼容
client = OpenAI(api_key=..., base_url=DASHSCOPE_BASE_URL, timeout=settings.http_timeout * 3)
```

**最容易踩的坑**：`enable_search` 是**非标准参数**，在 OpenAI 兼容通道里必须放进 `extra_body`，直接当顶层 kwargs 会 `TypeError`：

```python
kwargs["extra_body"] = {"enable_search": True, "search_options": {"search_strategy": "max"}}
```

**额度不要写死在文档里**：新人每模型约 100 万 token / 90 天，学生认证另有代金券，**以控制台实时显示为准**。本项目每次规划的 LLM 用量很小（最多 3 个口碑 topic + 1 次 narrate + 1 次问答总结），并且**任一调用失败都不影响出方案**。

**备选（同 OpenAI 兼容思路，需要改 provider）**：智谱 `https://open.bigmodel.cn/api/paas/v4`（GLM-4.x-Flash 有免费档）、硅基流动 `https://api.siliconflow.cn/v1`。接法见 [SECOND_DEVELOPMENT.md](SECOND_DEVELOPMENT.md) Recipe 1：注册 `@provider("llm", "glm")` + 把 `llm_chain` 改成 `glm,dashscope`。

### 2.5 和风天气（可选，坑最多）

1. `https://console.qweather.com/` 注册并创建凭据；
2. **必须记下你自己的专属 API Host**（控制台里显示的那串 `xxx.qweatherapi.com`）。**旧的 `devapi.qweather.com` / `api.qweather.com` 现在一律返回 `Invalid Host` 403**；
3. `.env`：

```
QWEATHER_KEY=你的key
QWEATHER_HOST=你的专属host          # 两个都要填，只填 key 时 describe() 会明确提示缺 Host
```

用到的路径：`/v7/weather/15d`（退 `/v7/weather/7d`）、`/v7/warning/now`、`/geo/v2/city/lookup`（拿 LocationID）。免费额度已改为**后付费 0~5 万次/月**。

本项目 `QWeather` 同时注册在 `forecast` 和 `alarm` 两条链的首位（`@provider("forecast","qweather")` 和 `@provider("alarm","qweather")` 双装饰器），`available()` 要求 key 与 host 都非空。**填了它，16 天诉求会退化成 15 天**（和风上限如此），这是设计上的取舍，不是 bug。

### 2.6 中国天气网（weather.com.cn）——**不推荐**

`/data/sk/`、`/data/cityinfo/` 已停更；官方开放平台需**付费 key**（仅 7 天体验）。投入产出比太差，本项目不接。

### 2.7 政务/名录类公开数据（做 fixture 的最佳来源，合法且稳定）

| 来源 | 地址 | 能拿到什么 |
|---|---|---|
| 文旅部 · 景区名录 | `https://sjfw.mct.gov.cn/site/dataservice/rural?type=10` | 5A 级景区名录（358 家，页面标注 2025-03-11） |
| 文旅部 · 新公示 | `http://zwgk.mct.gov.cn/zfxxgkml/zykf/202611/t20261105_967575.html` | 2026-11-05 新公示 17 家 |
| 上海市公共数据开放 | `data.sh.gov.cn`（文旅数据专区） | 场所名录、开放数据接口 |
| 北京公共数据 | `data.beijing.gov.cn` | 同上 |
| 浙江公共数据 | `data.zjzwfw.gov.cn` | 同上 |
| 中国非物质文化遗产网 | `ihchina.cn` | 非遗项目名录（人文古迹类 POI 的 `why` 素材） |

这些是**结构化、可长期引用、可注明出处**的来源，适合人工整理成 `data/fixtures/<city>.json`；比爬 UGC 稳。整理规范见 [SECOND_DEVELOPMENT.md](SECOND_DEVELOPMENT.md) Recipe 4。

### 2.8 12306 车次查询（免鉴权，但只有半个接口）

```
GET https://search.12306.cn/search/v1/train/search?keyword=G1&date=20261112
→ HTTP 200，无需 cookie
```

**能拿到车次/时刻，拿不到票价与余票**；余票接口需要登录 cookie 且有风控。**不要写进演示**——本项目 `IntercityLeg` 的 `note` 就明确写着"时刻与票价请以 12306/航旅纵横 实时为准（无个人可用的开放接口，本条为占位建议）"。

---

## 3. 最小依赖集结论

| 你想要的效果 | 需要几个 Key | 怎么配 |
|---|---|---|
| 完整跑通（演示、答辩、离线） | **0 个** | 什么都不用填；`TRIPAGENT_OFFLINE=1` 更保险 |
| 全国任意城市的**实时 POI**（评分/人均/营业时间）+ 真实路径 | **1 个：`AMAP_KEY`** | §2.2；这是投入产出比最高的一个 key |
| 联网口碑归纳 + 行程文案 + 城市背景知识 | 再加 `DASHSCOPE_API_KEY` | §2.4 |
| 15 天官方预报 + 官方分级预警（替代 Open-Meteo + CMA/NMC） | 再加 `QWEATHER_KEY` + `QWEATHER_HOST` | §2.5；**注意会退到 15 天** |

`config.py` 的 7 条降级链默认值，恰好体现"零 key 可用、填 key 自动升级"：

```python
forecast_chain: str = "qweather,open_meteo,fixture"
alarm_chain:    str = "qweather,cma_alarm,nmc_alarm,fixture"
poi_chain:      str = "amap,fixture"
transit_chain:  str = "amap,estimated"
evidence_chain: str = "fixture,qwen_search"
guide_chain:    str = "fixture,qwen_search"
llm_chain:      str = "dashscope"
```

没填 key 时 `available()` 返回 False，`Chain.run()` 在 trace 里记 `不可用（缺 key 或离线）` 并直接落到下一个源，**不需要改任何代码**。想临时改顺序（例如让 `open_meteo` 抢在 `qweather` 前）可以直接用环境变量 `FORECAST_CHAIN=open_meteo,qweather,fixture` 覆盖，不必动源码。

---

## 4. 合规红线（不爬小红书 / 抖音 / 点评）与替代方案

### 4.1 为什么接不了官方接口（2026-11 现状）

| 平台 | 开放平台门槛 | 个人学生号能否拿到 UGC 内容接口 |
|---|---|---|
| 小红书 | 需**企业资质** + **逐项权限审核**，笔记接口不对个人开放 | ❌ |
| 抖音 | 开放平台需**企业资质** | ❌ |
| B 站 | 官方原文：**"暂未开通个人开发者的申请入驻"** | ❌ |
| 微博 | 高级接口需**应用上线满 30 天 + 营业执照** | ❌ |
| 马蜂窝 / 携程门票开放平台 / 去哪儿 / 飞猪 | 全是**商家侧**接口（订单/POI 上架），拿不到 UGC 内容 | ❌ |

### 4.2 为什么不用逆向方案

GitHub 上的 `xhs` / `RedNote` 逆向库与 `xiaohongshu-mcp`：

- **违反平台 ToS**（参赛作品的合规风险由作者承担）；
- **封号风险**（要用个人账号登录态，账号本身是学生的）；
- **随时失效**（签名/加密参数一变就全崩）。

比赛演示**不能**把可信度压在这种东西上。这也是 `README.md` 常见问题里那条回答的完整版。

### 4.3 本项目的合法替代：LLM 联网检索 + 显式溯源

`providers/evidence.py:QwenSearchEvidence.collect()`：让通义千问开 `enable_search` 检索公开网络内容并归纳。提示词（`_UGC_PROMPT`）里写死了三条纪律：

1. **`"url":"仅在确实有出处时填写，绝不编造，否则空字符串"`**；
2. **"对不确定的内容把 confidence 降到 0.4 以下"**（代码里 `confidence=float(r.get("confidence") or 0.4)`，缺省就是 0.4）；
3. **"只写公开可查的信息"**、"不要输出 JSON 以外文字"。

产出的 `EvidenceRecord` 强制打标：`kind="model_summary"`、`source="qwen_web_search"`、`id=f"qws:{md5(内容)[:12]}"`。展示层（`app.py` 问答页 `st.caption(f"网友：{e.brief(110)} · {e.source}"）`、`export/markdown.py` 的"依据"）会把 source 带出来，**读者一眼能区分"本地精选口碑库"与"模型联网归纳"**。

另外三条产品级合规动作（都在代码里，不是口号）：

| 动作 | 位置 |
|---|---|
| 夜间场所必带身份证提醒 | `planner._packing()` 追加"身份证（酒吧/夜店/KTV 需实名核验，人证不一致不得接待）"；`validator` 的 `nightlife_compliance` 检查这条提醒在不在 |
| 未成年人禁入 | `validator.kid_safe_venues`（`HARD`，weight 2.0）：带娃却排了酒吧/夜店/KTV 直接判未通过 |
| 营业时间未结构化时不装懂 | `scoring.score()` 会写一条 reason「营业时间未结构化，出行前请复核」 |

### 4.4 红线之外的取舍

- **不存 UGC 原文、不二次分发**：`evidence` 只留 `title/snippet/url/published_at`，且 `url` 允许为空；
- **本地知识库（fixture）全部是人工整理的公开常识与稳定信息**，`source="fixture"`、`confidence` 自报；
- **不做人脸/车牌/定位等个人数据采集**；`lodging_area` 只用于选酒店商圈，不落用户位置。

---

## 5. 数据源失效时的排查顺序

按这个顺序走，**不要一上来就怀疑引擎**。

### 第 1 步：看谁被判定不可用

```bat
.venv\Scripts\python.exe -m tripagent.cli doctor
```

输出四列 `能力 / provider / 状态 / 说明`。三种状态含义不同：

| 状态 | 说明来源 | 下一步 |
|---|---|---|
| `跳过 · 未配置 XXX_KEY` | `provider.available()` 返回 False（缺 key） | §2 对应小节去申请 |
| `跳过 · 不可用` | `available()` 还包含 `settings.online`，**说明当前是离线模式** | 关掉 `TRIPAGENT_OFFLINE` / 取消网页侧栏"离线演示模式" |
| 可用但实际请求失败 | 见第 2 步的 attempts | 第 2 步 |

`doctor()` 遍历的能力固定是 `("forecast","alarm","poi","transit","evidence","guide","llm")`（`agents/service.py:TripService.doctor`）。新注册 provider 没出现在这里，通常是你忘了在 `providers/__init__.py` 里 import 那个模块。

### 第 2 步：带城市实测每一条链

```bat
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe scripts\probe_apis.py 成都
```

它会打印 `[forecast] source=… 天数=…`、`[alarm] source=… 条数=…`、`[guide] source=…`、`[llm:…]`，并且**逐条列出每个 attempt**：

```
[✓ forecast:open_meteo] hits=16 812ms
[✗ alarm:qweather] 不可用（缺 key 或离线）
[✗ alarm:cma_alarm] HTTP 403 https://weather.cma.cn/api/alarm
```

`✗` 后面的错误串来自 `ProviderError`，被 `Chain.run()` 截断到 160 字符。常见对应关系：

| 错误串 | 真实原因 | 处理 |
|---|---|---|
| `HTTP 403 https://weather.cma.cn/api/alarm` | 没带 `User-Agent` | 检查是否绕过了 `Provider.get_json()` |
| `HTTP 403 … Invalid Host` | 和风用了旧域名 / 没填 `QWEATHER_HOST` | §2.5 |
| `高德返回 <info>(<infocode>)`，info 含 `KEY` 字样 | key 无效 / 平台选错 / 未实名 | §2.2 第 4、5 步 |
| `高德返回 …(infocode)` 含配额 | 超日/月配额 | **只降级不扣费**，`Chain` 会落到 `fixture`，先跑 `doctor` 确认兜底在 |
| `非 JSON 响应 …` | 上游返回 HTML（被网关/登录页拦截） | 换网络或用 `.cache` 里的上次成功结果 |
| `网络失败 …` | DNS/超时 | 调 `TRIPAGENT_HTTP_TIMEOUT`；确认不是 `TRIPAGENT_OFFLINE=1` |
| `open-meteo 返回空 daily 序列` | 坐标不在表里 / 上游异常 | 见第 3 步核对坐标 |

### 第 3 步：看磁盘缓存里的原始响应

`Provider.cached_call()` 把成功结果按 `Cache.key(*parts)`（sha1 of JSON 序列化的 key 部件）写进 `.cache/<hash>.json`，TTL 由 `TRIPAGENT_CACHE_TTL_HOURS`（默认 6 小时）控制。缓存文件就是**上游返回结构被本项目解析后**的模型列表。

```bat
dir /b /o-d .cache
.venv\Scripts\python.exe -c "import glob,json;p=max(glob.glob(r'.cache/*.json'),key=lambda f:__import__('os').path.getmtime(f));d=json.load(open(p,encoding='utf-8'));print(p);print(json.dumps(d[:3] if isinstance(d,list) else d,ensure_ascii=False)[:600])"
```

- **想拿真正的原始报文**：缓存里存的是解析后的模型，不含原始 JSON。排查结构变更时，直接用浏览器/curl 打一次端点看原文；或者临时把 `fetch()` 里的 `payload` 打印出来。
- **缓存 key 里有 `city.key`**：改了 `knowledge/cities.py` 的坐标后旧缓存仍在（`(("forecast","open_meteo",city.key,days))` 用的是 key 不是坐标），**换坐标后请删 `.cache` 或把 TTL 设 0**（`TRIPAGENT_CACHE_TTL_HOURS=0` 时 `Cache.get()` 直接返回 None，等于禁用读缓存）。
- 预警两源都带城市 key 维度缓存，所以"某城市一直显示很旧的一条预警"基本都是缓存，不是接口。

### 第 4 步：还是不行就显式降级到离线

```bat
.venv\Scripts\python.exe -m tripagent.cli plan "成都 3天 11月20日出发 爱吃火锅 预算1500" --offline
```

`--offline` 会设 `TRIPAGENT_OFFLINE=1` 并 `reload_settings()`；`Settings.chain()` 只保留 `LOCAL_PROVIDERS = {"fixture","estimated","offline"}`。

- 离线能跑通 → 问题在数据源/网络/key，引擎无罪；
- 离线也跑不通 → 看 `tests/` 是否仍 31 passed（`conftest.py` 就是离线模式），再定位到 `core/`。

```bat
.venv\Scripts\python.exe -m pytest -q
```

### 失效风险日历（按需要盯的频率排序）

| 需要盯 | 源 | 征兆 |
|---|---|---|
| **每次路演前** | 高德 key 配额与实名状态 | `USERKEY` 类 infocode；个人认证过期 |
| 每周 | DashScope 额度 / 模型可用性 | `chat()` 抛异常 → trace 里出现「失败，使用模板文案」 |
| 每月 | 和风 Host 与计费口径 | `Invalid Host` |
| 一次性确认 | CMA / NMC 是否改结构 | `probe_apis.py` 里 `[alarm]` 条数为 0 且不是"确实没预警" |
| 已确认失效，不必再看 | 中国天气网 `data/sk`、12379.cn、Wikipedia/OSM/Overpass | 本项目不依赖 |
