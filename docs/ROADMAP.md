# TripAgent 迭代计划

> 分档：**P0 = 2026-11-30 18:00 提交前必做**，**P1 = 决赛路演（12/1–12/15 线上直播）加分项**，**P2 = 长期**。
> 工时是**单人净工时**估算。所有项都以「跑完回归三连」为完成定义（见 [SECOND_DEVELOPMENT.md](SECOND_DEVELOPMENT.md) 开头）。
> 硬约束：不引入违反 ToS 的抓取，不为演示增加新依赖而破坏"零 key / 断网可跑"。

## 总览

| 编号 | 项 | 档 | 工时 | 外部依赖 |
|---|---|---|---|---|
| P0-1 | Qoder Sites 部署 + 开放公开访问 | P0 | 12h | Qoder 账号；**无需任何数据 Key** |
| P0-2 | 演示城市 fixture 补齐，与 README 说法对齐 | P0 | 8h | 无 |
| P0-3 | 高德 Key 到位：真实 POI + 真实路径灰度打开 | P0 | 6h | `AMAP_KEY` + **个人实名认证（身份证+人脸）** |
| P0-4 | Token 成本统计与缓存命中率 | P0 | 6h | 无（有 `DASHSCOPE_API_KEY` 时数据更全） |
| P0-5 | 断网演示预案与路演彩排 | P0 | 4h | 无 |
| P0-6 | 作品帖发布（天池赛题页论坛）+ 演示链接回填 | P0 | 4h | P0-1 完成 |
| P1-1 | 地图可视化（真实底图 + 点位） | P1 | 10h | 高德 **Web 端 JS Key**（与 Web 服务 Key 不同平台） |
| P1-2 | 增量改单：patch 而非全量重算（用 `plan_versions`） | P1 | 14h | 无 |
| P1-3 | 演出/票务时间窗接入（大麦 / 秀动类） | P1 | 16h | 需查是否有个人可用的开放接口，**很可能只能做"引导跳转"** |
| P1-4 | 评测集：按 TravelPlanner / ChinaTravel 口径做自动回归 | P1 | 20h | 无 GPU；需下载公开数据集 |
| P1-5 | 酒店 / 车票真实价格 | P1 | 12h | 无合规接口 → 大概率保持"占位 + 引导" |
| P1-6 | ~~意图 LLM 补漏接线~~ **已完成**：`plan_trip` 在规则解析留缺口时调用 `llm_fill_request`，补出的字段记入 `PlanOutcome.filled_by_llm` | — | — | 需 `DASHSCOPE_API_KEY` 才生效 |
| P1-7 | 无障碍模式 | P1 | 8h | 无 |
| P1-8 | 分享链接（只读快照） | P1 | 6h | 依赖 P0-1 的产物存储位置 |
| P1-9 | 台风专项：路径与影响日期 | P1 | 6h | `typhoon.nmc.cn` 免 key JSONP |
| P2-1 | 多人协作与批注 | P2 | 24h | 需 Sites 数据库后端授权 |
| P2-2 | 语音输入 | P2 | 10h | 浏览器 Web Speech API；中文识别建议走 DashScope |
| P2-3 | UGC 语义检索（向量库） | P2 | 24h | Embedding 额度；本地可用 sqlite-vec |
| P2-4 | 城市覆盖 200+ 与 fixture 数据治理流水线 | P2 | 30h | 政务开放数据源 |
| P2-5 | 无人值守 PDF 导出 | P2 | 8h | 新依赖（weasyprint 需 GTK），**谨慎** |
| P2-6 | 出境游：汇率 + 签证提示 + 多语言 | P2 | 12h | `open.er-api.com` 免 key |
| P2-7 | 把能力包成 MCP / 对外 API | P2 | 12h | 无 |

---

## P0 · 提交前必做

### P0-1 Qoder Sites 部署与公网可访问

**做什么**：按 [SECOND_DEVELOPMENT.md](SECOND_DEVELOPMENT.md) Recipe 7 的**方案 A（预生成静态站）**落地：新增 `site/` 目录，`site/dist/index.html` 是作品首页（3 个演示行程 + 架构说明 + 数据源与合规声明），`site/dist/trips/*.html` 是从 `output/` 拷来的单文件行程报告，`site/dist/data/*.json` 是 Recipe 5 的 JSON 产物；`site/functions/` 里放一个免 key 的 Open-Meteo 只读转发函数证明"有后端能力"。然后 `prepare_site` → `get_publish_status` → `publish_site`，**最后把访问策略改成 Public**。

