# 部署与二次开发手册（照着敲就能跑）

这份文档回答三个问题：**改了代码怎么上线**、**两个部署目标各自怎么更新**、**出问题先看哪里**。
命令都是本机实测过的，路径按 Windows / Git Bash 写。

---

## 0. 当前两个部署目标

| 目标 | 地址 | 运行什么 | 更新方式 |
| --- | --- | --- | --- |
| 魔搭创空间 | https://ginpiga-travel-agent.ms.show （空间页内嵌） | 完整 Streamlit 应用（Python） | `git push modelscope deploy:master` → 在空间页点「重新部署」 |
| Qoder Sites | https://tripagent-travel-mo2hxigbsa3.qoder.zone | 静态页 + Deno 边缘函数（天气速查） | 改 `sites/` 后由 Qoder 重新 prepare + publish |

本地开发入口永远是：

```
D:\APP_develop\qoder-agent-develop\travel-agent> .venv\Scripts\python.exe -m streamlit run app.py
```

浏览器打开 **http**://localhost:8501（不是 https）。

---

## 1. 改完代码怎么上线魔搭（日常流程）

```bash
cd D:/APP_develop/qoder-agent-develop/travel-agent

# ① 先确认没把密钥写进代码
.venv/Scripts/python.exe scripts/check_secrets.py

# ② 跑测试（现在是 56 项，全绿才继续）
.venv/Scripts/python.exe -m pytest -q

# ③ 提交到 main
git add -A
git commit -m "说清楚改了什么、为什么"

# ④ 组装部署分支并推送（凭据从 api_keys/git-credentials 读，不进命令行、不进仓库）
H='store --file=D:/APP_develop/qoder-agent-develop/travel-agent/api_keys/git-credentials'
git -c credential.helper= -c credential.helper="$H" fetch modelscope master
git checkout -B deploy FETCH_HEAD      # 以平台最新 master 为底，避免强推
git checkout main -- .                 # 把我们的改动盖上去
git commit -m "部署：<本次改动摘要>"
git -c credential.helper= -c credential.helper="$H" push modelscope deploy:master
git checkout main                      # 本地回到开发分支
```

**为什么绕一圈 `deploy` 分支**：平台的 `master` 上有它自己的初始提交（`.gitattributes`），
我们的 `main` 是独立历史。直接推会被拒，强推会覆盖平台文件。
所以每次都从远端 master 起底、盖上我们的树、产生**一个干净的增量提交**，
平台看到的永远是快进。

推送后**必须去空间页面点「重新部署 / 启动」**：`DeployMode: prebuild` 模式下推送不会自动构建
（实测：推了三次，`Status` 一直是 `Empty`）。

---

## 2. 改 Qoder Sites 那一侧

站点源码在 `sites/web/index.html`（页面）和 `sites/functions/index.ts`（边缘函数）。
它不需要 git 推送，走 Qoder 的发布流程，直接对我说一句「站点改了 xxx，重新发布」即可；
我自己执行的顺序是：

1. 改文件 → 本地起静态服务预览
2. `prepare_site`（**必须换新 actionId**，同一 action 不会重新打包）
3. `get_publish_status` 看 `canPublish`
4. `publish_site` → 轮询到 `published: true`
5. 用真实 HTTP 请求验证函数返回

注意两点：
- 站点函数是 **Deno/TypeScript**，跑不了 Python。天气规则的**权威实现是 Python**
  （`src/tripagent/core/weather_risk.py`），函数里那份是等价移植。**改了阈值要两边一起改**，
  否则站点和完整应用会给出不同结论——这是本项目最容易踩的一致性坑。
- 城市坐标表由 `scripts/build_site_cities.py` 从 Python 知识库生成 `sites/functions/cities.ts`。
  改了城市数据就重跑它，别手改那个文件。

---

## 3. 什么改动需要重新部署哪个目标

| 改动 | 魔搭 | Qoder Sites |
| --- | --- | --- |
| `src/tripagent/**`、`app.py`、`ui_*.py`、`data/fixtures/**` | 需要 | 不需要 |
| `.streamlit/config.toml` | 需要（构建时读取） | 不需要 |
| `requirements.txt` / `Dockerfile` | 需要（依赖变化会重建镜像） | 不需要 |
| `sites/**` | 不需要 | 需要 |
| `README.md` 顶部的创空间元数据 | 需要（平台按它决定运行方式） | 不需要 |

---

## 4. 密钥与环境变量：两处，别混

**本地开发**：只写 `api_keys/tripagent.env`（双层 `.gitignore` 保护，永不入库）。

**魔搭空间**：只填「密文变量」，**明文变量里一个密钥都不要放**——明文意味着在空间页面公开可见。

```
密文变量：DASHSCOPE_API_KEY  AMAP_KEY  QWEATHER_KEY  QWEATHER_HOST
          TENCENT_MAP_KEY      腾讯位置服务 Web 服务 Key（只做双源校验，可留空）
          TRIPAGENT_ACCESS_CODES   体验口令（发给主办方）
          TRIPAGENT_OWNER_CODE     站长口令（只留给自己，无视日额度）
明文变量（可选，非敏感）：TRIPAGENT_LLM_MODEL=qwen3.6-flash
                          TRIPAGENT_LLM_DAILY_TOKEN_BUDGET=2000000
                          TRIPAGENT_TENCENT_DAILY_CALLS=40
```

