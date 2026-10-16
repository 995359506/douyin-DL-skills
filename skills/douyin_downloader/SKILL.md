---
name: douyin-downloader
description: 下载抖音（Douyin）视频、合集与用户主页作品，用于内容分析和 RAG 知识库构建。当用户需要"下载抖音视频"、"下载抖音合集"、"douyin download"、"get douyin video"、"download douyin mix" 时使用。基于 F2 框架，内置 Cookie 管理、任务队列、断点续传与风控保护。
---

# Douyin Video Downloader Skill

抖音视频合集下载器，专为AI Agent设计，提供稳定的视频数据采集能力。

## Description

下载抖音(TikTok/Douyin)视频合集，用于内容分析和RAG知识库构建。

主要特性：
- 风控保护：智能限速、自动降级、串行下载
- 断点续传：支持大视频中断恢复
- 数据持久化：SQLite存储所有任务和下载状态
- 相对路径：便于跨机器部署和数据迁移
- 元数据导出：完整视频信息JSON导出

## Installation

```bash
# 确保已安装F2框架
pip install f2

# 安装本技能依赖
pip install httpx aiosqlite
```

## Usage

### CLI 命令

> 以下命令需在项目根目录下执行。模块未安装为系统命令（无 `douyin-dl` 可执行文件），请使用 `python -m skills.douyin_downloader.cli`。

#### 1. 下载合集

```bash
# 基础下载
python -m skills.douyin_downloader.cli mix <mix_id>

# 带名称（便于识别）
python -m skills.douyin_downloader.cli mix 7303349061694785599 --name "马斯克传"

# 限制数量
python -m skills.douyin_downloader.cli mix <mix_id> --max 50

# 等待直到完成（阻塞模式）
python -m skills.douyin_downloader.cli mix <mix_id> --wait

# JSON输出（供Agent解析）
python -m skills.douyin_downloader.cli mix <mix_id> --wait --format json
```

**输出示例**（JSON格式）：
```json
{
  "success": true,
  "mix_id": "7303349061694785599",
  "mix_name": "马斯克传",
  "downloaded": 95,
  "skipped": 3,
  "message": "下载完成: 成功 95, 跳过 3",
  "download_path": "Download/7303349061694785599_马斯克传",
  "has_more": false
}
```

> **说明**：`mix` 命令直接下载并在完成后返回（不再创建后台任务、不返回 `task_id`）。下载文件位于 `download_path`，后续处理请直接扫描该目录。
>
> **注意**：`status` / `videos` / `export` / `retry` 命令操作的是**任务系统中的任务记录**（由 Web API 创建，或历史版本遗留）；`mix` / `video` / `post` 命令下载后不再自动创建任务记录，如需使用这些命令，请保留 Web API 返回的 `task_id`。

#### 2. 查看任务状态

```bash
python -m skills.douyin_downloader.cli status <task_id>
```

**输出示例**：
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "type": "mix",
  "mix_id": "7303349061694785599",
  "mix_name": "马斯克传",
  "status": "completed",
  "progress": {
    "current": 95,
    "total": 100,
    "percentage": 95
  },
  "result": {
    "success": true,
    "downloaded": 95,
    "skipped": 3,
    "failed": 2,
    "completed_at": "2024-02-15T11:05:22"
  }
}
```

#### 3. 获取视频列表

```bash
# 所有视频
python -m skills.douyin_downloader.cli videos <task_id> --format json

# 只显示失败的视频
python -m skills.douyin_downloader.cli videos <task_id> --status failed --format json
```

**输出示例**：
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "total": 100,
  "videos": [
    {
      "aweme_id": "7385019284026772770",
      "desc": "第一集 马斯克出生在南非",
      "nickname": "知识博主小王",
      "create_time": "2024-01-15T08:30:00",
      "status": "downloaded",
      "file_path": "7303349061694785599_马斯克传/7385019284026772770_第一集_马斯克出生在南非.mp4",
      "file_size": 15234567,
      "download_time": "2024-02-15T10:25:33"
    },
    {
      "aweme_id": "7385019284026772771",
      "desc": "第二集 学习经历",
      "status": "failed",
      "error_message": "网络超时，下载失败"
    }
  ]
}
```

#### 4. 导出元数据

