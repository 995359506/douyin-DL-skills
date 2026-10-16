"""
Douyin Video Downloader Skill

为AI Agent提供的抖音视频下载能力

主要模块：
- logger: 统一日志系统
- db: TaskDB数据库
- rate_limiter: 风控限速器
- downloader: 下载核心
- task_manager_v2: 任务管理器
- cli: CLI接口
- health: 健康检查

使用示例：
    from douyin_downloader import DouyinDownloader

    downloader = DouyinDownloader()
    task_id = await downloader.download_mix("mix_id", "合集名称")

详细文档请查看 SKILL.md
"""

__version__ = "2.0.0"
__author__ = "AI Assistant"

# 导出主要类
try:
    from .logger import get_logger, setup_logging
    from .db import TaskDB, TaskInfo, TaskVideoInfo
    from .rate_limiter import RateLimiter, RateLimitConfig, RiskLevel
    from .downloader import DouyinDownloader, DownloadResult
    from .task_manager_v2 import TaskManager, DownloadTask
    from .health import HealthChecker, check_health
    # 注意：不在这里导入 cli，避免使用 python -m 时出现 RuntimeWarning
except ImportError:
    # 如果是直接运行cli.py，避免循环导入
    pass

__all__ = [
    # 日志
    "get_logger",
    "setup_logging",
    # 数据库
    "TaskDB",
    "TaskInfo",
    "TaskVideoInfo",
    # 风控
    "RateLimiter",
    "RateLimitConfig",
    "RiskLevel",
    # 下载
    "DouyinDownloader",
    "DownloadResult",
    # 任务管理
    "TaskManager",
    "DownloadTask",
    # 健康检查
    "HealthChecker",
    "check_health",
    # 注意：CLI类不从顶层导出，请直接从 cli 模块导入
    # from skills.douyin_downloader.cli import DouyinDLCLI
]