程序读的是**环境变量名**，所以两边的名字必须完全一致；改名字等于把密钥弄丢。

### 访问闸门怎么运作（`core/access.py`）

| 来者 | 结果 |
| --- | --- |
| 没口令 / 口令错 | 只看到一个「体验入口」，连表单都看不到 |
| 体验口令正确 | 进入完整界面，可规划、可问答 |
| 日额度已用完 + 体验口令 | 能进页面，但「开始规划」「问一问」变灰并显示说明；**天气速查仍可用**（它不花钱） |
| 站长口令 | 到量后照常可用 |
| 两个口令都没配置 | 闸门完全不启用 —— 本地开发与断网演示零打扰 |

口令生成在 `api_keys/access_codes.txt`（已被 `api_keys/.gitignore` 排除，永不入库、永不推送）。
换口令就重跑一次生成，或直接手写一个新随机串，然后更新空间的密文变量。

---

## 5. 谁在花钱，以及三道闸

访客点一次「开始规划」≈ **1 万 token**（本机实测：3 次模型调用，prompt 8568 + completion 1336，
思考 token 0）。这些全部记在你的百炼账号上。

四道闸，按拦截能力从弱到强：

1. **体验口令**（`TRIPAGENT_ACCESS_CODES`）：没口令的人连表单都看不到，公开链接也不再是"谁都能刷"。
   站长口令（`TRIPAGENT_OWNER_CODE`）单独留给你自己，到量后仍可演示。
2. **会话冷却**（`app.py` 的 `PLAN_COOLDOWN_S = 20`）：同一个浏览器 20 秒内只能规划一次，
   挡住拿到口令的人反复刷。
3. **日 token 闸门**（`providers/usage.py` + `TRIPAGENT_LLM_DAILY_TOKEN_BUDGET`，默认 200 万）：
   按天记 token（含思考 token），超限后访客的规划/问答按钮直接变灰并给出说明，
   不花钱的天气速查继续可用；台账文件 `data/llm_usage.json` 已加入 `.gitignore`。
4. **平台侧额度上限**（只能你去百炼控制台设，代码替代不了）：给这个 Key 设**消费限额与告警**，
   这是唯一能在"程序被绕过"时仍然兜底的硬限制。

腾讯地图不花钱，但**个人开发者档的免费额度是按接口算的、量级只有几十次/天**，
公开空间里每个访客都要 4 次，一上午就光。所以第五道闸在代码里：
`TRIPAGENT_TENCENT_DAILY_CALLS`（默认 40）配合 `data/tencent_budget.json` 按天累计，
预算用完直接跳过双源校验并写进轨迹，不再去撞接口的 `status=121`。
额度恢复后自动放行；没配 Key 时这层完全不参与，本地开发零打扰。

再加一层平台侧的：魔搭空间设置里有「需要登录」开关，打开后未登录的匿名访客会被平台先拦住，
与体验口令叠加就是"魔搭账号 + 口令"双因子。

另外建议给高德 Key 加 IP/域名白名单：它按调用量计费，且白名单能防止 Key 泄漏后被别处盗用。

---

## 6. 出故障时的排查顺序

| 现象 | 先看 | 大概率原因 |
| --- | --- | --- |
| 空间页面空白 / 一直转圈 | 空间页「日志」 | iframe 跨源：`.streamlit/config.toml` 的 `enableCORS`/`enableXsrfProtection` 被改回 true |
| `ModuleNotFoundError: tripagent` | 构建日志 | `app.py` 里的 `sys.path` 引导被删了（平台不会执行 `pip install -e .`） |
| 推送 401 `HTTP Basic: Access denied` | 令牌类型 | 用了 `ms-` 开头的 **SDK 令牌**；git 要的是同页面的 **Git 访问令牌** |
| 凭据明明对却不生效 | `api_keys/git-credentials` | 文件被写成 CRLF，行尾 `\r` 混进主机名导致永不匹配（必须 `\n`） |
| 构建失败 | 平台构建日志 | 依赖装不上 / 镜像里 python 版本与 `pyproject` 的 `requires-python` 不符 |
| 界面显示"今日模型额度已用完" | `data/llm_usage.json` | 闸门生效，属预期；要么等次日，要么调高 `TRIPAGENT_LLM_DAILY_TOKEN_BUDGET` |
| 腾讯双源校验整轮不生效 | `data/tencent_budget.json`、轨迹里的"双源校验失败（腾讯）" | 个人开发者额度是按接口算的：`status=121` = 当天该接口打光；自闸预算用完则显示"腾讯调用预算今日已用完"。两者都不是代码坏了，次日自动恢复 |
| 站点天气结论与完整应用不一致 | 两边规则文件 | 只改了一边的阈值 |

**验证顺序永远是：本地 `pytest` → 本地起服务实测 → 推送 → 平台日志 → 公开地址实测。**
不要跳过"本地实测"直接信构建成功——构建通过只说明镜像装起来了，不说明页面能用。