```bash
# 输出到stdout
python -m skills.douyin_downloader.cli export <task_id>

# 输出到文件
python -m skills.douyin_downloader.cli export <task_id> --output ./metadata.json
```

#### 5. 重试失败视频

```bash
# 只重试失败的视频
python -m skills.douyin_downloader.cli retry <task_id>

# 等待直到完成
python -m skills.douyin_downloader.cli retry <task_id> --wait
```

#### 6. 列出所有任务

```bash
# 最近10个任务
python -m skills.douyin_downloader.cli list

# 筛选状态
python -m skills.douyin_downloader.cli list --status completed

# 限制数量
python -m skills.douyin_downloader.cli list --limit 20
```

#### 7. 下载单个视频

```bash
# 使用分享链接
python -m skills.douyin_downloader.cli video "https://v.douyin.com/xxxxx" --wait

# 使用标准URL
python -m skills.douyin_downloader.cli video "https://www.douyin.com/video/1234567890" --wait

# JSON输出
python -m skills.douyin_downloader.cli video "https://v.douyin.com/xxxxx" --format json
```

**输出示例**：
```json
{
  "success": true,
  "aweme_id": "7605930437021682950",
  "video_name": "Claude构建多智能体协作实战教程...",
  "file_name": "7605930437021682950.mp4",
  "message": "视频下载成功",
  "download_path": "Download"
}
```

#### 8. 刷新Cookie（浏览器扫码登录）

```bash
# 默认模式：显示浏览器窗口扫码登录
python -m skills.douyin_downloader.cli refresh-cookie

# 无头模式：后台运行
python -m skills.douyin_downloader.cli refresh-cookie --headless

# 表格格式输出
python -m skills.douyin_downloader.cli refresh-cookie --format table
```

**说明**：
- 打开浏览器窗口显示抖音登录二维码
- 请在120秒内完成扫码登录
- 如需取消，关闭浏览器窗口或按 Ctrl+C
- 登录成功后如弹出"保存登录状态"提示，请点击确认；系统也会尝试自动点击该浮层按钮
- 若浏览器进入"验证码中间页"（滑块验证），说明当前网络/IP 被抖音风控，请完成验证或更换网络后重试；日志中会给出明确警告
- 登录成功后自动保存Cookie到本地文件 `douyin_cookie.txt`
- Cookie 文件为所有使用方式（CLI / Web UI / Web API / Python API / Agent Skill）共享：刷新成功后其他方式自动复用；Web UI 的"刷新 Cookie"按钮、`POST /api/cookie/refresh`、`skill.refresh_cookie()` 触发的是同一套流程

#### 9. 健康检查

```python
from skills.douyin_downloader.health import print_health

print_health()
```

## RAG Integration Workflow

### Python API 使用

```python
import asyncio
from skills.douyin_downloader.task_manager_v2 import TaskManager

async def main():
    # 初始化
    manager = TaskManager()
    await manager.initialize()
    
    try:
        # 1. 下载合集
        task = await manager.create_mix_task(
            mix_id="7303349061694785599",
            mix_name="马斯克传"
        )
        print(f"任务创建: {task.id}")
        
        # 2. 等待完成（或轮询检查）
        while task.status in ["pending", "downloading"]:
            await asyncio.sleep(5)
            task = await manager.get_task(task.id)
            print(f"进度: {task.progress['percentage']}%")
        
        # 3. 获取视频列表
        videos = await manager.get_task_videos(task.id)
        
        # 4. 处理每个视频
        for video in videos:
            if video["status"] == "downloaded":
                video_path = f"./Download/{video['file_path']}"
                # 提取音频、转录、入RAG...
                print(f"处理视频: {video['desc']}")
        
        # 5. 导出元数据
        metadata = await manager.export_task_metadata(task.id)
        
    finally:
        await manager.shutdown()

asyncio.run(main())
```

### Agent Shell 脚本示例

