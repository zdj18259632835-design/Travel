# Qoder Sites 部署路线（P0，11/30 截止）

大赛硬性要求：**作品必须由 Qoder 开发，并部署到 Qoder Sites 开放公开访问**，11/30 18:00 前在天池赛题页论坛发作品帖。

## 1. 架构（已选定，别再改）

```
浏览器
  └─ Qoder Sites 静态前端        sites/web/index.html      ← 公开访问的入口
       └─ Qoder Sites 函数       sites/functions/index.ts  ← TS 运行时，注入 Secret、收敛跨域
            └─ Python 引擎 HTTP 服务  src/tripagent/server.py（外部托管）
                 └─ Open-Meteo / CMA / 高德 / 通义千问
```

**为什么 Python 不直接放 Sites 上**：Sites 的服务端函数是 TypeScript 运行时（`functions/index.ts`），不能托管 Python 进程。智能体的装配与校验逻辑（`core/planner.py`、`core/validator.py`，约 1200 行）只维护一份，放在 Python 侧；Sites 只做入口与密钥隔离。

## 2. 三步走

### 第 1 步：把 Python 引擎挂到公网（先做这个，它是前置）

本机 `tripagent.cli serve --port 8000` 已经验证可用，但那是 localhost。要公开访问，选一个：

| 方案 | 成本 | 适配度 | 注意 |
|---|---|---|---|
| **魔搭 ModelScope 创空间**（推荐） | 免费 | 高：直接跑 Python + FastAPI，国内可访问 | 需要把项目做成可运行的 Space（Docker 或 requirements + 启动命令）；冷启动慢，第一次请求可能 20-40 秒 |
| 阿里云函数计算 FC Web 函数 | 有免费额度 | 高：原生支持 Python 自定义运行时 | 要设**函数超时 ≥120 秒**（联网口碑归纳很慢），否则 502 |
| HuggingFace Spaces | 免费 | 中 | 大陆访问不稳定，评委可能打不开 → **不推荐** |

**必须做的两件安全事**（这个服务会把你的 Key 花出去）：

```bash
# 1) 给 API 加鉴权：Python 侧设环境变量，函数层会自动带上同名 Header
TRIPAGENT_API_TOKEN=<随机长串>

# 2) 前端页面里绝对不出现任何 Key；Key 只留在 api_keys/tripagent.env（不提交）
```

设了 `TRIPAGENT_API_TOKEN` 之后，`/api/health` 会返回 `auth_required: true`，其余接口不带 `X-API-TOKEN` 一律 401。

### 第 2 步：在 Qoder 里创建站点并接上函数

在 Qoder 里说「**用 Qoder Sites 部署 sites/web 目录，函数目录是 sites/functions**」，会走 `prepare_site` → 授权 → `publish_site` 的流程。关键参数：

```
webDirectory      = sites/web
functionDirectory = sites/functions      # 里面是 index.ts
secretNames       = ["TRIPAGENT_API_BASE", "TRIPAGENT_API_TOKEN"]
spa               = false                 # 单页静态站，不需要 SPA 回退
databaseAccess    = none                  # 本期不用数据库；要做历史记录再开
```

发布后拿到 `https://<子域名>.qoder.website` 公开地址。**Secret 值在站点后端配置里填，不写进代码。**

### 第 3 步：验收清单

- [ ] 手机浏览器打开公开地址，能出行程（说明公网链路通）
- [ ] 站点页面里 `F12 → Network` 看 `/api/plan` 的请求头**不含**任何 Key 明文
- [ ] 后端挂了时前端显示「后端不可达」而不是白屏（函数层已实现这个降级）
- [ ] 关掉「允许联网口碑归纳」勾选时响应 < 30 秒；打开时 < 120 秒（否则调函数超时）
- [ ] 把公开地址 + 3 张截图放进作品帖

## 3. 已知取舍（写进作品帖，别说成完美）

1. **冷城市慢**：本地知识库只覆盖 6 城；其它城市要现查高德 POI + 地理编码，首次 60-170 秒。缓解：函数层默认关联网归纳；同一城市第二次走 `.cache` 与动态城市表，明显变快。
2. **联网口碑是模型归纳，不是原始笔记**：输出显式标 `source=qwen_web_search` 与置信度，URL 只在确有出处时填。这是为了合规，不是偷懒。
3. **住宿/车票价格**：无个人可用的开放接口，价格字段经常缺失；程序的做法是**如实标注"价格待确认"**并在预算校验里说明未覆盖住宿，而不是编一个数。

## 4. 下一步动作（按顺序）

1. 你选后端托管方案（我建议 ModelScope 创空间），我据此写对应的启动文件与依赖清单；
2. 我加鉴权与限流（`TRIPAGENT_API_TOKEN` 已在 server 里预留，还没接站点侧配置）；
3. 跑 `prepare_site` 出草稿 → 你在浏览器确认公开访问正常 → 说「发布」才真正上线；
4. 上线后我按 [COMPETITION.md](COMPETITION.md) 的骨架生成作品帖正文与截图清单。
