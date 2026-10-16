"""
健康检查和监控系统

提供：
- 健康检查端点 (/healthz)
- 关键指标监控
- 系统状态查询
"""

import json
import platform
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from .logger import get_logger
from .db import TaskDB

try:
    from .task_manager_v2 import TaskManager
except ImportError:
    from task_manager_v2 import TaskManager

logger = get_logger()


class HealthChecker:
    """健康检查器"""

    def __init__(
        self,
        task_manager: Optional[TaskManager] = None,
        download_root: str = "./Download",
    ):
        self.task_manager = task_manager
        self.download_root = Path(download_root)

    def check_all(self) -> Dict[str, Any]:
        """执行所有健康检查"""
        checks = {
            "database": self._check_database(),
            "disk_space": self._check_disk_space(),
            "download_dir": self._check_download_dir(),
        }

        # 如果有task_manager，添加更多检查
        if self.task_manager:
            checks["rate_limiter"] = self._check_rate_limiter()

        # 确定整体状态
        status = "healthy"
        for check in checks.values():
            if check.get("status") == "critical":
                status = "unhealthy"
                break
            elif check.get("status") == "warning":
                status = "degraded"

        return {
            "status": status,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "checks": checks,
        }

    def _check_database(self) -> Dict[str, Any]:
        """检查数据库状态"""
        try:
            if self.task_manager:
                # 简单查询测试
                stats = self.task_manager.db.get_statistics()
                return {
                    "status": "ok",
                    "message": "数据库连接正常",
                    "total_tasks": stats.get("total", 0),
                }
            else:
                return {"status": "ok", "message": "未配置数据库检查"}
        except Exception as e:
            return {"status": "critical", "message": f"数据库错误: {str(e)}"}

    def _check_disk_space(self) -> Dict[str, Any]:
        """检查磁盘空间"""
        try:
            usage = shutil.disk_usage(self.download_root)
            free_mb = usage.free / (1024 * 1024)
            total_mb = usage.total / (1024 * 1024)
            used_percent = (usage.total - usage.free) / usage.total * 100

            # 判断状态
            if used_percent > 95:
                status = "critical"
                message = f"磁盘空间严重不足: 仅剩 {free_mb:.0f}MB"
            elif used_percent > 90:
                status = "warning"
                message = f"磁盘空间不足: 剩余 {free_mb:.0f}MB"
            else:
                status = "ok"
                message = f"磁盘空间充足: 剩余 {free_mb:.0f}MB / {total_mb:.0f}MB"

            return {
                "status": status,
                "message": message,
                "free_mb": free_mb,
                "total_mb": total_mb,
                "used_percent": used_percent,
            }

        except Exception as e:
            return {"status": "warning", "message": f"无法检查磁盘空间: {str(e)}"}

    def _check_download_dir(self) -> Dict[str, Any]:
        """检查下载目录"""
        try:
            if not self.download_root.exists():
                self.download_root.mkdir(parents=True, exist_ok=True)

            # 测试写入
            test_file = self.download_root / ".health_check_test"
            test_file.write_text("test")
            test_file.unlink()

            return {
                "status": "ok",
                "message": f"下载目录可访问: {self.download_root}",
                "path": str(self.download_root),
            }

        except Exception as e:
            return {"status": "critical", "message": f"下载目录不可写: {str(e)}"}

    def _check_rate_limiter(self) -> Dict[str, Any]:
        """检查风控限速器状态"""
        try:
            if self.task_manager and self.task_manager.downloader:
                stats = self.task_manager.downloader.get_rate_limiter_stats()
                if stats:
                    return {
                        "status": "ok",
                        "message": f"风控状态: {stats.get('current_level', 'unknown')}",
                        "stats": stats,
                    }

            return {"status": "ok", "message": "限速器未启用或未配置"}

        except Exception as e:
            return {"status": "warning", "message": f"无法检查限速器: {str(e)}"}

    def get_metrics(self) -> Dict[str, Any]:
        """获取关键指标"""
        metrics = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "system": {
                "platform": platform.system(),
                "python_version": platform.python_version(),
            },
        }

        if self.task_manager:
            # 任务统计
            stats = self.task_manager.get_statistics()
            metrics["tasks"] = stats.get("tasks", {})
            metrics["rate_limiter"] = stats.get("rate_limiter", {})

        return metrics

    def print_health_report(self):
        """打印健康报告"""
        health = self.check_all()

        print("=" * 50)
        print(f"健康状态: {health['status'].upper()}")
        print(f"检查时间: {health['timestamp']}")
        print("=" * 50)

        for check_name, check_result in health["checks"].items():
            status = check_result.get("status", "unknown")
            icon = {"ok": "✓", "warning": "⚠", "critical": "✗"}.get(status, "?")

            print(f"\n{icon} {check_name.upper()}")
            print(f"   状态: {status}")
            print(f"   消息: {check_result.get('message', 'N/A')}")

        print("\n" + "=" * 50)


# 便捷函数
def check_health(task_manager: Optional[TaskManager] = None) -> Dict[str, Any]:
    """快速健康检查"""
    checker = HealthChecker(task_manager)
    return checker.check_all()


def print_health(task_manager: Optional[TaskManager] = None):
    """打印健康报告"""
    checker = HealthChecker(task_manager)
    checker.print_health_report()