**为什么**：这是**比赛的硬性要求**（必须用 Qoder 开发并部署到 Qoder Sites 开放公开访问），不做等于没参赛。同时它是评委能"自己点进去玩"的唯一入口，比任何截图都有效。

**涉及文件**：`site/dist/index.html`（新）、`site/dist/assets/`（新）、`site/functions/index.ts` + `site/functions/handler.ts`（新）、`src/tripagent/export/json_export.py`（新）、`src/tripagent/export/__init__.py`（`_EXT` + 映射 + `__all__` 各加一处）、`scripts/` 新增一个 `build_site.py` 做「跑 3 条演示诉求 → 导出 → 拷进 dist」。

**工时**：12h（其中 4h 花在踩 Sites 的打包约束上：`webDirectory` 不能是项目根、目录索引被禁用、`.env*` 被排除）。

**依赖**：Qoder 账号 + 完成一次真实 `publish_site`。**不需要数据 Key**（这是方案 A 的最大好处）。**风险**：新站点默认私有，改公开需要单独授权确认；Desktop 发布预览不执行 Functions，后端行为必须发布后拿真实 URL 复验。

### P0-2 演示城市 fixture 补齐

**做什么**：新增 `data/fixtures/harbin.json`、`data/fixtures/chongqing.json`（各 24–28 个 POI + 12 条 evidence + guide 六段），字段按 Recipe 4 的模板；同时把 `README.md` 目录结构那行"（chengdu/xian/changsha/hangzhou/chongqing/harbin.json）"改成与实际一致。

**为什么**：**当前仓库里只有 `chengdu / xian / changsha / hangzhou` 四个 fixture**，README 写了六个。路演脚本里必演的「哈尔滨冬季道路结冰」在 `--offline` 下会走 `FixturePOI` 的空池分支，页面只剩"暂无结构化景点数据"——这是会在现场被抓到的真实缺陷。哈尔滨冰雪 + 重庆夜生活的视觉冲击力恰好是我们的卖点。

**涉及文件**：`data/fixtures/harbin.json`（新）、`data/fixtures/chongqing.json`（新）、`README.md`（§4 目录结构一行）；城市坐标已在 `knowledge/cities.py:_BUILTIN`（`harbin`/`chongqing` 都有），不需要动。

**工时**：8h（数据整理占 6h，代码 0）。

**依赖**：无。数据来源用 §2.7 的政务/名录公开数据 + 高德 MCP 核对字段，**不爬 UGC**。

### P0-3 高德 Key 到位后的真实路径

**做什么**：`.env` 填 `AMAP_KEY` 后做三件事——① `probe_apis.py` + `doctor` 确认 `poi:amap`、`transit:amap` 变绿；② 用真实 POI 跑通成都/西安/长沙，比对 fixture 版差异（评分、人均、营业时间结构化率）；③ 通勤已从"直线估算"升级为真实路径，且 `validator.walk_leg_within_limit` 已消费 `max_walk_minutes_per_leg=35`；下一步接的是**换乘首末班校验**（`transit` 返回的线路名与末班时间已在 `RouteLeg.summary` 里，缺的是"散场时间 vs 末班车"的硬约束）。

**为什么**：没有真实路径，通勤是直线估算，"不折返"约束只能定性；接上之后路线图与 `transit_before.summary`（换乘线路名）才有说服力。

**涉及文件**：`.env`、`src/tripagent/providers/transit.py`（`_parse_amap_route` 的 `transit/integrated` 分支需实测校准）、`src/tripagent/core/validator.py`、`src/tripagent/providers/poi.py`（`sort_rule` 与 `show_fields` 实测调优）。

**工时**：6h + 实名认证等待（身份证 + 人脸，通常几分钟，但**别卡在最后一天**）。

**依赖**：`AMAP_KEY`（**Key 服务平台必须选「Web 服务」**）；不需要 GPU。注意配额是"超限只降级不扣费"，所以最坏情况退到 fixture，不会崩。

### P0-4 Token 成本统计与缓存命中率

**做什么**：

