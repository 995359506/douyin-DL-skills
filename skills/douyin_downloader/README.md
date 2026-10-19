# Douyin Video Downloader Skill

抖音视频下载器 Skill 模块，提供完整的下载功能和 CLI 接口。

## 本地口播文案提取

Web 工作台支持对已完成的单视频任务提取口播文案，也支持在作者画廊中逐条批量提取；已有成功结果与正在运行的作业会跳过。流程为本机 `ffmpeg` 抽取音频、音频模型转写、`gpt-5.6-luna` 整理文案，结果保存在任务数据库中并可导出 TXT/JSON。模型配置使用 `DOUYIN_LLM_BASE_URL`、`DOUYIN_LLM_API_KEY`、`DOUYIN_LLM_TRANSCRIBE_MODEL` 和 `DOUYIN_LLM_POLISH_MODEL`；瞬时网络、限流、上游错误和无效响应每个模型请求最多自动重试 3 次，鉴权或请求格式错误不会重复提交。可通过 `DOUYIN_LLM_REQUEST_RETRIES` 调整额外重试次数，默认为 3；不要把 API Key 写入代码或前端。

视频总结按新网关 `gemini-3.8-flash` → text168 → `gemini-2.5-flash` 保底的顺序切换。新网关使用 `DOUYIN_VIDEO_SUMMARY_GATEWAY_BASE_URL`、`DOUYIN_VIDEO_SUMMARY_GATEWAY_MODEL`，默认地址为已验证支持 Gemini 原生协议的 `https://api.aigateway.online/v1beta`；其 `/v1` 地址是 OpenAI 兼容协议。原主接口使用 `DOUYIN_VIDEO_SUMMARY_BASE_URL`、`DOUYIN_VIDEO_SUMMARY_MODEL`；保底接口使用 `DOUYIN_VIDEO_SUMMARY_FALLBACK_BASE_URL`、`DOUYIN_VIDEO_SUMMARY_FALLBACK_MODEL`。API Key 只放在 macOS Keychain（服务名分别为 `douyin-downloader-summary-aigateway`、`douyin-downloader-text168`、`douyin-downloader-summary-fallback`）中，不要提交到仓库、前端、数据库或日志。
通道选择只在当前作业内保持；服务重启后会从主通道重新尝试。失败作业仅记录通道、模型、HTTP 状态和视频准备体积，不保存请求体。
自动视频总结在每条下载完成后立即提交，服务级最多 5 个并发；下载队列的 30 秒冷却不限制总结，也不等待总结结束。
新生成的视频总结包含「神评论灵感」：根据视频内容提供搞笑有梗、抒情共鸣和金句类原创候选，可逐条复制，并随单视频及作者汇总 Markdown 一同导出。评论在同一次总结请求中生成；已有总结不会自动重做，可点击「重新总结」生成新版内容。

博主归纳超时由 `DOUYIN_DISTILL_SYNTHESIS_TIMEOUT` 控制，默认 300 秒。归纳响应只记录长度、结束原因和用量，不保存模型返回正文。
批量下载连续 403 熔断阈值由 `DOUYIN_DISTILL_DOWNLOAD_CIRCUIT_BREAKER` 控制，默认 5 条；口播最低信息量由 `DOUYIN_DISTILL_MIN_TRANSCRIPT_CHARS` 控制，默认 150 字，设为 0 可关闭。画面总结的中文占比下限由 `DOUYIN_DISTILL_MIN_SUMMARY_CJK_RATIO` 控制，默认 0.15。
博主蒸馏可选择预筛档：先下载并提取口播，再勾选作品继续画面理解与跨条归纳；预筛结果复用同一运行的文件和转写。图文作品只使用扫描时已有正文，不下载图片、不执行 OCR 或视频总结。

## 概述

本模块是抖音视频下载器的核心模块，提供：
- **下载能力**：单视频、合集、用户作品的下载
- **CLI 接口**：完整的命令行工具
- **Web API**：FastAPI 提供的 HTTP 接口
- **Agent Skill 定义**：SKILL.md + skill.yaml（供 AI Agent 发现与调用）
- **数据持久化**：SQLite 存储任务状态

## 快速开始

### 安装依赖

```bash
pip install f2>=0.0.1.8 playwright httpx aiosqlite fastapi uvicorn
playwright install chromium
```

