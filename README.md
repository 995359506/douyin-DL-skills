# 抖音视频下载器

## 油猴：刷到视频后一键下载并总结

启动本地工作台后，在已安装 Tampermonkey（篡改猴）的浏览器中打开
[安装油猴脚本](http://127.0.0.1:8080/app/userscripts/douyin-download-summary.user.js)，按提示安装；
如显示源码，可在油猴里新建脚本，将 `frontend/public/userscripts/douyin-download-summary.user.js` 全部复制进去保存。

刷新抖音网页，右下角会出现“下载并总结”。支持推荐流、视频详情页，以及带 `modal_id` 的搜索/主页视频弹窗；
按钮下方显示当前视频 ID，切换视频会自动更新。点击时只向本机 `127.0.0.1:8080` 发送视频链接，
不读取抖音 Cookie、不发送模型密钥，不会在未点击收集按钮时自动采集整页作品。

项目使用现有 `POST /api/download` 接口并设置 `auto_summary: true`：下载后自动总结，
已下载的视频复用文件，已有总结复用结果。任务提交后可关闭抖音网页，进度和总结在“打开工作台”查看；
本机服务、网络和电脑需持续运行，总结会调用工作台已配置的模型。连接失败时确认服务已启动，
并在油猴提示中允许脚本访问 `127.0.0.1`。无法确定当前视频时按钮禁用，可打开单独的视频详情页操作。

### 油猴主页批量下载

更新脚本到 1.2.3 后，点击右下角小圆按钮展开面板（可点收起或按 Esc 收回）。进入博主主页的“作品”标签，清空作品搜索和日期筛选，点击“收集主页全部视频”。
脚本每 3 秒滚动一次，只读取该主页作品列表中的视频链接，跨滚动去重，跳过图文，不采集喜欢、推荐和合集。
到达页尾或收集数达到主页作品数后提示完成；页面报错、停滞 24 秒或收集超过 30 分钟时提示未确认全部，
可核对已收集数量后仅提交这些视频，不会将未加载的作品算作成功收集。

点击“下载并总结这 N 条”并确认后，所有链接通过 `POST /api/download-batches` **一次提交到本地后台**并保存队列。
首条即可开始下载；下载保持串行，每条实际下载结束后默认等待 **30 秒**，才请求下一条，具体间隔以看板显示为准，不会在接收队列时同时请求所有抖音链接。已经开始的冷却不会因更新缩短。
按视频编号核对本地文件，单视频、旧批量下载和已落盘但记录失败的作品均可自动跳过；空文件、临时文件和目录外文件不算已下载。冷却期间会提前检查本地文件并处理跳过项，不请求抖音。跳过项显示“已下载跳过”，不新增等待，也不会清除前一次实际下载留下的冷却。已有成功总结或正在处理的总结会复用，不会重新生成。
每条下载成功后立即提交独立总结，最多 5 个同时处理；30 秒间隔只用于下载，不给总结加延迟，也不等待整批下载完成。总结不阻塞下一条下载，不合并为一个视频任务。
面板显示后台计数和倒计时，暂停/继续和停止均由后台执行；下载异常会暂停，继续时重试失败项。
停止只停止后续视频，当前在途下载和已创建的总结继续运行。

**收到批次编号后即可关闭抖音网页，全部下载和总结继续由本地后台执行；本地服务、网络和电脑需保持运行。**
服务重启会从已保存队列恢复；同一浏览器标签页刷新只查询已有批次状态，不会再次提交队列。
若提交响应超时，点击“确认上次提交”会沿用原请求编号，后台不会重复创建批次。
旧版 1.1.0 尚未提交的浏览器队列不会自动迁移，请核对看板后重新收集。下载间隔只能降低请求频率，
不能保证避免平台风控；遇到验证码或限制应停止并处理。

## iPhone：分享链接到电脑看板

手机访问服务器上的 `/douyin-inbox/`，填写收件口令并粘贴单条抖音视频的分享内容。
也可以安装私人快捷指令：在抖音中复制链接后运行“发送到抖音看板”，或从系统分享菜单运行。
快捷指令显示“服务器已收下”只表示链接已保存，实际下载和总结状态可在手机收件页刷新查看。

服务器只保存规范化视频链接、回执和固定状态，不接收抖音 Cookie、模型密钥、视频或总结正文。
电脑每 10 秒主动取件，先在本机落盘再确认；提交与入队均按请求编号去重。
下载复用现有后台队列，已下载自动跳过，实际下载结束后间隔 30 秒；每条下载完成后独立总结，已有结果复用。
**关闭手机页面不会停止任务。电脑断网、关机或休眠期间，服务器暂存链接，电脑恢复联网后继续处理。**

服务端使用 Python 标准库，可直接运行 `skills/douyin_downloader/mobile_inbox.py`，
参数为 `--db <持久化数据库> --page <mobile-inbox.html> --port 8025`。
只绑定 `127.0.0.1`，由已有 Nginx 将 `/douyin-inbox/` 转发至回环端口；可使用 HTTP + IP。
收件页面来自 `frontend/public/mobile-inbox.html`，构建后复制 `frontend/dist/mobile-inbox.html`。
代理须保留路径尾部斜杠，设置请求大小上限 16 KB、访问频率限制，并关闭该路径的访问日志。

服务器设置两个不同的随机口令（至少 20 字符）：`DOUYIN_INBOX_SUBMIT_TOKEN` 供手机提交，
`DOUYIN_INBOX_WORKER_TOKEN` 只供电脑取件；环境文件权限 `0600`，数据库目录权限 `0700`。
电脑独立运行 `.venv/bin/python -m skills.douyin_downloader.mobile_inbox_worker`，设置：

| 环境变量 | 用途 |
| --- | --- |
| `DOUYIN_INBOX_URL` | 含 `/douyin-inbox/` 的服务器地址 |
| `DOUYIN_INBOX_WORKER_TOKEN` | 取件口令，与手机收件口令不同 |
| `DOUYIN_INBOX_MAIN_DB` | 现有看板数据库的绝对路径，仅查询状态 |
| `DOUYIN_INBOX_STATE_DIR` | 本地回执目录，默认在用户 Library/Application Support 下 |

可用单独的 LaunchAgent 保持取件进程运行，无需重启看板或打断现有总结。
重启时本地回执和队列继续复用；先保留两个数据库，再重启对应进程。不要删除回执数据库来重试。
登录或配置未就绪时链接会保留，手机显示处理提示，修复电脑配置后自动重试。

`scripts/build_mobile_shortcut.py --server <收件地址> --output <私人目录/发送到抖音看板.shortcut>`
从 `DOUYIN_INBOX_SUBMIT_TOKEN` 读取口令生成快捷指令，再通过 macOS `shortcuts sign` 签名。
签名产物含手机收件口令，只放个人 iCloud 云盘或其他私人目录，不提交仓库，也不公开提供下载。
iPhone 首次安装、首次网络授权和抖音分享菜单显示情况需要在真机确认；也可直接使用手机网页提交。
若旧版提示“找不到操作”或很快结束且没有提示，请重新导入最新版并替换旧指令；
更新 iCloud 文件不会自动替换手机中已经安装的快捷指令。新版使用动作输出直接传递分享内容和提交编号，
避免命名变量的输入包装不兼容导致空请求；服务器反馈会直接显示，空剪贴板会提示先复制链接。
手机页面仅显示提交和处理状态，视频播放与逐条总结仍在电脑看板中查看。

## 口播文案提取

任务工作台中的已完成单视频可使用“提取口播”生成原始转写和整理后的口播文案；作者画廊也支持逐条批量提取，已有口播和正在处理的视频会跳过。处理只读取下载目录中的本地视频，不会重新下载媒体。语音转写与文案整理阶段遇到网络中断、限流、上游错误或无效响应时，每个模型请求最多自动重试 3 次；鉴权和请求格式错误不会盲目重试。

后端通过环境变量读取 OpenAI 兼容模型服务配置：

```bash
DOUYIN_LLM_BASE_URL=https://your-llm-host/v1
DOUYIN_LLM_TRANSCRIBE_MODEL=qwen-audio-3.0-asr-flash
DOUYIN_LLM_POLISH_MODEL=gpt-5.6-luna
DOUYIN_LLM_REQUEST_RETRIES=3
DOUYIN_LLM_API_KEY=your-key
DOUYIN_DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
DOUYIN_DASHSCOPE_API_KEY=your-dashscope-key
```

生产环境建议使用 HTTPS。API Key 也可以保存到 macOS Keychain 的 `douyin-downloader-llm` 项中，服务会自动读取；密钥不会进入前端、任务数据库或日志。音频抽取依赖本机 `ffmpeg`。

基于 F2 框架的抖音视频下载工具。支持单视频、合集、用户主页作品的批量下载，内置任务队列、Cookie 管理、断点续传与风控保护。**既可以作为 Agent Skill 安装给 AI Agent 使用，也提供 Web UI / CLI / Python API / Web API 供人工或程序化使用。**

## 视频总结

任务工作台中的已完成单视频可使用“视频总结”。小视频直接发送完整视频；超过安全请求大小时，服务会在本机用 `ffmpeg` 生成临时压缩副本，仍发送完整画面和音频，原始下载文件不会修改。压缩后仍无法达到大小目标时，会按连续时间片分析并自动合并结果。临时文件在任务结束、失败或取消后清理。

视频总结依次使用 aigateway、text168、原 2.5 Flash 备用接口，最后才尝试 heju 的 3.8 Flash；未配置密钥的通道不启用。aigateway Key 存在 macOS Keychain 的 `douyin-downloader-summary-aigateway`，heju Key 单独存为 `douyin-downloader-summary-heju`，不写入配置文件。

单视频下载确认时默认勾选“下载完成后自动生成视频总结”；下载成功后后台创建总结任务，已有结果会复用，总结失败不改变下载完成状态。工作台原有合集和主页扫描下载不会自动触发逐条模型调用；油猴“后台下载队列”经确认后会为每条下载成功的视频自动总结。

相关配置：

```bash
DOUYIN_VIDEO_SUMMARY_GATEWAY_BASE_URL=https://api.aigateway.online/v1beta
DOUYIN_VIDEO_SUMMARY_GATEWAY_MODEL=gemini-3.8-flash
DOUYIN_VIDEO_SUMMARY_BASE_URL=https://api.text168.com/v1beta
DOUYIN_VIDEO_SUMMARY_MODEL=gemini-3.7-flash
# 可选备用接口（默认地址已内置，也可显式覆盖）
DOUYIN_VIDEO_SUMMARY_FALLBACK_BASE_URL=https://ai.image.mohuajz.eu.org/v1beta
DOUYIN_VIDEO_SUMMARY_FALLBACK_MODEL=gemini-2.5-flash
# 最低优先级备用接口（使用原生 Gemini 视频接口，不是 /v1 文本接口）
DOUYIN_VIDEO_SUMMARY_HEJU_BASE_URL=https://www.hejuapi.com/v1beta
DOUYIN_VIDEO_SUMMARY_HEJU_MODEL=gemini-3.8-flash
DOUYIN_VIDEO_SUMMARY_MAX_BYTES=8388608
DOUYIN_VIDEO_SUMMARY_SEGMENT_BYTES=8388608
DOUYIN_VIDEO_SUMMARY_TIMEOUT_SECONDS=600
# 瞬时连接或 HTTP 错误时的重试次数（默认 3 次；每段最多请求 4 次）
DOUYIN_VIDEO_SUMMARY_REQUEST_RETRIES=3
```

主 API Key 使用 macOS Keychain 的 `douyin-downloader-text168` 项保存，备用 API Key 使用 `douyin-downloader-summary-fallback` 项保存；不写入前端、数据库或日志。主接口遇到鉴权、限流、上游错误或连接失败时，当前作业会自动切换备用接口。处理阶段和原始/发送大小会显示在视频总结结果中。

heju 也可通过 `DOUYIN_VIDEO_SUMMARY_HEJU_API_KEY` 环境变量配置，优先于其 Keychain 项。新增备用通道不会改变已有三条通道的顺序；同一作业内成功通道的粘滞策略保持不变。粘滞索引仅在内存中，服务重启后新请求重新从最高优先级通道尝试。`/api/config/status` 和 `/api/ai/diagnostics` 提供 heju 的脱敏启用状态，不返回密钥。

作者画廊的“一键视频总结”与“一键提取口播”始终按单视频处理，不合并为作者文档；已有成功结果与运行中的作业会跳过。模型请求级自动重试有次数上限，鉴权、格式错误、缺失本地文件等确定性错误仍需修复配置或文件后再试。

项目提供 **5 种使用方式**，覆盖最终用户、开发者和 AI Agent 三类人群：

| 使用方式 | 面向对象 | 说明 |
|---------|---------|------|
| [Agent Skill](#agent-skillai-agent-集成) | AI Agent | 能力描述（SKILL.md + skill.yaml），AI Agent 据此调用以下方式完成任务 |
| [Web UI](#web-ui浏览器界面) | 最终用户 | 浏览器操作界面（Vue 3 + FastAPI） |
| [CLI](#cli命令行) | 最终用户 / 脚本 | 命令行工具（`python -m skills.douyin_downloader.cli`） |
| [Python API](#python-apisdk) | 开发者 | SDK 式导入，在程序中调用 |
| [Web API](#web-apihttp--websocket) | 开发者 / 其他程序 | HTTP REST 接口 + WebSocket 实时推送 |

> **层级关系说明**：Web UI、CLI、Python API、Web API 是本项目**实际可运行**的四种能力通道；Agent Skill 是"**能力描述层**"——它本身不包含独立可运行的下载实现，而是以标准化的 SKILL.md 文档告诉 AI Agent（Claude Code、OpenCode 等）何时使用、如何调用，Agent 最终仍通过 CLI / Python API / Web API 之一来执行下载。

---

## Agent Skill（AI Agent 集成）

本项目遵循 [Agent Skills](https://agentskills.io) 开放规范，为 AI Agent 提供了标准化的**能力描述**。安装 Skill 后，AI Agent 可自动发现并执行抖音视频下载任务。

### 一键安装（AI Agent 自动安装，推荐）

本项目提供面向 AI Agent 的自动安装指南，用户无需手动操作——只需把仓库地址发给任意支持 Agent Skills 的 AI 工具，并说明参照安装指南执行即可。例如：

> 请参照 https://github.com/amibaren/douyin-DL-skills 仓库中的 `SKILL_INSTALL.md`，将该项目安装为我的 skill。

Agent 会读取 [SKILL_INSTALL.md](./SKILL_INSTALL.md) 自动完成：克隆源码 → 注册到对应客户端（Claude Code / OpenCode 等）→ **安装 Skill 运行所需的全部依赖包**（`pip install -r requirements.txt` + `playwright install chromium`）→ 初始化 F2 配置与 Cookie → 验证安装。

> **安装包含依赖包**：仅把 `SKILL.md` 复制到 skills 目录并不能运行，必须同时安装依赖包（`f2`、`playwright`、`aiosqlite`、`httpx` 等，见 [SKILL_INSTALL.md](./SKILL_INSTALL.md) 第 4 步）并完成 Cookie 初始化。整套流程已写入 [SKILL_INSTALL.md](./SKILL_INSTALL.md)，交给 AI Agent 执行即可。

### 什么是 Agent Skill？

Agent Skill 是一种轻量的开放格式：一个目录下包含带 YAML frontmatter 的 `SKILL.md`（`name` + `description` + 使用说明）。支持该规范的 AI 工具（Claude Code、OpenCode 等）启动时会发现可用 Skill，任务与描述匹配时读取完整指令，由 Agent 按指令**执行其中的步骤**。

**注意**：Agent Skill 描述的是"如何使用本项目的下载能力"，Agent 实际执行下载时调用的是 [CLI](#cli命令行) / [Python API](#python-apisdk) / [Web API](#web-apihttp--websocket) 之一（见下文「使用方式详解」）——Skill 是能力描述，不是独立运行的实现。

### Skill 文件

| 文件 | 作用 |
|------|------|
| `skills/douyin_downloader/SKILL.md` | Skill 定义文档：frontmatter（name/description）+ 使用指令，Agent 读取后据此调用 |
| `skills/douyin_downloader/skill.yaml` | Skill 元数据：公开函数签名、参数与返回值说明 |
| `SKILL_INSTALL.md` | 安装指南（面向 AI Agent 的自动安装流程，含依赖安装与配置） |

Skill 对外暴露的下载能力（与 `core.py` 中的 `DouyinDownloadSkill` 实际方法一一对应）：

| 函数 | 说明 |
|------|------|
| `download_single_video(video_url)` | 下载单个视频 |
| `download_mix_by_id(mix_id, mix_name)` | 根据合集 ID 下载合集 |
| `download_mixes_by_ids(mix_ids_input, mix_name)` | 批量下载多个合集 |
| `download_mix_videos(user_home_url, mix_id)` | 下载指定用户的指定合集（兼容旧接口） |
| `get_mix_info_by_video_url(video_url)` | 根据视频 URL 获取所属合集信息 |
| `download_user_posts(user_url)` | 下载用户主页全部作品 |
| `refresh_cookie(headless)` | 强制刷新 Cookie（浏览器扫码） |

### 如何接入 AI Agent

> 自动安装请直接让 AI Agent 参照 [SKILL_INSTALL.md](./SKILL_INSTALL.md) 执行（含依赖安装）；以下为手动接入方式。

> 本项目**不预设** `.claude/skills/`、`.opencode/skills/` 等自动发现目录，Skill 文件位于 `skills/douyin_downloader/`。按以下方式之一接入后即可被 Agent 发现。**接入时目标目录名请使用连字符 `douyin-downloader`**（与 SKILL.md frontmatter 的 `name` 一致，符合 OpenCode 命名校验）。**注意：只复制 `douyin_downloader` 这一个子目录，切勿复制整个 `skills/` 目录**（`skills/` 是源码根，与安装目标的 skills 目录同名，整目录复制会产生如 `.claude/skills/skills/douyin_downloader/` 的嵌套路径，Agent 无法发现；安装后正确结构应为 `<skills目录>/douyin-downloader/SKILL.md`）。

**① 在本仓库中使用（最简单）**

Agent 的会话工作目录为项目根目录时，直接告知 Agent 读取 `skills/douyin_downloader/SKILL.md`，或将其路径加入项目的 `CLAUDE.md` / `AGENTS.md` 中即可。此时 SKILL.md 中的 `skills.douyin_downloader.*` 导入路径与 CLI 命令均可直接使用。

**② Claude Code（项目级注册）**

```bash
# 方式 A：复制到项目 .claude/skills/ 下
mkdir -p .claude/skills
cp -r skills/douyin_downloader .claude/skills/douyin-downloader

# 方式 B：软链（Claude Code 支持 symlink，可避免双份维护）
ln -s ../../skills/douyin_downloader .claude/skills/douyin-downloader
```

也可注册到个人全局（所有项目可用）：复制到 `~/.claude/skills/douyin-downloader/`。

**③ OpenCode**

```bash
# 项目级：复制到 .opencode/skills/ 下
mkdir -p .opencode/skills
cp -r skills/douyin_downloader .opencode/skills/douyin-downloader

# 全局：复制到 ~/.config/opencode/skills/douyin-downloader/
```

OpenCode 会扫描 `.opencode/skills/*/SKILL.md`、`.claude/skills/*/SKILL.md`、`.agents/skills/*/SKILL.md`。

**④ 其他支持 Agent Skills 规范的客户端**

任意支持 [agentskills.io](https://agentskills.io) 的工具均可通过上述任意路径发现该 Skill。

### 支持的 AI Agent

| Agent 工具 | 接入方式 | 说明 |
|-----------|---------|------|
| **Claude Code** | 复制/软链到 `.claude/skills/` 或 `~/.claude/skills/` | 读取 SKILL.md 后自动触发 |
| **OpenCode** | 复制到 `.opencode/skills/` 或 `~/.config/opencode/skills/` | 读取 SKILL.md 后自动触发 |
| **其他兼容客户端** | 任意支持 agentskills.io 的路径 | 见各客户端文档 |

### Skill 模块结构

```
skills/douyin_downloader/
├── SKILL.md              # Skill 定义文档（Agent 读取，含 name/description）
├── skill.yaml            # Skill 元数据（函数签名、参数说明）
├── __init__.py           # 模块导出
├── core.py               # 下载核心（DouyinDownloadSkill，Skill 函数的实现）
├── cli.py                # 命令行接口（Agent 执行通道之一）
├── web.py                # Web API 服务（Agent 执行通道之一）
├── task_manager_v3.py    # 异步任务管理器
├── cookie_manager.py     # Cookie 管理
├── db.py                 # 数据库操作
├── downloader.py         # 数据库集成的下载器
├── rate_limiter.py       # 风控限速
├── f2_progress.py        # F2 下载进度桥
├── health.py             # 健康检查
├── logger.py             # 日志系统
├── scan_manager.py       # 用户主页作品扫描会话
├── transcription_service.py # 音频转写与口播文案整理
└── video_summary_service.py # Gemini 兼容视频总结
```

---

## 快速开始（传统安装方式）

> 面向 AI Agent 的 Skill 安装请走 [Agent Skill 一键安装](#一键安装ai-agent-自动安装推荐)；以下为传统的手动安装与启动方式。

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

运行默认单元回归：

```bash
.venv/bin/python -m pytest
```

需要访问本机服务的集成测试和浏览器 E2E 测试请显式指定目录：
`.venv/bin/python -m pytest tests/integration` 或
`.venv/bin/python -m pytest tests/e2e`。

### 2. 配置文件

F2 使用分层配置系统，**必须正确配置才能正常使用**。

```bash
# 初始化抖音应用配置
f2 dy --init-config my_douyin.yaml

# 创建 conf 目录并禁用 Bark 通知（避免 60+ 秒超时）
mkdir conf
cat > conf/conf.yaml << 'EOF'
f2:
  enable_bark: false
EOF
```

**配置说明：**
- `conf/conf.yaml` - F2 全局配置（⚠️ 需显式设置 `enable_bark: false`，避免 Bark 通知超时）
- `my_douyin.yaml` - 抖音应用配置（下载路径、并发数等），主要字段：

```yaml
douyin:
  path: Download              # 下载目录
  max_connections: 5          # 最大并发连接数
  timeout: 10                 # 请求超时时间（秒）
  max_retries: 3              # 失败重试次数
  max_tasks: 5                # 异步任务数
  page_counts: 20             # 每页获取作品数
  naming: '{aweme_id}_{desc}' # 文件命名格式
  interval: all               # 下载日期区间
```

### 3. 启动服务

#### 方式一：使用启动脚本（推荐）

启动脚本会自动检测并关闭占用端口的进程。

**Windows:**
```bash
start_douyin_skill.bat
```

**Linux/Mac:**
```bash
./start_douyin_skill.sh
```

菜单选项（仅用于开发调试）：
- `[1]` 启动后端服务（端口 8080）
- `[2]` 启动前端开发服务
- `[3]` 同时启动前后端
- `[4]` 检查依赖
- `[5]` 退出

#### 方式二：手动启动

```bash
# 生产/本地使用：先构建前端，再由 FastAPI 同源托管
cd frontend && npm ci && npm run build && cd ..
python -m skills.douyin_downloader.web --host 127.0.0.1 --port 8080

# 可选开发模式：Vite 开发服务器
cd frontend && npm run dev
```

说明：Vite 开发服务器（默认 `127.0.0.1:5173`）只用于调试，不能作为交付入口。
本项目唯一正式页面是 `http://127.0.0.1:8080/app/`；所有前端改动必须在主目录
`/Users/example/Documents/ChatGPT/agent专家/douyin-DL-skills` 构建后，通过该地址验收。
不要使用 3001 或其他 worktree 页面作为项目版本，避免出现“改了前端但 8080 不生效”的分叉。

---

## 使用方式详解

### Web UI（浏览器界面）

生产构建后，浏览器访问：

- 前端页面：`http://127.0.0.1:8080/app/`
- 后端 API 文档（Swagger）：`http://127.0.0.1:8080/docs`

服务默认代码仍支持本机模式；当前 Mac 的 LaunchAgent 已开启局域网模式，仅放行 `10.22.22.0/24`，访问地址为 `http://10.22.22.243:8080/app/`。不会配置公网代理或端口转发。

功能：可视化发起单视频 / 合集 / 用户作品下载、查看实时进度（WebSocket 推送）、管理任务与 Cookie。三个链接入口均可直接粘贴完整抖音分享文案，页面会自动提取并校验其中唯一的抖音链接；原始文案不会发送到后端。

---

### CLI（命令行）

命令入口：`python -m skills.douyin_downloader.cli`（需在项目根目录下执行）

```bash
# 下载合集（单个 ID；批量合集请使用 Web API /api/download-mix 或 Python API download_mixes_by_ids）
python -m skills.douyin_downloader.cli mix <mix_id> --name "名称" --wait

# 下载单个视频（分享链接或标准 URL）
python -m skills.douyin_downloader.cli video "https://v.douyin.com/xxxxx" --wait

# 下载用户主页作品
python -m skills.douyin_downloader.cli post "https://www.douyin.com/user/xxx" --max 50 --wait

# 查看任务状态 / 获取任务视频列表
python -m skills.douyin_downloader.cli status <task_id>
python -m skills.douyin_downloader.cli videos <task_id> --status failed

# 导出任务元数据 / 重试失败视频
python -m skills.douyin_downloader.cli export <task_id> --output ./metadata.json
python -m skills.douyin_downloader.cli retry <task_id> --wait

# 刷新 Cookie（浏览器扫码登录）
python -m skills.douyin_downloader.cli refresh-cookie

# 列出任务
python -m skills.douyin_downloader.cli list --status completed --limit 20
```

所有命令默认输出 JSON（便于脚本解析），可用 `--format table` 切换为表格；下载类命令加 `--wait` 会阻塞至完成。

---

### Python API（SDK）

将 `skills/douyin_downloader/` 作为 Python 包导入，在程序中直接调用。这是**程序化集成**的用法，与 Agent Skill 无关——SDK 是指把本模块当作库来调用。

#### 核心类

| 类 | 位置 | 用途 |
|----|------|------|
| `DouyinDownloadSkill` | `core.py` | 底层下载能力：单视频 / 合集 / 用户作品 / Cookie |
| `DouyinDownloader` | `downloader.py` | 数据库集成的下载器 |
| `TaskManager` | `task_manager_v3.py` | 完全异步的任务管理器（任务队列、持久化、重试） |
| `TaskDB` | `db.py` | SQLite 数据模型与操作 |

#### 示例：直接下载

```python
import asyncio
from skills.douyin_downloader.core import DouyinDownloadSkill

async def main():
    skill = DouyinDownloadSkill(config_path="./my_douyin.yaml")

    # 下载单个视频
    result = await skill.download_single_video("https://v.douyin.com/xxxxx")
    print(result)

    # 下载合集
    result = await skill.download_mix_by_id("7303349061694785599", "合集名称")
    print(result)

    # 批量下载合集
    result = await skill.download_mixes_by_ids("7303349061694785599, 7303349061694785600")
    print(result)

asyncio.run(main())
```

#### 示例：任务队列（推荐用于大批量下载）

```python
import asyncio
from skills.douyin_downloader.task_manager_v3 import TaskManager

async def main():
    manager = TaskManager()
    await manager.initialize()
    try:
        task = await manager.create_mix_task(
            mix_id="7303349061694785599",
            mix_name="合集名称",
        )
        while task.status in ("pending", "downloading"):
            await asyncio.sleep(5)
            task = await manager.get_task(task.id)
            print(f"进度: {task.progress['percentage']}%")
    finally:
        await manager.shutdown()

asyncio.run(main())
```

#### 打包分发（SDK 方式）

将 `skills/douyin_downloader/` 目录整体复制到目标项目的 `skills/` 下即可使用（保持包结构不变）：

```bash
cd skills
zip -r douyin-downloader.zip douyin_downloader/
```

分发后，对方将 `douyin_downloader/` 放入 Python 路径后即可导入：

```python
import asyncio
from douyin_downloader import DouyinDownloader

async def main():
    downloader = DouyinDownloader()
    task_id = await downloader.download_mix("mix_id", "合集名称")
    print(f"任务ID: {task_id}")

asyncio.run(main())
```

---

### Web API（HTTP + WebSocket）

后端服务启动后（`python -m skills.douyin_downloader.web --host 127.0.0.1 --port 8080`），可通过 HTTP 接口程序化调用。完整接口定义见 `http://127.0.0.1:8080/docs`。

#### 常用端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/status` | 服务状态 |
| `POST` | `/api/download` | 下载单个视频 |
| `POST` | `/api/download-mix` | 下载合集（支持批量 ID） |
| `POST` | `/api/download-user-posts` | 下载用户主页作品 |
| `POST` | `/api/user-info` | 后端验证用户主页元数据 |
| `POST` | `/api/mix-info` | 根据视频 URL 获取所属合集信息 |
| `POST` | `/api/cookie/refresh` | 刷新 Cookie（浏览器扫码） |
| `GET` | `/api/cookie/status` | Cookie 状态 |
| `GET` | `/api/tasks` | 任务列表（支持 `status`、`type`、`keyword`、`sort_by`、`sort_order`、分页） |
| `GET` | `/api/config/status` | 脱敏模型配置状态 |
| `GET` | `/api/ai/diagnostics` | 本机 ffmpeg、Keychain 与模型配置诊断（不上传视频） |
| `POST` | `/api/tasks` | 创建任务 |
| `GET` | `/api/tasks/{task_id}` | 任务详情 |
| `POST` | `/api/tasks/{task_id}/retry` | 仅重试失败项 |
| `POST` | `/api/tasks/{task_id}/continue` | 继续未完成项 |
| `POST` | `/api/tasks/{task_id}/cancel` | 取消任务 |
| `DELETE` | `/api/tasks/{task_id}` | 仅删除任务记录，保留文件 |
| `GET` | `/api/download/file/{filename}` | 下载已下载的文件 |
| `WS` | `/ws` | WebSocket 实时进度推送 |

#### 示例

```bash
# 下载单个视频
curl -X POST http://127.0.0.1:8080/api/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://v.douyin.com/xxxxx"}'

# 下载合集（支持批量 ID：空格/逗号/分号分隔）
curl -X POST http://127.0.0.1:8080/api/download-mix \
  -H "Content-Type: application/json" \
  -d '{"mix_id": "7303349061694785599", "mix_name": "合集名称", "max_counts": 20, "interval": "all"}'

# 搜索任务并按标题排序
curl 'http://127.0.0.1:8080/api/tasks?keyword=猫&type=single&sort_by=title&sort_order=asc&limit=20'
```

合集默认最多 20 个、用户主页默认最多 50 个；只有在用户明确确认后才应传 `max_counts: null` 进行全量下载。日期范围格式为 `YYYY-MM-DD|YYYY-MM-DD`。

#### WebSocket 实时进度

```javascript
const ws = new WebSocket("ws://127.0.0.1:8080/ws");
ws.onmessage = (e) => console.log(JSON.parse(e.data));
```

---

## Cookie 管理（所有使用方式共享）

抖音下载必须使用已登录的 Cookie。**所有使用方式（Web UI / CLI / Python API / Web API / Agent Skill）共享同一个 Cookie 文件 `douyin_cookie.txt`（位于项目根目录），Cookie 的初始化不限于 Web UI——任一使用方式都可以独立完成，初始化后其他使用方式自动复用。**

### 获取流程

Cookie 按以下优先级自动获取：

1. 当前进程内存缓存
2. 本地文件 `douyin_cookie.txt`（自动加载并校验有效性）
3. 都没有 → 启动浏览器尝试自动获取（默认**无头模式**：无可见窗口、无法人工扫码，120 秒后超时）

由此可得：

- 本地已有有效 `douyin_cookie.txt` 时，所有使用方式自动使用，**无需任何初始化操作**；
- **首次使用前必须先主动初始化 Cookie**（下表任一入口均可，人工扫码需显示窗口）；初始化后自动保存到 `douyin_cookie.txt`，所有使用方式自动复用同一文件，直至 Cookie 失效（失效后需再次主动初始化）。

### 各使用方式的初始化入口

| 使用方式 | 初始化 / 刷新 Cookie | 说明 |
|---------|---------------------|------|
| Web UI | 界面"Cookie 管理"面板的"刷新 Cookie"按钮 | 调用 `POST /api/cookie/refresh`，默认显示浏览器窗口扫码 |
| CLI | `python -m skills.douyin_downloader.cli refresh-cookie` | 默认显示浏览器窗口扫码；`--headless` 切换无头 |
| Python API / Skill | `await skill.refresh_cookie(headless=False)` | `headless` 默认 `True`，人工扫码需传 `False` |
| Web API | `POST /api/cookie/refresh`，body `{"headless": false}` | 默认无头，人工扫码需显式传 `headless: false` |
| 状态检查 | Web UI 界面 / `GET /api/cookie/status` | 校验 `douyin_cookie.txt` 中的 Cookie 是否有效 |

> **注意**：人工扫码需要可见的浏览器窗口。CLI 的 `refresh-cookie` 与 Web UI 默认显示窗口；Python API / Web API 的 `refresh_cookie` 默认 `headless=True`（无窗口），如需人工扫码请显式传入 `headless=False`。**下载过程中的自动获取同样为无头模式，无法用于首次人工扫码——请先通过任一入口主动初始化 Cookie。**

---

## 主要功能

- **单视频下载** - 支持完整分享文案、Markdown 链接、分享短链和标准 URL
- **合集下载** - 支持 200+ 视频的大合集全量下载，支持批量合集 ID
- **用户作品下载** - 下载用户主页全部/指定数量作品
- **Cookie 管理** - 浏览器扫码登录获取、自动校验（所有使用方式共享；首次使用需主动初始化，见 [Cookie 管理](#cookie-管理所有使用方式共享)）
- **任务队列** - 异步队列，支持批量任务（完全异步化 V3）
- **实时推送** - WebSocket 毫秒级进度更新
- **断点续传** - 支持中断后自动恢复下载
- **风控保护** - 智能限速、自动降级、串行下载
- **Agent Skill 支持** - 符合 Agent Skills 规范，可接入 AI Agent（含依赖包安装，见 [SKILL_INSTALL.md](./SKILL_INSTALL.md)）

## 项目结构

```
├── skills/douyin_downloader/    # 核心模块（下载能力 + Agent Skill 定义）
│   ├── SKILL.md                 # Agent Skill 定义文档
│   ├── skill.yaml               # Agent Skill 元数据
│   ├── cli.py                   # CLI 入口
│   ├── core.py                  # 下载核心
│   ├── web.py                   # Web API 服务
│   ├── task_manager_v3.py       # 任务管理器 V3
│   └── ...
├── frontend/                    # Web 前端（Vue3）
├── conf/conf.yaml               # F2 全局配置（enable_bark: false）
├── my_douyin.yaml               # 抖音应用配置
├── start_douyin_skill.bat       # Windows 启动脚本
├── start_douyin_skill.sh        # Linux/Mac 启动脚本
├── requirements.txt             # Python 依赖
├── SKILL_INSTALL.md             # Skill 安装指南（AI Agent 自动安装）
└── README.md                    # 本文档
```

## 系统要求

- **Python**: 3.9+
- **Node.js**: 16+ (前端开发)
- **操作系统**: Windows / macOS / Linux

## 常见问题

**Q: 下载速度慢/白屏时间长？**  
A: 检查 `conf/conf.yaml` 是否包含 `enable_bark: false`

**Q: 报错 `'gbk' codec can't encode`？**  
A: Windows 控制台编码问题，已修复。请更新到最新版本。

**Q: Agent 没有自动发现 Skill？**  
A: 本项目不内置 `.claude/skills/` 等自动发现目录，请按[接入方式](#如何接入-ai-agent)手动配置，或直接告知 Agent 读取 `skills/douyin_downloader/SKILL.md`。

**Q: Skill 已注册但下载报"模块不存在"？**  
A: 仅复制 SKILL.md 不会安装依赖。请参照 [SKILL_INSTALL.md](./SKILL_INSTALL.md) 安装依赖包（`pip install -r requirements.txt`）并在仓库根目录或 `PYTHONPATH` 包含仓库根的环境下执行。

## 文档

- [CHANGELOG.md](./CHANGELOG.md) - 版本变更日志
- [SKILL_INSTALL.md](./SKILL_INSTALL.md) - Skill 安装指南（AI Agent 自动安装，含依赖安装）
- [skills/douyin_downloader/SKILL.md](./skills/douyin_downloader/SKILL.md) - Agent Skill 定义（面向 AI Agent）
- [skills/douyin_downloader/README.md](./skills/douyin_downloader/README.md) - 核心模块详细文档
- [frontend/README.md](./frontend/README.md) - 前端开发文档

## 许可证

MIT License