1. `providers/llm.py:DashScope.chat()` 里把 `resp.usage`（`prompt_tokens` / `completion_tokens`）累加到 provider 实例计数器，并让 `json_call`/`chat` 返回时带上下文；
2. `providers/base.py:Cache.get()` 记 `hit/miss` 计数（**注意 `_dump`/`_revive` 的现有语义不要改**），`Chain.run()` 的 `Attempt` 已有 `ms` 与 `hits`，再补一个 `cached: bool`；
3. `TripService.plan_trip()` 把汇总写进 `plan.extras["cost"]`（LLM 调用次数、总 token、缓存命中/未命中、各能力实际来源 `sources_used`）；
4. `cli.py` 的 `plan` 尾部与 `app.py` 的「数据源自检」页展示。

**为什么**：这是把"零 key 也能跑"从口号变成证据的东西，也是评委必问的"AI 成本多少"。有了数字才能说"一次规划 ≈ N 千 token、命中率 X%，边际成本 ¥Y"。**额度以控制台实时显示为准**，不要写死单价。

**涉及文件**：`src/tripagent/providers/llm.py`、`src/tripagent/providers/base.py`（`Attempt` dataclass 加字段，`Chain.run` 里赋值；`Attempt.__str__` 同步）、`src/tripagent/agents/service.py`、`src/tripagent/cli.py`、`app.py`。

**工时**：6h。

**依赖**：无。有 `DASHSCOPE_API_KEY` 时才有 token 数据，没有则统计恒为 0（这条要在 UI 里写清楚，别让人以为坏了）。

### P0-5 断网演示预案与路演彩排

**做什么**：把三条必演 query 在**拔网**状态下各跑 2 遍；确认 `--offline`（CLI）与侧栏「离线演示模式」（网页）两条路径都能出完整行程；准备一张"网络挂了怎么办"的一页纸；记录 `--offline` 下 `FixtureWeather.fetch_alarms()` 是否恰好造出演练预警（它有 12% 概率），必要时改用固定日期做演示以保证画面稳定。

**为什么**：`TRIPAGENT_OFFLINE=1` 时 `Settings.chain()` 只保留 `LOCAL_PROVIDERS = {"fixture","estimated","offline"}`，是我们的保险丝；但**没人验证过的保险丝不算保险丝**。另外离线预警是概率性生成的，路演时"忽有忽无"会显得不专业。

**涉及文件**：无代码改动；`docs/` 新增一份 `DEMO_CHECKLIST` 内容并入 [COMPETITION.md](COMPETITION.md) 的现场演示脚本（**不另建文件，直接更新 COMPETITION.md**）。

**工时**：4h。

**依赖**：无。

### P0-6 作品帖发布

**做什么**：按 [COMPETITION.md](COMPETITION.md) 的骨架在天池赛题页论坛发帖，回填 P0-1 的公网链接、3 张截图、架构图。**截止时间 11-30 18:00**，与作品发布同一天不可延期。

**工时**：4h。**依赖**：P0-1（链接）、P0-5（截图要在演示环境里截）。

---

## P1 · 决赛路演加分

### P1-1 地图可视化

**做什么**：在 HTML 报告与 Streamlit 新增「地图」页签里引入高德 JS API：真实底图 + 带编号的点位 marker + 折线；保留现有 SVG 作为**降级形态**（无 JS Key / 断网时自动回退）。

**为什么**：现在的 SVG 是"顺序/折返示意图"，不表达真实道路与比例尺，答辩时我们必须自己承认这个局限（`ARCHITECTURE.md` §4.8）。真地图能把"夜间返程""同街区聚类"这类卖点一眼看出来。

**涉及文件**：`src/tripagent/export/html.py`（`build_context` 增加 `amap_map: bool` 与 `points` JSON）、`src/tripagent/export/templates/report.html.j2`（`{% if map_raw %}{{ map_raw | safe }}{% endif %}` + 懒加载 script）、`app.py` 新页签（Recipe 6）。

**工时**：10h。**依赖**：**高德 Web 端 JS Key**（与 §2.2 的 Web 服务 Key 是两个平台，要再建一次）；JS SDK 需联网加载 → 必须设计成离线可降级，否则破坏 P0-5。

### P1-2 增量改单（patch 而不是全量重算）

**做什么**：新增 `TripService.patch_plan(trip_id, instruction)`：从 `TripStore.load()` 取上一版 → 定位被点的 `Slot` → 只重跑「该日的候选池 + 打分 + 该日之后的预算余量」→ 产出 `plan_version+1` 的新版本，`reason` 写成人类可读的"替换 Day2 下午的 X 为 Y"。UI 提供"换掉这一站 / 这天别去太远的地方 / 预算再压 200"。

