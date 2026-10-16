"""
F2 进度桥 - 挂钩 F2 的 Rich ProgressManager，将文件级下载进度转换为任务级回调

F2 0.0.1.7 没有公开的进度回调 API，进度数据在 RichConsoleManager().progress
（ProgressManager 单例）中维护：每个下载文件对应一个 Rich Task，
`_download_chunks` 每写入一个 chunk 就调用一次 progress.update(advance=len(chunk))。

本模块包装 ProgressManager 的 add_task / update 方法，捕获：
- 文件开始（add_task：总大小、文件名）
- 文件进行中（update：completed/total 字节推进）
- 文件完成/失败（update 的 state 变化）

并把所有文件聚合为任务级进度（百分比 + 当前文件名），按节流间隔回调。

并发说明：F2 的 ProgressManager 是全局单例，而下载任务可能并发执行。
本桥采用"单占用"设计：同一时刻只允许一个下载任务安装进度桥，
install() 返回 False 表示已被其他任务占用（该任务放弃文件级中间进度，
仍可通过作品级进度回调获得进度）。这避免了多任务互相覆盖回调导致的状态错乱。
"""

import threading
import time
from typing import Callable, Dict, Optional

from f2.cli.cli_console import RichConsoleManager

# 保护 install/uninstall 与 ProgressManager 方法替换的互斥锁
_patch_lock = threading.Lock()


class F2ProgressBridge:
    """挂钩 F2 RichConsoleManager.progress 单例，捕获 add_task/update 事件"""

    def __init__(self, throttle: float = 0.3):
        """
        Args:
            throttle: 回调节流间隔（秒），chunk 更新频率很高，需要限流
        """
        self._throttle = throttle
        self._callback: Optional[Callable[[int, str], None]] = None
        self._orig_add_task = None
        self._orig_update = None
        self._installed = False
        self._file_progress: Dict[int, Dict] = {}
        self._last_emit = 0.0
        self._pm = None  # 安装时的 ProgressManager 实例（卸载时恢复用）

    def install(self, callback: Callable[[int, str], None]) -> bool:
        """
        安装进度桥，开始捕获 F2 下载进度

        Returns:
            True 安装成功；False 已被其他下载任务占用，本次放弃文件级进度
        """
        with _patch_lock:
            pm = RichConsoleManager().progress
            if self._installed:
                # 重复安装（防御），保持已安装状态
                self._callback = callback
                return True
            if hasattr(pm, "_f2_bridge_active"):
                # 已被其他桥实例占用，本次放弃文件级进度
                return False
            # 保存原始方法并标记占用
            self._orig_add_task = pm.add_task
            self._orig_update = pm.update
            self._pm = pm
            pm._f2_bridge_active = True
            self._callback = callback
            self._file_progress.clear()
            self._last_emit = 0.0
            pm.add_task = self._add_task
            pm.update = self._update
            self._installed = True
            return True

    def uninstall(self) -> None:
        """卸载进度桥，恢复 F2 原始方法"""
        with _patch_lock:
            if not self._installed:
                return
            pm = self._pm or RichConsoleManager().progress
            if self._orig_add_task is not None and hasattr(pm, "_f2_bridge_active"):
                pm.add_task = self._orig_add_task
                pm.update = self._orig_update
                del pm._f2_bridge_active
            self._orig_add_task = None
            self._orig_update = None
            self._callback = None
            self._file_progress.clear()
            self._installed = False
            self._pm = None

    async def _add_task(self, *args, **kwargs):
        if self._orig_add_task is None:
            raise RuntimeError("F2ProgressBridge 未安装")
        task_id = await self._orig_add_task(*args, **kwargs)
        self._file_progress[task_id] = {
            "completed": kwargs.get("completed", 0) or 0,
            "total": kwargs.get("total"),
            "filename": kwargs.get("filename", "") or "",
        }
        self._emit()
        return task_id

    async def _update(self, task_id, *args, **kwargs):
        if self._orig_update is None:
            raise RuntimeError("F2ProgressBridge 未安装")
        await self._orig_update(task_id, *args, **kwargs)

        state = kwargs.get("state")
        # 文件完成/失败后移除记录，避免 _file_progress 无限增长
        # （移除后 total/done 同步减少，聚合比例不变）
        if state in ("completed", "error"):
            self._file_progress.pop(task_id, None)
            self._emit()
            return

        rec = self._file_progress.setdefault(
            task_id, {"completed": 0, "total": None, "filename": ""}
        )
        completed = kwargs.get("completed")
        advance = kwargs.get("advance")
        if completed is not None and advance is None:
            rec["completed"] = completed
        elif advance:
            rec["completed"] += advance
        total = kwargs.get("total")
        if total is not None:
            rec["total"] = total
        filename = kwargs.get("filename")
        if filename is not None:
            rec["filename"] = filename

        # 兜底清理：F2 若未传 state 但已完成（completed >= total），同样移除记录
        if rec["total"] and rec["completed"] >= rec["total"]:
            self._file_progress.pop(task_id, None)
        self._emit()

    def _emit(self) -> None:
        """聚合所有文件进度并回调（带节流）"""
        if self._callback is None:
            return
        now = time.monotonic()
        if now - self._last_emit < self._throttle:
            return
        self._last_emit = now

        total_bytes = 0.0
        done_bytes = 0.0
        current_file = ""
        for rec in self._file_progress.values():
            total = rec["total"]
            if not total:
                continue
            total_bytes += total
            done_bytes += min(rec["completed"] or 0, total)
            if rec["filename"] and rec["filename"] != current_file:
                current_file = rec["filename"]

        if total_bytes <= 0:
            return
        pct = min(99, int(done_bytes / total_bytes * 100))
        self._callback(pct, current_file)