```bash
#!/bin/bash

MIX_ID="7303349061694785599"
MIX_NAME="马斯克传"
DOWNLOAD_ROOT="./Download"

# 1. 下载合集（直接下载，返回下载目录）
echo "开始下载合集: $MIX_NAME"
RESULT=$(python -m skills.douyin_downloader.cli mix "$MIX_ID" --name "$MIX_NAME" --wait --format json)
MIX_PATH=$(echo "$RESULT" | python -c "import sys,json; print(json.load(sys.stdin)['download_path'])")
echo "下载完成，视频目录: $MIX_PATH"

# 2. 扫描下载目录中的视频文件
find "$MIX_PATH" -name "*.mp4" | while read video_path; do
    echo "处理: $video_path"

    # 1. 提取音频
    # 2. LLM转录
    # 3. 入RAG知识库
    # process_video("$video_path")
done

echo "处理完成!"
```

## Data Schema

### 文件组织

```
Download/
├── 7303349061694785599_马斯克传/           # 合集目录
│   ├── 7385019284026772770_第一集_马斯克出生在南非.mp4
│   ├── 7385019284026772770_第一集_马斯克出生在南非.mp4.part  # 临时文件（断点续传）
│   ├── 7385019284026772771_第二集_学习经历.mp4
│   └── ...
├── 7605930437021682950.mp4                 # 单视频文件
├── douyin_videos.db                        # F2视频数据库（去重）
└── douyin_downloader.db                    # 任务数据库（项目根目录）
```

**重要**：所有路径使用相对于下载根目录的相对路径，确保跨机器部署时可移植。

### 项目结构

```
skills/douyin_downloader/
├── __init__.py          # 模块导出
├── cli.py               # CLI入口（python -m skills.douyin_downloader.cli）
├── core.py              # 原版下载核心（DouyinDownloadSkill）
├── downloader.py        # 新版下载器（数据库+风控集成）
├── task_manager_v2.py   # 任务管理器（TaskDB集成）
├── db.py                # TaskDB数据库实现
├── rate_limiter.py      # 风控限速器
├── logger.py            # 统一日志系统
├── health.py            # 健康检查
├── f2_progress.py       # F2 下载进度桥（文件级进度 → 任务级回调）
└── SKILL.md             # 本文档
```

**架构说明**：
- CLI层：`cli.py` 提供命令行接口，复用原版 `core.py` 的下载逻辑
- 核心层：`core.py` 包含原版稳定的下载实现
- 数据层：`db.py` + `task_manager_v2.py` 提供任务持久化
- 风控层：`rate_limiter.py` 提供下载限速保护

### Video Object

```json
{
  "aweme_id": "string",           // 视频唯一ID
  "desc": "string",               // 视频标题（已清理）
  "nickname": "string",           // 作者名称
  "create_time": "ISO8601",       // 发布时间
  "status": "downloaded|skipped|failed",
  "file_path": "string",          // 相对路径: "{mix_id}_{mix_name}/{aweme_id}_{desc}.mp4"
  "file_size": 15234567,          // 字节数
  "download_time": "ISO8601",     // 下载完成时间
  "error_message": "string"       // 失败原因（如果失败）
}
```

### Task Object

```json
{
  "task_id": "uuid",
  "type": "mix",
  "mix_id": "7303349061694785599",
  "mix_name": "马斯克传",
  "status": "completed",
  "base_path": "7303349061694785599_马斯克传",
  "statistics": {
    "total": 100,
    "downloaded": 95,
    "skipped": 3,
    "failed": 2
  },
  "created_at": "2024-02-15T10:20:00",
  "completed_at": "2024-02-15T11:05:22"
}
```

## Configuration

### 环境变量

```bash
export DOUYIN_DOWNLOAD_PATH="./Download"        # 下载根目录
export DOUYIN_CONFIG_PATH="./my_douyin.yaml"    # F2配置文件
export DOUYIN_DB_PATH="./douyin_downloader.db"  # 数据库路径
export DOUYIN_LOG_LEVEL="INFO"                  # 日志级别 (DEBUG/INFO/WARNING/ERROR)
```

### Cookie 文件

- Cookie 统一存储在项目根目录 `douyin_cookie.txt`，所有使用方式共享
- 本地存在有效 Cookie 时自动加载，所有使用方式自动复用
- **首次使用需先主动初始化 Cookie**（任一入口均可，不限于 Web UI）：CLI `refresh-cookie`、Web UI "刷新 Cookie"按钮、`POST /api/cookie/refresh`、`skill.refresh_cookie()`；人工扫码时 `headless` 需为 `False`（显示浏览器窗口）
- 注意：下载过程中的自动获取为无头模式（无窗口、无法人工扫码），不能替代首次主动初始化

