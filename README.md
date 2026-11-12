---
# 魔搭创空间（ModelScope Studio）元数据：删掉这段就变回普通 README
# 运行方式选的是 Streamlit（平台直接跑根目录 app.py + requirements.txt）。
# 想切回 Docker 就把 sdk 改成 docker 并加回 app_port: 7860，仓库里的 Dockerfile 随时可用。
title: TripAgent 山水行旅 · 会看天气的旅行管家
emoji: 🧭
colorFrom: green
colorTo: gray
sdk: streamlit
pinned: false
license: apache-2.0
domain: lifestyle
tags:
  - travel
  - agent
  - streamlit
  - weather
short_description: 说人话就能拿到会避开极端天气、每条推荐都有依据的专属行程
---

# TripAgent · 会看天气的生活娱乐旅行管家

> 天池 2026 Qoder 高校创造力大赛 · **LifeHack 生活娱乐** 赛道参赛作品
> 一句话：说人话（"成都 3 天 带爸妈 爱吃火锅 预算 3000"），拿到一份**会避开极端天气、每条推荐都有依据、并且逐条通过约束校验**的专属行程；追问"附近有什么评分高的清吧"也能答。

---

## 1. 三分钟跑起来（先看这个）

**用什么软件打开**：直接用 **Qoder**（本对话所在的 IDE，VS Code 内核）打开本目录即可——内置终端、可断点调试、无需再装 Visual Studio。
配置已写在 [.vscode/launch.json](.vscode/launch.json)，按 `F5` 就能带调试启动。

**环境**：Python 3.12（本机已装），依赖全部装在**项目内 `.venv`**，不污染系统 Python。

```bash
# 在项目根目录 D:\APP_develop\qoder-agent-develop\travel-agent 下
# —— 网页版（推荐，演示效果最好）——
.venv\Scripts\python.exe -m streamlit run app.py

# —— 命令行版 ——
.venv\Scripts\python.exe -m tripagent.cli plan "成都 3天 11月10日出发 人均预算800 爱吃火锅 想体验夜生活"
.venv\Scripts\python.exe -m tripagent.cli ask "成都有哪些评分高、性价比好的清吧？"
.venv\Scripts\python.exe -m tripagent.cli weather 哈尔滨
.venv\Scripts\python.exe -m tripagent.cli doctor
```

嫌路径长可以双击 [run_web.bat](run_web.bat)（网页）或 [run_cli.bat](run_cli.bat)（命令行）。
**如果双击 .bat 没反应或报"无法打开"**，改用下面任一方式（与 .bat 文件关联无关）：

- 右键 [run_web.ps1](run_web.ps1) → **使用 PowerShell 运行**
- 或在 Qoder 终端里粘贴上面那条命令（最可靠，推荐）

> 注意：网页版必须**先有进程在跑**才能打开 `http://localhost:8501`。这个地址不是常驻服务，
> 关掉运行中的终端窗口就打不开了 —— 重新执行启动命令即可。

**一个 Key 都不用也能完整跑通**：天气走免密钥公开接口，地点/口碑走本地知识库。
填 Key 后自动升级，**不需要改任何代码**。密钥集中在专用目录 `api_keys/`（已被两层 `.gitignore` 排除，不会进 GitHub）：

```bash
# 只需要编辑这一个文件：
api_keys\tripagent.env
```

```ini
DASHSCOPE_API_KEY=sk-xxxx        # 通义千问：联网口碑归纳 + 行程文案 + 意图补漏
AMAP_KEY=xxxxxxxx                # 高德 Web 服务：任意城市实时 POI（评分/人均/营业时间）+ 真实路径
```

推送 GitHub 前先跑一次密钥扫描（会真的拦截泄露，不是摆设）：

```bash
.venv\Scripts\python.exe scripts\check_secrets.py
```

---

## 2. 主程序在哪里

| 入口 | 文件 | 说明 |
|---|---|---|
| **网页版** | [app.py](app.py) | Streamlit，5 个页签：行程规划 / 即时问答 / 数据源自检 / 历史 / 关于 |
| **命令行版** | [src/tripagent/cli.py](src/tripagent/cli.py) | `plan / ask / weather / doctor / cities / history / show / demo` |
| **核心引擎（被上面两者调用）** | [src/tripagent/agents/service.py](src/tripagent/agents/service.py) | `TripService.plan_trip()` / `.ask()` / `.doctor()` |

`python -m tripagent.cli` 与直接 `python src/tripagent/cli.py` 等价；已在 `pyproject.toml` 里注册了 `tripagent` 命令名。

---

## 3. 它到底做了什么（流水线）