**为什么**：① `memory/store.py` 的 `plan_versions(trip_id, version, created_at, reason, plan_json)` 是**不可变追加**，天生为版本 diff 设计，现在只有"存"和"读"，没有"改"，能力浪费；② 全量重算会连带改掉用户已经认可的其它天，体验是"我让你换一个店，你把三天重排了"；③ 重算还要再打一遍外部 API，成本与延迟都高。

**涉及文件**：`src/tripagent/memory/store.py`（加 `diff_versions(a,b)` 之类的只读方法）、`agents/service.py`、`core/planner.py`（需要把 `DayAssembler` 改造成可只跑单日并保持 `used_names` / `budget_left` 上下文）、`validator`（改完必须重跑全量校验）、`app.py` 行程页 + `cli.py` 新子命令。

**工时**：14h。**依赖**：无。**难点**：`DayAssembler` 的跨日状态（`used: set[str]`、`budget_left`）要能重建，`ItineraryPlanner.plan()` 的"风险日优先分配"顺序也要保留，否则增量结果与全量结果不一致。

### P1-3 演出 / 票务时间窗接入

**做什么**：新增 `providers/shows.py`，能力名 `show`，`config.py` 加 `show_chain: str = "damai,fixture"`；`POIRecord` 侧用 `need_reservation / reservation_channel / reservation_cutoff` + 新增 `extras` 表达"场次时间/余票状态"；装配时把 `SlotType.SHOW` 的时段**改为按场次时间倒推**（现在只按 `night_end_minute=1440` 顺排）。

**为什么**：剧场演出是本项目 `SHOW` 类目的核心，但现在是"19:30 给你留 120 分钟"这种近似。真实场景里 19:30 开演意味着 19:00 前必须离场赶路、散场后 21:30 才有夜场——这是"生活娱乐"赛道最贴题的细节，也是**评委一听就懂**的改进。

**涉及文件**：`providers/shows.py`（新）、`providers/__init__.py`（加一行 import，否则注册表里没有）、`config.py`、`core/planner.py:_fill_night`、`models/poi.py`（用 `raw`/`extras` 或正式加字段——加字段属于契约变更，要同步测试）。

**工时**：16h。**依赖**：**关键前置调查**——大麦/秀动有没有个人可申请的开放接口。**据本项目经验（见 DATA_SOURCES §4.1），票务类平台几乎全是商家侧接口**；拿不到就走"官方小程序/网页引导跳转 + fixture 场次"，**不做逆向抓取**，这条要写进合规声明。

### P1-4 评测集与自动回归

**做什么**：把公开数据集的约束口径落成 JSON 用例，跑批输出通过率矩阵。具体：以 `TravelPlanner` 的 env/commonsense/hard 三分法为准（与本项目 `ConstraintKind` 一一对应），挑 30–50 个"中文城市 + 天数 + 预算 + 人群"组合，每个组合固定一份候选池快照，断言 18~19 条约束的期望结果；再加一组"天气注入用例"（把某天改成暴雨/台风橙警，断言 `weather_adapted` 与 `blocking_advice`）。

**为什么**：现在只有 31 条单测，覆盖的是**单元行为**，不覆盖"整份行程端到端合不合理"。路演时说"通过率 92%"必须能回答"在什么集上测的"。有了固定评测集，之后每次调打分权重都能看回归差异，避免"改一个阈值崩三个场景"。

**涉及文件**：`tests/golden/`（新增用例 JSON + 期望结果）、`tests/test_e2e_golden.py`（新）、`scripts/eval_run.py`（新，输出表格）、`data/fixtures/`（评测要固定候选池，避免依赖外部 API）。

**工时**：20h。**依赖**：**不需要 GPU**（本项目全程不调本地模型）；需要能下载 TravelPlanner / ChinaTravel 的公开数据（境内网络可能要手动下载后入库）。**注意**：不宣称"在这两个基准上取得 SOTA"，只借它们的**约束口径**做自己的回归。

### P1-5 酒店 / 车票真实价格

**做什么**：酒店保持现有"fixture 或高德 POI 的 `avg_cost_cents`（人均/参考价位）× 人数 × 晚数"口径，加一条 `hotel_price_source` 到 `extras`；车票评估接入 §2.8 的 12306 车次查询（**只有车次与时刻，无票价无余票**），把 `IntercityLeg.cost_cents` 从 0 改成"标注为估算"。

