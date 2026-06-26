# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

LivePool 是一个 IPTV 直播源采集、校验、去重、分类和 m3u8 生成系统。Python 3.13+，FastAPI Web 管理面板 + APScheduler 定时任务。

## 常用命令

```bash
pip install -r requirements.txt

# 一次运行完整流水线
python3 src/main.py run

# 启动定时任务守护进程（启动后立即执行一次，之后按 config.yaml scheduler.cron 周期运行）
python3 src/main.py schedule

# 查看系统 crontab 表达式
python3 src/main.py cron

# 启动 Web 管理面板（默认 0.0.0.0:8008）
python3 src/main.py web

# Docker 部署
docker build -t livepool:latest .                        # 本地构建镜像
docker save livepool:latest -o livepool-image.tar        # 导出镜像
# 传输到服务器后：
docker load -i livepool-image.tar
docker run -d --name livepool --restart unless-stopped \
  -p 8008:8008 -v /path/to/config.yaml:/app/config.yaml:ro \
  -v /path/to/data:/app/data -e TZ=Asia/Shanghai \
  -e JWT_SECRET=xxx livepool:latest

# 一键构建部署脚本
./build.sh <服务器IP> <SSH端口>
```

## 架构核心

### 六阶段流水线

`scheduler.run_pipeline()` 串联完整 ETL：

| 阶段 | 模块 | 职责 |
|------|------|------|
| Collect | `collector.py` | 运行所有启用的爬虫 + 自动发现 `data/sources/` 下的种子文件，按 URL 去重 |
| Validate | `validator.py` | 异步并发 HTTP 探测（含三次 HEAD 取中位数延迟、32KB NAL 扫描） |
| Filter | `filter.py` | 分离存活/死亡流，URL 去重，频道名去重（加权评分制） |
| Classify | `classifier.py` | EXTINF tag → channels.json → 关键词 → 省份 → 默认 |
| Generate | `generator.py` | 合并 m3u8 + 缓存 Logo，持久化到 SQLite |
| EPG | `epg.py` | XMLTV 节目数据缓存与代理 |
| Stats | `scheduler.py` | 统计写入 SQLite stats_history |

**关键设计决策**: `save_state(alive_raw + dead_raw)` 在去重 **之前** 调用，因此 DB 包含所有频道的全量状态（含已死亡）。去重仅在生成 m3u8 的存活流上执行。`save_state` 使用 `INSERT OR REPLACE`（upsert）而非全量覆盖，并跳过任何被用户收藏的频道 ID。

### 校验器四阶段探测

`validator.py:_check_one()` 按以下顺序逐级检查：

1. **三次 HEAD 探测**（取延迟中位数，减少网络波动影响）
2. **GET + HTTP 状态码**（200-399 以外直接判死）
3. **内容验证** — 最小字节数 / 非 HTML / m3u8 签名 (`#EXTM3U` 或 `#EXTINF`)
4. **Segment HEAD** — 解析 m3u8 找到首个媒体片段的 URL，检查是否可达
5. **NAL 单元扫描** — 下载片段前 32KB，扫描 H.264/H.265 NAL 起始码。无视频 track 判为 `AUDIO`

**注意**: 死链退避机制（`_should_skip`/`_SKIP_THRESHOLD`/`last_check.json`）已在重构中删除。每次管道运行检查全部 URL，确保统计口径一致。

### 数据模型（`src/__init__.py`）

- `StreamEntry` — 采集阶段原始条目（name, url, source, group, tvg_id 等）
- `CheckResult` — 校验结果（status, http_code, latency_ms, has_video, has_cors 等）
- `ChannelRecord` — 持久化频道记录（含 score, last_check, last_alive）
- `Stats` — 流水线统计快照
- `StreamStatus` 枚举：`pending | alive | dead | timeout | error | audio`
- `SourceType` 枚举：`github_m3u | raw_m3u | web_scrape | local_file | manual`

### 存储层（`src/store.py`）

- **SQLite + WAL 模式**，通过 `aiosqlite` 异步访问，单连接单次事务模式
- 首次启动自动从旧 JSON 文件迁移数据（原始文件重命名为 `.bak`）
- **_SyncStore 包装器**: `DbStore` 是全异步的，但部分旧代码需要同步调用。`_SyncStore` 使用 `concurrent.futures.ThreadPoolExecutor` + `asyncio.run()` 桥接
- **`save_state` 收藏保护**: 不再使用 `replace_all`。改用逐条 `INSERT OR REPLACE`，清理孤立频道时跳过已被用户收藏的 ID，避免收藏频道因源文件变化而丢失
- 表：`users`（含 2FA/TOTP + 拉取统计列）、`sources`、`channels`（含 `score` 质量评分列）、`invite_codes`、`local_seeds`、`stats_history`
- `users` 表含按需迁移（`ALTER TABLE ADD COLUMN` 包在 try/except 中向前兼容）

### 爬虫插件（`src/sources/`）

- `BaseCrawler` ABC 定义接口，新爬虫继承后注册到 `collector.py` 的 `CRAWLER_REGISTRY` 字典
- 现有爬虫：`GitHubM3UCrawler`（= `M3UCrawler`，SourceType.GITHUB_M3U）、`RawM3UCrawler`（= `M3UCrawler` 别名）
- 爬虫可在 Web UI 采集源管理页中启用/禁用。禁用的爬虫不会被执行

### 本地种子自动发现