### 配置文件 (my_douyin.yaml)

```yaml
douyin:
  cookie: "your_cookie_here"
  path: "Download"
  max_connections: 5
  timeout: 10
```

## Risk Control (风控)

### 默认策略（保守）

- **并发数**: 1（串行下载）
- **基础间隔**: 3秒
- **随机抖动**: ±30%
- **失败重试**: 对 HTTP 403/408/429/5xx 自动重试 3 次，默认每次等待 10 秒
- **风控检测**: HTTP 429/403自动降级

### 风控等级

1. **Normal**: 正常下载，3秒间隔
2. **Warning**: 检测到异常，5秒间隔
3. **Restricted**: 触发429/403，10秒间隔
4. **Cooldown**: 连续风控，暂停5分钟

### 预计下载时间

100个视频的合集：约60-90分钟（风控保护模式）

## Error Handling

### 常见错误

| 错误 | 原因 | 处理 |
|------|------|------|
| HTTP 429 | 请求过于频繁 | 自动降级，增加间隔 |
| HTTP 403 | 可能触发抖音风控 | 批量任务会先自动等待 10 秒并重试 3 次；仍失败时再检查 Cookie/网络 |
| 磁盘空间不足 | 磁盘满 | 暂停下载，等待清理 |
| 网络超时 | 网络不稳定 | 自动重试3次 |
| 视频不存在 | 视频被删除 | 标记为失败，继续下一个 |

### 重试策略

Web 批量下载会对当前失败作品自动重试，不会重新下载已经成功的作品。等待期间任务进度会显示“疑似风控，等待 10 秒后自动重试”。默认策略可用环境变量调整：

```bash
export DOUYIN_DOWNLOAD_AUTO_RETRIES=3
export DOUYIN_DOWNLOAD_AUTO_RETRY_DELAY=10
```

超过自动重试上限后，任务才会保留为失败，用户仍可使用下面的手动重试命令或页面中的“只重试失败项”。

```bash
# 自动重试失败视频
python -m skills.douyin_downloader.cli retry <task_id> --wait
```

## Monitoring

### 健康检查

```python
from skills.douyin_downloader.health import check_health

health = check_health()
print(health["status"])  # healthy | degraded | unhealthy
```

### 关键指标

```python
import asyncio
from skills.douyin_downloader.task_manager_v2 import TaskManager

async def main():
    manager = TaskManager()
    await manager.initialize()
    try:
        stats = await manager.get_statistics()
        print(f"总任务: {stats['tasks']['total']}")
        print(f"成功率: {stats['rate_limiter']['success_rate']:.2%}")
    finally:
        await manager.shutdown()

asyncio.run(main())
```

## Maintenance

### 自动清理

默认保留30天的已完成/失败任务，自动清理。

### 手动清理

```python
import asyncio
from skills.douyin_downloader.task_manager_v2 import TaskManager

async def main():
    manager = TaskManager()
    await manager.initialize()
    try:
        deleted_count = await manager.cleanup_old_tasks(days=30)
        print(f"清理了 {deleted_count} 个旧任务")
    finally:
        await manager.shutdown()

asyncio.run(main())
```

## Limitations

1. **单账号限制**: 每个Cookie账号建议日下载量 < 500个视频
2. **风控风险**: 过度下载可能导致账号临时限制
3. **存储空间**: 大合集需要足够的磁盘空间
4. **网络依赖**: 需要稳定的网络连接

## Troubleshooting

### 问题1: 下载速度太慢

**原因**: 风控保护策略（3秒间隔）

**解决**: 这是预期行为，不建议修改。如需加速，可配置多账号轮询。

### 问题2: 频繁触发风控

**解决**: 
1. 检查Cookie是否有效
2. 暂停下载，等待1-2小时
3. 考虑更换账号或IP

### 问题3: 任务状态显示"downloading"但无进展

**解决**: 服务可能中断，重启后会自动恢复任务状态。

```bash
# 查看健康状态
python -c "from skills.douyin_downloader.health import print_health; print_health()"
```

## License

MIT License

## Support

如有问题，请查看日志文件或提交Issue。
