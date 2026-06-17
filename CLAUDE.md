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
docker compose up -d                     # 仅 Web 面板
docker compose --profile scheduler up -d  # Web + 定时任务
```

## 架构核心

### 六阶段流水线

`scheduler.run_pipeline()` 串联完整 ETL：

| 阶段 | 模块 | 职责 |
|------|------|------|
| Collect | `collector.py` | 运行所有启用的爬虫 + 导入本地种子文件，按 URL 去重 |
| Validate | `validator.py` | 异步并发 HTTP 探测 |
| Filter | `filter.py` | 分离存活/死亡流，URL 去重，频道名去重（加权评分制） |
| Classify | `classifier.py` | EXTINF tag → channels.json → 关键词 → 省份 → 默认 |
| Generate | `generator.py` | 合并 m3u8 + 按分组 m3u8，持久化到 SQLite |
| Stats | `scheduler.py` | 统计写入 `data/stats_snapshot.json` + SQLite stats_history |

**关键设计决策**: `save_state(alive_raw + dead_raw)` 在去重 **之前** 调用，因此 DB 包含所有频道的全量状态（含已死亡）。去重仅在生成 m3u8 的存活流上执行。

### 校验器四阶段探测

`validator.py:_check_one()` 按以下顺序逐级检查：

1. **GET + HTTP 状态码**（200-399 以外直接判死）
2. **内容验证** — 最小字节数 / 非 HTML / m3u8 签名 (`#EXTM3U` 或 `#EXTINF`)
3. **Segment HEAD** — 解析 m3u8 找到首个媒体片段的 URL，检查是否可达
4. **NAL 单元扫描** — 下载片段前 8KB，扫描 H.264/H.265 NAL 起始码。无视频 track 判为 `AUDIO`

**死链退避** (`_should_skip`, `_SKIP_THRESHOLD=3`): 连续失败 3 次的 URL 跳过本次检测，退避时间 = 2^(fail_count - 2) 小时（上限 7 天）。结果标记 `[SKIPPED]` 而非重测。

### 数据模型（`src/__init__.py`）

- `StreamEntry` — 采集阶段原始条目（name, url, source, group, tvg_id 等）
- `CheckResult` — 校验结果（status, http_code, latency_ms, has_video, has_cors 等）
- `ChannelRecord` — 持久化频道记录（含 fail_count, last_check, last_alive）
- `Stats` — 流水线统计快照
- `StreamStatus` 枚举：`pending | alive | dead | timeout | error | audio`
- `SourceType` 枚举：`github_m3u | raw_m3u | web_scrape | local_file | manual`

### 存储层（`src/store.py`）

- **SQLite + WAL 模式**，通过 `aiosqlite` 异步访问，单连接单次事务模式
- 首次启动自动从旧 JSON 文件迁移数据（原始文件重命名为 `.bak`）
- **_SyncStore 包装器**: `DbStore` 是全异步的，但部分旧代码（auth、部分 pipeline 代码）需要同步调用。`_SyncStore` 使用 `concurrent.futures.ThreadPoolExecutor` + `asyncio.run()` 桥接。若运行在已有事件循环的线程（Web），则在新线程中执行
- **`replace_all` 性能优化**: 将 `DELETE + bulk INSERT` 放在同一连接、同一事务中完成，将 ~2N+1 次连接降至 1 次
- 表：`users`（含 2FA/TOTP 列）、`sources`、`channels`（含 `group` 列）、`stats_history`
- `users` 表含按需迁移（`ALTER TABLE ADD COLUMN` 包在 try/except 中向前兼容）

### 爬虫插件（`src/sources/`）

- `BaseCrawler` ABC 定义接口，新爬虫继承后注册到 `collector.py` 的 `CRAWLER_REGISTRY` 字典
- 现有爬虫：`GitHubM3UCrawler`（= `M3UCrawler`，SourceType.GITHUB_M3U）、`RawM3UCrawler`（= `M3UCrawler` 别名）
- `M3UCrawler.fetch()` 对每个 URL 发起 HTTP GET → `parser.parse_m3u8_content()` → `StreamEntry[]`

### 频道去重算法（`filter.py:dedup_by_name`）

按优先级分组：先按 `tvg_id` 分组，剩余按归一化名称分组。每组内以加权评分选择最优流：