**为什么**：预算是 `weight=3.0` 的最重约束，现在住宿价来自高德 `business.cost`（并非实时房价），车票是占位。诚实标注比假装精确更重要——`_intercity()` 的 `note` 已经在说"本条为占位建议"。

**涉及文件**：`core/planner.py:_intercity`、`models/plan.py:IntercityLeg`、`agents/service.py:pick_lodging`、`export/*`（价格来源标注）。

**工时**：12h。**依赖**：**没有个人可用的实时房价/余票接口**（余票接口需登录 cookie 且有风控，明确不写进 demo）。所以这一项的正确目标是**"更准确地承认不精确"**，不是接爬虫。

### P1-6 意图 LLM 补漏接线（已完成）

**现状**：已在 `TripService.plan_trip()` 接线——当 `settings.dashscope_api_key` 存在**且**规则解析留下缺口（城市 / 兴趣 / 出发日期 / 预算任一为空）时调用 `agents/enrich.py:llm_fill_request(req, chain)`，补出的字段名记入 `PlanOutcome.filled_by_llm` 并在 CLI/UI 的 trace 里显示"哪几项是模型补的"。没有 key 时这一步整体跳过，规则解析独立成立。

**待办**：`req.missing` 目前只在"完全没提到"时触发；像"下个月第二个周末""预算大概两三千"这类**说法在但解析失败**的情形，还需要在 `core/intent.py` 里补一个"识别到线索却解析不出"的标记位，才能把 LLM 补漏的触发条件从"字段为空"收紧到"字段可疑"。

**为什么**：规则解析对"下个月第二个周末""预算大概两三千"这类说法会失败；LLM 补漏是它最该干的活，而且函数已经写好了，**只差 3 行接线**。同时 `filled_by_llm` 让"模型补的"与"用户说过的"可区分，符合可解释性主线。

**涉及文件**：`agents/service.py`（`plan_trip` 第 1 步之后）、`cli.py:_print_request`、`app.py`。

**工时**：3h。**依赖**：`DASHSCOPE_API_KEY`。无 key 时静默跳过（函数内部已有 `if not chain.settings.dashscope_api_key: return []`）。

### P1-7 无障碍模式

**做什么**：两条线。① 产品功能线：`Party.accessible` 与 `Interest.ACCESSIBLE` 已存在（`intent.parse_party` 已识别"轮椅/行动不便/无障碍/腿脚"），把它从打分（`scoring` 里 `p.accessible and poi.accessible is False` 扣 40）提升为**硬约束**（新增 `hard` 约束：行程内不得出现 `accessible is False` 的已确认场所，`None` 放行），并在导出里输出"无障碍动线备注"；② UI 线：Streamlit 加"无障碍浏览"开关（大字号、高对比、纯键盘可达、表格加 `aria-label`），HTML 模板已有 `role="img"` / `aria-label` 的 SVG 继续补齐。

**为什么**：LifeHack 赛道讲"生活"，无障碍是真正的生活议题，也是本项目的 `Crowd`/`Party` 模型已经预留但没用满的能力。UI 侧 HTML 报告已按 `@media print` 与响应式做过，加高对比模式成本低。

**涉及文件**：`core/validator.py`（新约束）、`core/scoring.py`（阈值）、`export/markdown.py` + `export/templates/report.html.j2`（无障碍提示区块）、`app.py`。

**工时**：8h。**依赖**：无。**注意**：加约束会改变 `pass_rate()` 分母，要和 P1-4 的评测集一起做，否则历史通过率不可比。

### P1-8 分享链接（只读快照）

**做什么**：把一份 `TripPlan` 的 JSON 产物 + HTML 报告以 `trip_id` 为路径发布到 `site/dist/share/<trip_id>.html`，生成可分享 URL。不做登录、不做编辑。

**为什么**：路演的自然动作是"扫码看我的行程"。它也是 P2-1（多人协作）的最小可行前菜。

**涉及文件**：`scripts/build_site.py`（P0-1 引入）、`export/__init__.py`（Recipe 5 的 json 格式）、`site/dist/share/`。

**工时**：6h。**依赖**：P0-1。注意 Sites 打包限制（单文件 ≤50 MiB、总量控制），分享页要做数量上限。

### P1-9 台风专项

