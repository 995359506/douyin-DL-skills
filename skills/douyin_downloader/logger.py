"""
统一日志系统

解决F2日志和本项目日志的重复、冗余、格式不统一问题
提供结构化、简洁、易分析的日志输出
"""

import json
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


class JSONFormatter(logging.Formatter):
    """JSON格式日志格式化器"""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "component": getattr(record, "component", "app"),
            "message": record.getMessage(),
        }

        # 添加额外字段
        if hasattr(record, "task_id"):
            log_data["task_id"] = record.task_id
        if hasattr(record, "aweme_id"):
            log_data["aweme_id"] = record.aweme_id
        if hasattr(record, "mix_id"):
            log_data["mix_id"] = record.mix_id
        if hasattr(record, "event"):
            log_data["event"] = record.event
        if hasattr(record, "duration_ms"):
            log_data["duration_ms"] = record.duration_ms

        # 添加异常信息
        if record.exc_info:
            log_data["exception"] = traceback.format_exception(*record.exc_info)

        return json.dumps(log_data, ensure_ascii=False, default=str)


class SimpleFormatter(logging.Formatter):
    """简洁的文本格式（用于开发环境）"""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        component = getattr(record, "component", "app")
        return f"[{timestamp}] [{record.levelname:8}] [{component:12}] {record.getMessage()}"


class DouyinLogger:
    """抖音下载器统一日志管理器"""

    _instance: Optional["DouyinLogger"] = None

    def __new__(cls) -> "DouyinLogger":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self.logger = logging.getLogger("douyin_downloader")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False  # 防止重复输出

    def setup(
        self,
        level: int = logging.INFO,
        json_format: bool = False,
        log_file: Optional[str] = None,
        suppress_f2: bool = True,
    ) -> logging.Logger:
        """
        配置日志系统

        Args:
            level: 日志级别 (DEBUG, INFO, WARNING, ERROR)
            json_format: 是否使用JSON格式（生产环境推荐）
            log_file: 日志文件路径，None则只输出到控制台
            suppress_f2: 是否抑制F2的冗余日志
        """
        # 清除现有处理器
        self.logger.handlers.clear()
        self.logger.setLevel(level)

        # 控制台处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)

        if json_format:
            console_handler.setFormatter(JSONFormatter())
        else:
            console_handler.setFormatter(SimpleFormatter())

        self.logger.addHandler(console_handler)

        # 文件处理器
        if log_file:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(level)
            file_handler.setFormatter(JSONFormatter())
            self.logger.addHandler(file_handler)

        # 抑制F2日志
        if suppress_f2:
            self._suppress_f2_logs()

        return self.logger

    def _suppress_f2_logs(self):
        """抑制F2框架的冗余日志"""
        # 将F2日志级别提升到WARNING，减少冗余输出
        f2_logger = logging.getLogger("f2")
        f2_logger.setLevel(logging.WARNING)

        # 抑制其他第三方库的冗余日志
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    def get_logger(self) -> logging.Logger:
        """获取日志记录器实例"""
        return self.logger


# 全局日志管理器实例
_log_manager = DouyinLogger()


def setup_logging(
    level: int = logging.INFO, json_format: bool = False, log_file: Optional[str] = None
) -> logging.Logger:
    """
    快速配置日志系统

    示例:
        >>> from douyin_downloader.logger import setup_logging
        >>> logger = setup_logging(level=logging.INFO)
        >>> logger.info("开始下载", extra={"component": "download", "mix_id": "123"})
    """
    return _log_manager.setup(level=level, json_format=json_format, log_file=log_file)


def get_logger() -> logging.Logger:
    """获取配置好的日志记录器"""
    if not _log_manager.logger.handlers:
        # 如果未配置，使用默认配置
        _log_manager.setup()
    return _log_manager.logger


# 便捷的日志方法
def info(msg: str, **kwargs):
    """记录INFO级别日志"""
    get_logger().info(msg, extra=kwargs)


def debug(msg: str, **kwargs):
    """记录DEBUG级别日志"""
    get_logger().debug(msg, extra=kwargs)


def warning(msg: str, **kwargs):
    """记录WARNING级别日志"""
    get_logger().warning(msg, extra=kwargs)


def error(msg: str, **kwargs):
    """记录ERROR级别日志"""
    get_logger().error(msg, extra=kwargs)


def critical(msg: str, **kwargs):
    """记录CRITICAL级别日志"""
    get_logger().critical(msg, extra=kwargs)
