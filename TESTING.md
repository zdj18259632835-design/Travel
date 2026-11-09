# 怎么自己跑、怎么测、怎么把 bug 说清楚

这份文档只干一件事：让你**不依赖我**就能把程序跑起来、点一遍、发现问题、并且改得动。

---

## 1. 三个入口，先用哪个

| 入口 | 怎么启动 | 适合什么时候用 |
|---|---|---|
| **网页版（先玩这个）** | `.venv\Scripts\python.exe -m streamlit run app.py` | 5 个页签全都能点，最接近评委看到的形态 |
| 命令行 | `.venv\Scripts\python.exe -m tripagent.cli plan "成都 3天 预算800"` | 看结构化输出、验证单条逻辑、跑 demo |
| HTTP API | `.venv\Scripts\python.exe -m tripagent.cli serve --port 8000` | 前端联调、测部署链路，浏览器开 `http://127.0.0.1:8000/docs` 能直接点接口 |

双击 `run_web.bat` 等价于第一条。网页起来后浏览器自动开 `http://localhost:8501`。

**改代码后怎么生效**：Streamlit 页面右上角 `Always rerun` 或按 `R`；CLI/API 重新运行即可（API 加 `--reload` 会自动重启）。

---

## 2. 十分钟验收清单（照着点，每步都该看到什么）

打开网页版，按顺序做：

- [ ] **侧边栏**：三个 Key 状态应显示 `✓ 已配置`（DashScope、高德）。若显示 ✗，先查 `api_keys\tripagent.env` 是否被读到：跑 `.venv\Scripts\python.exe -m tripagent.cli doctor`。
- [ ] **行程规划页**：城市选「成都」，天数 3，勾上「安排夜生活」，点**开始规划**。
  期望：出现 16 天天气表（有"舒适"和"风险"两列）→ 3 个 Day 页签 → 预算表 → 「方案校验记分卡」四个百分比指标 → 三个下载按钮。
- [ ] **天气表**：任意一行的"风险因素"若为 `气象预警·xxx`，说明实时预警进来了（不是我们编的）。
- [ ] **记分卡**：点开看每条约束。全绿时 verdict 应是"方案可直接执行"。有 ⚠️ 时，`说明` 列必须写清是哪一天哪个项目。
- [ ] **即时问答页**：输入 `成都有哪些评分高、性价比好的清吧` → 应有表格 + 一句结论 + 至少一条身份证/末班车提醒。
- [ ] **数据源自检页**：`poi / amap` 应显示"可用"。
- [ ] **历史页**：刚才那次规划应出现在列表里（存在 `data\tripagent.db`）。
- [ ] **下载**：点 HTML 下载，浏览器打开——**逐日卡片里应有小地图路线**（SVG 折线 + 编号圆点）。

**必测的 4 个刁钻输入**（这几个最容易暴露 bug）：

```
带娃 + 夜生活    →  成都 3天 带一个小孩 想体验夜生活 预算800      期望：不出现酒吧，夜场是演出/密室，kid_safe_venues 通过
极端预算         →  重庆 2天 预算300                              期望：住宿警告或 budget_within 未通过，而不是硬排贵的
冷门城市         →  潮州 2天 预算1500                              期望：trace 里出现"动态识别城市"，能出方案
冬季冰雪         →  哈尔滨 3天 想看冰雪 带爸妈 预算3000             期望：Day 主题出现"室内优先（低温大风）"，行前清单有保暖/防滑
```

命令行一次跑完上面四个：`.venv\Scripts\python.exe -m tripagent.cli demo`

---

## 3. 出问题了先看这三处

1. **trace**：网页版「数据来源与降级轨迹」折叠区 / CLI 输出顶部 / API 响应里的 `trace` 字段。它按顺序写了每个能力用了哪个源、谁被跳过。
2. **`doctor`**：`.venv\Scripts\python.exe -m tripagent.cli doctor` —— 一眼看出哪个 provider 不可用、为什么（缺 key / 离线）。
3. **`probe_apis`**：`.venv\Scripts\python.exe -m tripagent.cli` 之外单独跑
   ```
   .venv\Scripts\python.exe scripts\probe_apis.py 成都
   ```
   它绕开引擎直接打天气与预警接口，用来区分"接口挂了"还是"我的逻辑挂了"。

原始响应都缓存在 `.cache/*.json`（文件名是哈希）。**排查解析类 bug 时直接打开对应缓存看真实字段**，比猜接口结构可靠。想强制重新联网取数就 `del .cache\*.json`。

---

## 4. 常见症状 → 定位路径

| 症状 | 最可能的地方 | 怎么确认 |
|---|---|---|
| 中文乱码 / UnicodeEncodeError | 终端编码 | `set PYTHONIOENCODING=utf-8` 后再跑（`.bat` 已内置） |
| 提示"没能识别目的地城市" | `core/intent.py` 切词 或 `providers/geo.py` 地理编码 | 跑 `city_candidates("你的原话")` 看候选词对不对；`doctor` 看 `city/amap` 是否可用 |
| 行程里全是"自由活动" | 候选池为空 | 看 trace 里 `POI xxx：0 条`；`poi_chain` 是否降级到了 fixture |
| 通过率突然掉很多 | 新加的约束或改了权重 | `tests/test_engine.py` 里记分卡断言会指出维度；用 `git diff` 看 `core/validator.py` |
| 联网口碑没有内容 / 等很久 | `providers/evidence.py` 输出格式 | 单独跑 `probe`；`evidence_chain` 改成 `fixture` 立刻变快 |
| 大模型报超时 | `llm_timeout` 太小 | 开 `enable_search` 实测 30-120 秒，默认已给 150 |
| 预算明明超了却显示通过 | 住宿计价口径 | 看 `budget_breakdown` 的「住宿（N 间 × M 晚）」；`Party.room_count` 是 2 人 1 间 |

---

## 5. 改动前必做的两件事

```bat
:: 1) 基线：先确认现在是绿的
.venv\Scripts\python.exe -m pytest -q

:: 2) 改完再跑一遍；任何一条红了就别往下走
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m tripagent.cli demo
```

测试文件在 `tests/`，全离线、不联网、约 1 秒跑完。**加功能就加一条断言**，这是这个项目能长期改下去的唯一保障。改哪个模块对应加哪个文件的用例：

- 解析类 → `tests/test_intent.py`
- 天气/装配/校验 → `tests/test_engine.py`
- 接口解析/降级/缓存 → `tests/test_providers.py`

---

## 6. 发现 bug 怎么说，我才能一次改对

按这个格式给我，基本不会返工：

```
1. 我输入的原话（原样，别改写）
2. 我点了哪个页签 / 跑了哪条命令
3. 实际看到什么（截图或文字都行）
4. 我期望看到什么
5. trace 里那几行（网页版折叠区复制，或命令行的"数据来源"行）
```

第 5 项最关键——有 trace 我能直接判断是数据源问题还是引擎问题；没有的话我得先复现，慢很多。

---

## 7. 想加东西（不是改 bug）

看 [docs/SECOND_DEVELOPMENT.md](../docs/SECOND_DEVELOPMENT.md)，里面 7 个 Recipe 都是"改哪个文件、贴哪段代码、跑哪条验证命令"的照做级别。最常做的三个：

- 加数据源（比如接入大麦演出）→ Recipe 1
- 加一条行程约束 → Recipe 2
- 加一个城市知识库 → Recipe 4