```
一句中文
  └─ understand   core/intent.py      规则解析：城市/天数/日期/预算/人群/兴趣/禁忌/节奏
  └─ weather      providers/weather.py 16 天预报（Open-Meteo 免密钥）+ 实时预警（CMA/NMC，按 adcode 过滤到本市）
  └─ assess_risk  core/weather_risk.py 逐日风险：暴雨/高温/寒潮/大风/雷暴/结冰 → 适宜·关注·警告·危险
  └─ gather       providers/poi.py     地点：高德 POI（有 Key）或本地知识库（无 Key），类目含酒吧/KTV/夜店/剧场/乐园/密室
  └─ gather       providers/evidence.py 口碑：通义千问 enable_search 联网归纳（无 Key 时用本地精选口碑库）
  └─ refine       agents/enrich.py     UGC 提纯：把「预约窗口/游玩时长/避坑/最佳时段」写回 POI 字段
  └─ assemble     core/planner.py      按天装配：风险日优先分配室内名额、锚点+最近邻贪心、预算按「分」裁剪
  └─ validate     core/validator.py    18~19 条约束逐项打分（env 4 / commonsense 8 / hard 6+1 条件项），产出通过率记分卡
  └─ narrate      agents/enrich.py     整体点评与逐日亮点（失败自动退回事实型模板文案）
  └─ export       export/              Markdown / HTML(含 SVG 路线图) / .ics 日历
```

**四个和同类项目不一样的点**

1. **天气是决策者不是装饰**：出现台风/暴雨红色等高等级预警时直接给「不建议出行」，并在 15 天窗口里推荐改期日期；警告级天气则自动把户外换成博物馆/剧场/商场（`weather_adapted` 约束会验证这件事）。
2. **方案有记分卡**：借鉴 [TravelPlanner](https://github.com/OSU-NLP-Group/TravelPlanner) 的约束三分法，输出可展示的通过率，而不是"AI 说行就行"。
3. **生活娱乐是一等公民**：酒吧/KTV/夜店/剧场/密室/温泉有独立类目、准入规则（身份证、未成年人禁入、低消）、跨零点营业时间，以及"散场后能不能回酒店"的夜间返程提示。
4. **降级链可观测**：`doctor` 与每次运行的 trace 都会告诉你"谁被跳过、为什么、数据来自哪"，断网现场演示不会翻车。

---

## 4. 目录结构

```
travel-agent/
├─ app.py                  ← 网页入口（streamlit run app.py）
├─ run_web.bat / run_cli.bat  ← Windows 双击启动
├─ requirements.txt / pyproject.toml / .env.example
├─ api_keys/                  ← 密钥专用目录（tripagent.env 不入库；目录内另有一层 .gitignore 兜底）
├─ scripts/                   ← probe_apis.py 探活、check_secrets.py 推送前密钥扫描
├─ src/tripagent/
│  ├─ config.py            ← 所有开关与降级链都在这里
│  ├─ models/              ← 数据契约（TripRequest / POIRecord / TripPlan / ValidationReport …）
│  ├─ knowledge/           ← 城市坐标表、WMO 天气码、高德 POI 分类编码、类目↔兴趣映射
│  ├─ providers/           ← 外部数据源适配层（base.py 是注册表+降级链+磁盘缓存）
│  ├─ core/                ← 规则引擎：intent / weather_risk / scoring / planner / validator
│  ├─ agents/              ← 编排：service.py（主）+ advisor.py（即时问答）+ enrich.py（UGC 提纯/文案）
│  ├─ memory/store.py      ← SQLite 历史与不可变版本
│  ├─ export/              ← Markdown / Jinja2 HTML / ICS
│  └─ cli.py
├─ data/
│  ├─ fixtures/            ← 离线知识库（chengdu/xian/changsha/hangzhou/chongqing/harbin.json）
│  └─ tripagent.db         ← 运行时生成
├─ scripts/probe_apis.py   ← API 探活（改 provider 后先跑它）
├─ tests/                  ← 31 个用例，全部离线确定性，`python -m pytest`
├─ output/                 ← 导出的行程报告
└─ docs/                   ← 架构、二次开发、数据源、参赛材料
```

---

## 5. 常见问题

- **中文乱码**：终端请先 `set PYTHONIOENCODING=utf-8`（`.bat` 脚本已内置）。
- **`python` 命令找不到**：本机 Python 3.12 通过 `py -3.12` 调用；项目内一律用 `.venv\Scripts\python.exe`。
- **想换城市**：`tripagent.cli cities` 看已收录城市；新城市加 `data/fixtures/cities_extra.json`（key/name/adcode/lat/lng）即可，坐标缺失会导致通勤估算与路线图退化。
- **预报只有 16 天**：Open-Meteo 上限如此。出发日期超出窗口时，校验会报 `行程日期均有预报` 未通过，这是设计行为不是 bug。
- **为什么没有小红书/抖音接口**：两家开放平台都要求企业资质 + 逐项权限审核，个人学生号拿不到内容接口，逆向爬取违反 ToS 且随时失效。本项目改用「通义千问联网检索 + 口碑归纳」，并显式标注来源与置信度。详见 [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)。

---

## 6. 下一步做什么

- 二次开发指南（加数据源 / 加约束 / 加类目 / 加城市）：[docs/SECOND_DEVELOPMENT.md](docs/SECOND_DEVELOPMENT.md)
- 架构与数据契约：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 数据源清单、配额与申请步骤：[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)
- 参赛提交与路演材料：[docs/COMPETITION.md](docs/COMPETITION.md)
- 迭代计划（已排好优先级）：[docs/ROADMAP.md](docs/ROADMAP.md)