### 配置文件

创建 `my_douyin.yaml`：

```yaml
douyin:
  path: "Download"          # 下载目录
  max_connections: 5        # 最大连接数
  timeout: 10              # 超时时间（秒）
```

### 使用方式

#### 1. CLI 命令行

```bash
# 下载合集
python -m skills.douyin_downloader.cli mix <mix_id> --name "名称" --wait

# 下载单视频
python -m skills.douyin_downloader.cli video "https://v.douyin.com/xxxxx" --wait

# 刷新 Cookie
python -m skills.douyin_downloader.cli refresh-cookie

# 查看任务列表
python -m skills.douyin_downloader.cli list
```

#### 2. Web API 服务

```bash
# 启动服务
python -m skills.douyin_downloader.web --host 127.0.0.1 --port 8080

# 访问 API 文档
open http://127.0.0.1:8080/app/
```

#### 3. Python API

```python
from skills.douyin_downloader.task_manager_v2 import TaskManager

manager = TaskManager()
await manager.initialize()

# 下载合集
task = await manager.create_mix_task(
    mix_id="7303349061694785599",
    mix_name="马斯克传"
)

# 获取任务视频
videos = manager.get_task_videos(task.id)
```

## 项目结构

```
skills/douyin_downloader/
├── cli.py               # CLI 入口
├── core.py              # 原版下载核心（DouyinDownloadSkill）
├── downloader.py        # 新版下载器（数据库集成）
├── task_manager_v3.py   # 任务管理器 V3（完全异步化）
├── db.py                # SQLite 数据库实现
├── rate_limiter.py      # 风控限速器
├── logger.py            # 统一日志系统
├── web.py               # FastAPI Web 服务
├── health.py            # 健康检查
├── SKILL.md             # Agent Skill 定义文档（frontmatter + 使用指令）
└── README.md            # 本文档
```

## 核心组件

### 1. CLI (cli.py)

命令行入口，提供以下命令：
- `mix` - 下载合集
- `video` - 下载单视频
- `refresh-cookie` - 刷新 Cookie
- `status` - 查看任务状态
- `videos` - 获取视频列表
- `export` - 导出元数据
- `retry` - 重试失败任务
- `list` - 列出所有任务

### 2. Core (core.py)

原版下载核心，包含：
- `DouyinDownloadSkill` - 主要下载逻辑
- `download_mix_by_id()` - 合集下载
- `download_single_video()` - 单视频下载
- `refresh_cookie()` - Cookie 刷新

### 3. Task Manager (task_manager_v3.py)

任务管理器 V3（完全异步化），提供：
- 完全异步的任务管理
- SQLite 数据库存储
- 进度追踪和状态管理
- 任务恢复和重试机制
- WebSocket 实时推送支持

### 4. Web API (web.py)

FastAPI 服务，提供：
- RESTful API
- WebSocket 实时推送
- Cookie 管理接口
- 任务管理接口

## 详细文档

- **[SKILL.md](./SKILL.md)** - Agent Skill 定义文档（面向 AI Agent，描述能力与调用方式）
- **[项目根目录 README](../../README.md)** - 项目概览
- **[前端文档](../../frontend/README.md)** - Web 界面使用说明

## 架构说明

本模块采用分层架构：

1. **CLI 层** (`cli.py`): 命令行接口
2. **API 层** (`web.py`): HTTP/WebSocket 接口（完全异步化）
3. **核心层** (`core.py`): 复用原版稳定的下载逻辑
4. **数据层** (`db.py` + `task_manager_v3.py`): SQLite 持久化（完全异步化）
5. **风控层** (`rate_limiter.py`): 下载限速保护
6. **下载层** (`downloader.py`): 集成 F2 下载器的数据库版本

### V3 架构改进

**完全异步化设计**：
- 所有数据库操作使用 `aiosqlite` 异步执行
- WebSocket 连接管理异步化
- 后台下载任务使用 `asyncio.create_task()`
- 任务状态回调机制支持实时推送

**数据库优化**：
- `tasks` 表：任务主表，存储任务元信息
- `task_videos` 表：视频详情表，支持单视频和合集
- `douyin_videos.db`：F2 框架视频去重数据库

## 许可证

MIT License