**做什么**：新增 `@provider("alarm", "nmc_typhoon")` 解析 `https://typhoon.nmc.cn/weatherservice/typhoon/jsons/list_default`（JSONP，需剥壳），产出 `AlarmRecord` + `extras` 里的路径点，让 `weather_risk._BLOCKING_ALARMS["台风"]` 能按"影响日期"精确到日而不是整窗保守处理。

**为什么**：现在 `assess()` 对提取不到生效日期的预警按"窗口期整体生效"保守处理；台风是**唯一有时间轴的自然灾害**，接上后"建议改期窗口"会显著更准。夏季路演（暑期游场景）说服力直接拉满。

**涉及文件**：`providers/weather.py`、`config.py:alarm_chain`、`core/weather_risk.py:_alarm_dates`。

**工时**：6h。**依赖**：免 key。JSONP 解析要处理回调名变化，做成失败即降级。

---

## P2 · 长期

| 项 | 做什么 / 为什么 | 主要文件 | 依赖 |
|---|---|---|---|
| **P2-1 多人协作与批注** | 分享链接升级为"同行人可投票/留言/认领 AA"。真实生活需求，也是 `trips` 表加 `owner` 的自然演进 | `memory/store.py`（新表）、`agents/service.py` | **需要 Sites 数据库后端与用户身份**：`prepare_site.databaseAccess=read_write` + `schemaRef`；写操作需同源校验；`x-qoder-user-context` 只是上下文不是签名，隔离性必须实测 |
| **P2-2 语音输入** | 长辈用户不会打字。Web Speech API 起步，中文识别不稳时退回 DashScope 语音 | `app.py`（新组件）、新增 `providers/speech.py` | 浏览器支持；DashScope key |
| **P2-3 UGC 语义检索** | fixture 涨到几千条后，别名+子串匹配会失效，才值得上向量库（本项目现在**刻意不做**，见 `ARCHITECTURE.md` §5 与 zhilv-yuntu 的差异说明） | `providers/evidence.py`、新增 `knowledge/embeddings.py` | Embedding 额度或本地模型（**这时才可能需要 GPU/较大内存**）；新增 sqlite-vec 依赖 |
| **P2-4 城市覆盖 200+** | 用 §2.7 的政务/名录公开数据做批量整理流水线 + 人工抽检；配套字段完整度校验脚本 | `data/fixtures/`、`knowledge/cities.py`、新增 `scripts/lint_fixtures.py` | 无；主要是人力 |
| **P2-5 无人值守 PDF** | 现在靠浏览器 `Ctrl+P`（HTML 模板已带 `@media print`）。真 PDF 需 `weasyprint`（要 GTK）或 `reportlab`（要中文字体） | `export/pdf_export.py`（新）、`export/__init__.py` | **会新增重量级依赖，与"零依赖可演示"冲突** — 除非有明确需求，否则不做 |
| **P2-6 出境游** | 汇率 `open.er-api.com/v6/latest/CNY` 或 `api.frankfurter.dev`（都免 key）、签证提示、多语言 UI | `providers/fx.py`（新）、`models/common.py`（加币种） | 无（免 key）；但小红书式境外 UGC 同样拿不到 |
| **P2-7 MCP / API 化** | 把 `plan` / `ask` 包成对外工具，让别的 agent 调用；或直接用高德官方 MCP（`https://mcp.amap.com/sse?key=<Web服务key>`）替换自研 POI 适配层做 A/B | 新增 `providers/mcp_server.py` 或 `serve.py` | 无；注意 TREK 式"工具门控"会重新引入不可控性，只在**校验层仍由本项目独占**时开放 |
| **P2-8 移动端 / 小程序** | 行程是天然移动场景；`.ics` 已是最低成本方案 | 无（产物已就绪） | 需要另一套前端工程 |

---

## 排序原则（为什么这么排）

1. **P0 只做"没有就不能参赛"和"会被当场抓到"的事**：部署、fixture 与 README 不一致、离线预案、成本可观测。
2. **P1 只排"能让评委多给分"且不需要新平台授权的事**；需要企业资质/商家接口的（票务、房价）**不做**，只做"更准确地标注不确定"。
3. **P2 才是能力扩张**（协作、语音、向量检索、出境），它们的共同点是**改变产品边界**，而不是改变分数。
4. 任何 P1/P2 项若要**加约束或改打分权重**，都必须和 P1-4（评测集）同批做，否则历史通过率不可比，记分卡就失去了意义。