| 维度 | 权重 | 说明 |
|------|------|------|
| 稳定性 | ×50 | 来源整体存活率 |
| 可播放性 | ×8 + ×8 | has_cors + has_video |
| 质量 | ~0-4 | resolution (像素数 / 500000) |
| 延迟 | -20~20 | (2000 - min(ms, 2000)) / 100 |

### 分类器优先级（`classifier.py`）

1. EXTINF 已有 tag（非空 `group` 字段）
2. `channels.json` 手动映射（精确匹配 → 最长子串匹配）
3. 内置关键词（CCTV→央视频道, 卫视→卫视频道 等）
4. 省份/地区名匹配 → 地方频道
5. 默认组（`default_group: 其他`）

### 生成器（`generator.py`）

- 输出合并 `live.m3u8` + 按分组 `output/by_group/<组名>.m3u8`
- **Logo 缓存**: 下载远程 logo 到 `output/logos/`（MD5 文件名），通过 `/api/logo/{token}/{filename}` 反防盗链提供
- EXTINF 模板转义：`str.replace("{", "{{")` 防止 `.format()` 崩溃
- `save_state()` → `load_state()`: 通过 SQLite `replace_all` 持久化/恢复 `ChannelRecord[]`

### Web 层（`src/api.py + src/auth.py`）

- **FastAPI + Jinja2** 服务端渲染，无前端框架
- **JWT 认证**: pyjwt (python-jose) HS256，HttpOnly cookie（名称 `livepool_session`） + Bearer header。secret 优先 $JWT_SECRET 环境变量
- **2FA/TOTP**: pyotp + QR 码（qrcode SVG），支持临时 `pre_auth` token（5 分钟有效期）、备用恢复码（bcrypt 哈希存储）
- **AuthRedirectMiddleware**: API 请求 401 直接返回；浏览器请求 302 → `/login`
- **订阅端点**: `/api/subscribe/{token}`（完整列表）、`/api/subscribe/{token}/favorites`（收藏）、`/tv/{token}`（短 URL，兼容 IPTV 播放器）。支持 `?https=1`（仅 HTTPS）、`?all=1`（不过滤 CORS）、按组过滤
- **频道列表 API**: 60 秒内存缓存（`_channels_cache`、`_cache_time`），无手动失效机制
- **收藏**: 每个用户 `favorites` 字段存放 channel_id 数组，支持 toggle
- **CORS**: 通配符 `*` + `allow_credentials=True` 是规范违规（浏览器忽略），仅适用于同源 dashoard。跨域 client 请在 `config.yaml` 设置 `web.cors_origins`。
- **任务追踪**: 内存 dict `_tasks`，运行时通过 `api/tasks/{id}` 轮询进度。服务重启后丢失。

### 配置（`config.yaml`）

`config.py` 提供类型化访问器（`get_validator_config()`, `get_web_config()` 等），首次加载后全局缓存。运行时修改配置需调用 `reload_config()`。所有路径解析相对于项目根目录。

## 关键约定

- `src/` 是 Python 包，所有内部导入使用 **相对导入**（`from . import ...`）
- 流水线全异步，validator 使用 `asyncio.Semaphore` 控制并发数
- 数据目录：`data/`（持久化）、`output/`（生成的 m3u8 和 logo 缓存）、`sources/`（本地种子文件）、`logs/`（运行日志）
- 无测试框架、无 lint 配置、无 CI pipeline
- **Dead-URL 退避**: 连续 3 次失败后跳过该 URL，退避时间指数增长。存储文件 `data/last_check.json`

## Docker 架构

- **Web 服务**: Dockerfile CMD `python3 src/main.py web`（默认），端口 8008，`/api/stats` 健康检查
- **定时任务**: 独立容器，`command: ["python3", "src/main.py", "schedule"]`，通过 `--profile scheduler` 启用
- 持久化卷：config.yaml、sources、output、data、logs（两容器共享）
- 隐式并发陷阱：两容器都使用 `livepool.db`，调度器运行时 Web 容器的 DB 访问会与调度器的 `replace_all` 冲突。当前无锁机制

## 未使用的依赖（维护债务）

`requirements.txt` 中的以下依赖在源码中未被导入：
- `beautifulsoup4`、`lxml` — 可能留给未来 HTML 爬虫
- `m3u8` — Python m3u8 解析库，当前使用手写解析器 `parser.py`
- `httpx` — 当前使用 aiohttp 做 HTTP 客户端