采集器自动扫描 `data/sources/` 目录下所有 `.m3u`、`.m3u8`、`.txt` 文件：
- 无需手动配置 `local_seeds`（config.yaml 中设为空数组即可启用自动发现）
- 每个文件的启用/禁用状态可通过 Web UI 采集源管理页中的"本地种子文件"表格控制
- 状态存储在 SQLite `local_seeds` 表中

### 频道去重算法（`filter.py:dedup_by_name`）

按优先级分组：先按 `tvg_id` 分组，剩余按归一化名称分组。每组内以加权评分选择最优流：

| 维度 | 权重 | 说明 |
|------|------|------|
| 稳定性 | ×50 | 来源整体存活率 |
| 可播放性 | ×8 | has_video |
| 质量 | ~0-4 | resolution (像素数 / 500000) |
| 延迟 | 0~100 | (3000 − min(ms, 3000)) / 30 |

### 分类器优先级（`classifier.py`）

1. EXTINF 已有 tag（非空 `group` 字段）
2. `channels.json` 手动映射（精确匹配 → 最长子串匹配）
3. 内置关键词（CCTV→央视频道, 卫视→卫视频道 等）
4. 省份/地区名匹配 → 地方频道
5. 默认组（`default_group: 其他`）

### 生成器（`generator.py`）

- 输出 `data/live.m3u8`（原子写入：临时文件 + rename，防止读取半截文件）
- **Logo 缓存**: 下载远程 logo 到 `data/logos/`（MD5 文件名），通过 `/api/logo/{token}/{filename}` 反防盗链提供
- EXTINF 模板转义：`str.replace("{", "{{")` 防止 `.format()` 崩溃
- `save_state()` → `load_state()`: 通过 SQLite upsert 持久化/恢复 `ChannelRecord[]`

### Web 层（`src/api.py + src/auth.py`）

- **FastAPI + Jinja2** 服务端渲染，无前端框架
- **JWT 认证**: pyjwt (python-jose) HS256，HttpOnly cookie（名称 `livepool_session`） + Bearer header。secret 优先 $JWT_SECRET 环境变量
- **2FA/TOTP**: pyotp + QR 码（qrcode SVG），支持临时 `pre_auth` token（5 分钟有效期）、备用恢复码（bcrypt 哈希存储）
- **AuthRedirectMiddleware**: API 请求 401 直接返回；浏览器请求 302 → `/login`
- **订阅端点**: `/tv/{token}.m3u8`、`/tv/{token}/favorites.m3u8`、`/tv/{token}/epg.xml`。支持 `?cors=1` 过滤非 CORS 流
- **EPG 地址**: 仪表盘在配置了 EPG 源后自动显示带 Token 的 EPG 订阅地址
- **频道列表 API**: 60 秒内存缓存（`_channels_cache`、`_cache_time`），无手动失效机制
- **频道质量评分**: `score` 字段存储在 SQLite 中，频道列表页面展示质量等级（优秀/良好/一般/较差）
- **本地种子管理**: `api/local-seeds` 端点展示 `data/sources/` 中的文件、频道数、启用/禁用状态
- **收藏**: 每个用户 `favorites` 字段存放 channel_id 数组，支持 toggle
- **CORS**: 通配符 `*` + `allow_credentials=True` 是规范违规（浏览器忽略），仅适用于同源 dashboard。跨域 client 请在 `config.yaml` 设置 `web.cors_origins`。
- **任务追踪**: 内存 dict `_tasks`，运行时通过 `api/tasks/{id}` 轮询进度。服务重启后丢失。

### 配置（`config.yaml`）

`config.py` 提供类型化访问器（`get_validator_config()`, `get_web_config()` 等），首次加载后全局缓存。运行时修改配置需调用 `reload_config()`。所有路径解析相对于项目根目录。

大部分配置项可通过 Web UI 修改（采集源管理页的调度配置和 EPG 配置面板），无需编辑 config.yaml。

## 关键约定

- `src/` 是 Python 包，所有内部导入使用 **相对导入**（`from . import ...`）
- 流水线全异步，validator 使用 `asyncio.Semaphore` 控制并发数
- 所有持久化数据统一存放在 `data/` 目录下（子目录：`data/logos/`、`data/logs/`、`data/sources/`）
- 无测试框架、无 lint 配置、无 CI pipeline
- 死链退避已删除，每次全量检测检查所有 URL

## Docker 架构

- **单容器部署**: Dockerfile CMD `python3 src/main.py web`（默认），端口 8008，`/api/health` 健康检查
- **调度器内嵌**: APScheduler 在 Web 进程启动时自动运行（`api.py:startup`），无需独立容器
- **构建流程**: 本地构建镜像 → `build.sh` 自动传输到服务器 → 服务器仅 `docker load` + `docker run`
- 持久化卷：`config.yaml:ro`（配置）、`data/`（全量数据：数据库、m3u8、日志、图标缓存、种子文件）

### 数据目录结构

```
data/
├── livepool.db          # SQLite 数据库
├── live.m3u8            # 主播放列表
├── logos/               # 频道图标缓存
├── sources/             # 本地种子文件（自动发现）
├── app.log              # 运行日志
├── epg_cache.xml        # EPG 节目数据缓存
└── local_seeds_state.json  # 种子启停状态（已废弃，改用 SQLite local_seeds 表）
```

## 未使用的依赖（维护债务）

`requirements.txt` 中的以下依赖在源码中未被导入：
- `beautifulsoup4`、`lxml` — 可能留给未来 HTML 爬虫
- `m3u8` — Python m3u8 解析库，当前使用手写解析器 `parser.py`
- `httpx` — 当前使用 aiohttp 做 HTTP 客户端
