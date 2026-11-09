# api_keys/ —— 密钥专用目录

这个目录**专门存放需要调用外部 API 的密钥**，整个目录被 git 忽略，不会进入 GitHub 仓库。

## 目录约定

| 文件 | 是否提交 | 用途 |
|---|---|---|
| `tripagent.env` | ❌ 不提交 | 你本机真实的 Key，程序优先读这里 |
| `README.md` | ✅ 提交 | 本说明 |
| `.gitignore` | ✅ 提交 | 本目录的自我保护层：`*` 全忽略，只放行这两个文件 |

根目录的 `.env.example` 是**模板**（提交，值全部留空），别人 clone 后照着建自己的 `tripagent.env` 即可。

## 为什么要两层 .gitignore

根目录 `.gitignore` 负责常规忽略；本目录内的 `.gitignore` 是第二道保险——
即使根目录那份被误删或改写，`api_keys/*` 仍然不会被 `git add .` 收进去。

## 加载优先级（后者覆盖前者）

```
代码默认值  <  根目录 .env  <  api_keys/tripagent.env  <  系统环境变量
```

所以推荐只维护 `api_keys/tripagent.env` 一处。改完直接重新运行程序即可生效，无需改代码。

## 需要填哪些 Key

```ini
DASHSCOPE_API_KEY=sk-...      # 阿里百炼 https://bailian.console.aliyun.com/ → API-KEY 管理
AMAP_KEY=...                  # 高德 https://console.amap.com/dev/key/app → 服务平台必须选「Web 服务」
AMAP_SECRET=                  # 只有开启了「数字签名校验」才需要填，否则留空
QWEATHER_KEY=                 # 可选，留空自动用免密钥的 Open-Meteo
QWEATHER_HOST=                # 可选，和风每个账号有独立 API Host
```

## 泄露了怎么办

1. 立刻去对应控制台**吊销并重建** Key（百炼：API-KEY 管理 → 删除；高德：应用列表 → 重置安全密钥）；
2. 更新本文件；
3. 如果你曾经把 Key 明文提交过 git，仅删除文件不够——历史里还在，需要
   `git filter-repo` 清理或直接重建仓库。**最稳妥的做法是现在就别提交。**

## 上传 GitHub 前必做

```bat
.venv\Scripts\python.exe scripts\check_secrets.py
```

扫描所有将被提交的文件，发现 Key 形态的字符串就报错退出（非 0）。
把它当成推送前的固定动作，比"我记得没提交"可靠。
