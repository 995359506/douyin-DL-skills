"""
任务管理器（重构版）

集成：
- TaskDB数据库持久化
- 新DouyinDownloader
- 自动恢复机制
"""

import asyncio
import json
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field, asdict
from threading import Lock

# 新项目模块
try:
    from .logger import get_logger
    from .db import TaskDB, TaskInfo
    from .downloader import DouyinDownloader
except ImportError:
    from logger import get_logger
    from db import TaskDB, TaskInfo
    from downloader import DouyinDownloader

logger = get_logger()


class TaskStatus(Enum):
    """任务状态"""

    PENDING = "pending"
    QUEUED = "pending"  # 兼容旧版Web API
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    COMPLETE = "completed"  # 兼容旧版
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(Enum):
    """任务类型"""

    SINGLE = "single"
    SINGLE_VIDEO = "single"  # 兼容旧版Web API
    MIX = "mix"
    MIX_VIDEO = "mix"  # 兼容旧版Web API
    MIX_USER = "mix_user"


@dataclass
class DownloadTask:
    """下载任务（兼容旧版）"""

    id: str
    type: str
    status: str
    video_name: str
    file_name: str
    original_desc: str
    video_info: Dict[str, Any] = field(default_factory=dict)
    url: Optional[str] = None
    mix_id: Optional[str] = None
    mix_name: Optional[str] = None
    download_path: Optional[str] = None
    progress: Dict[str, Any] = field(
        default_factory=lambda: {
            "current": 0,
            "total": 0,
            "percentage": 0,
            "current_video_name": None,
        }
    )
    result: Dict[str, Any] = field(
        default_factory=lambda: {
            "success": False,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "message": "",
            "completed_at": None,
        }
    )
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    retry_count: int = 0
    download_options: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_db_task(cls, task_info: TaskInfo, videos: List) -> "DownloadTask":
        """从数据库TaskInfo创建"""
        # 从videos列表中获取第一个视频的desc作为video_name
        video_name = task_info.mix_name or "未知视频"
        file_name = task_info.mix_id or ""
        original_desc = task_info.mix_name or ""
        aweme_id = task_info.mix_id or ""

        if videos and len(videos) > 0:
            first_video = videos[0]
            # 处理 TaskVideoInfo 对象或 dict
            if hasattr(first_video, "desc"):
                # TaskVideoInfo 对象
                desc = first_video.desc
                video_aweme_id = first_video.aweme_id
            else:
                # dict
                desc = first_video.get("desc")
                video_aweme_id = first_video.get("aweme_id")

            # 如果是单视频任务，使用视频的desc作为video_name
            if task_info.type == "single" and desc:
                video_name = desc
                original_desc = desc
            # 获取aweme_id
            if video_aweme_id:
                aweme_id = video_aweme_id
                file_name = f"{aweme_id}.mp4"

        # 解析下载选项（JSON字符串 → dict）
        download_options = {}
        if getattr(task_info, "download_options", None):
            try:
                download_options = json.loads(task_info.download_options) or {}
            except Exception:
                download_options = {}

        return cls(
            id=task_info.task_id,
            type=task_info.type,
            status=task_info.status,
            video_name=video_name,
            file_name=file_name,
            original_desc=original_desc,
            mix_id=task_info.mix_id,
            mix_name=task_info.mix_name,
            download_path=task_info.base_path,
            download_options=download_options,
            progress={
                "current": task_info.downloaded_count + task_info.skipped_count,
                "total": task_info.total_count,
                "percentage": int(
                    (task_info.downloaded_count + task_info.skipped_count)
                    / max(task_info.total_count, 1)
                    * 100
                ),
            },
            result={
                "success": task_info.status == "completed",
                "downloaded": task_info.downloaded_count,
                "skipped": task_info.skipped_count,
                "failed": task_info.failed_count,
                "completed_at": task_info.completed_at,
            },
            created_at=task_info.created_at or datetime.now().isoformat(),
            completed_at=task_info.completed_at,
        )


class TaskManager:
    """
    任务管理器（重构版）

    使用TaskDB替代JSON文件存储
    使用DouyinDownloader替代旧版Skill
    """

    def __init__(
        self,
        db_path: str = "./douyin_downloader.db",
        config_path: str = "./my_douyin.yaml",
    ):
        self.db = TaskDB(db_path)
        self.config_path = config_path
        self.downloader = DouyinDownloader(config_path, db_path)
        self.lock = Lock()
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._status_callbacks: List[Callable[[DownloadTask], None]] = []

        # 内存缓存（热数据）
        self._task_cache: Dict[str, DownloadTask] = {}
        self._cache_size = 100

        logger.info(
            "任务管理器初始化完成",
            extra={
                "component": "task_manager",
                "db_path": db_path,
                "config_path": config_path,
            },
        )

    @property
    def tasks(self) -> Dict[str, DownloadTask]:
        """兼容旧版Web API的tasks属性访问"""
        return self._task_cache

    async def create_task(
        self,
        task_type: TaskType,
        video_name: str,
        file_name: str,
        original_desc: str,
        video_info: Dict[str, Any],
        url: str,
        download_path: str,
    ) -> DownloadTask:
        """
        创建新任务（兼容旧版Web API）
        注意：这是一个同步方法，返回DownloadTask对象而非task_id
        """
        task_id = str(uuid.uuid4())

        # 从video_info中获取mix_id和mix_name
        mix_id = video_info.get("mix_id") if video_info else None
        mix_name = video_info.get("mix_name") if video_info else None

        # 先保存到数据库（同步操作）
        try:
            db_task_id = await self.db.create_task(
                task_type=task_type.value,
                mix_id=mix_id,
                mix_name=mix_name,
            )
            # 使用数据库生成的ID
            task_id = db_task_id
        except Exception as e:
            logger.warning(
                "数据库创建任务失败，使用UUID",
                extra={"component": "task_manager", "error": str(e)},
            )

        # 创建DownloadTask对象
        task = DownloadTask(
            id=task_id,
            type=task_type.value,
            status=TaskStatus.PENDING.value,
            video_name=video_name,
            file_name=file_name,
            original_desc=original_desc,
            video_info=video_info,
            url=url,
            download_path=download_path,
        )

        # 添加到缓存
        self._task_cache[task_id] = task

        logger.info(
            "创建任务",
            extra={
                "component": "task_manager",
                "task_id": task_id,
                "type": task_type.value,
                "video_name": video_name,
            },
        )

        return task

    async def create_tasks_batch(
        self, urls: list, task_type: TaskType = TaskType.SINGLE
    ) -> list:
        """批量创建任务（兼容旧版Web API）"""
        tasks = []
        for url in urls:
            task = await self.create_task(
                task_type=task_type,
                video_name="",
                file_name="",
                original_desc="",
                video_info={},
                url=url,
                download_path="",
            )
            tasks.append(task)
        return tasks

    async def initialize(self):
        """初始化任务管理器"""
        # 恢复中断的任务
        recovered = await self.downloader.recover_interrupted_tasks()

        self._running = True
        self._worker_task = asyncio.create_task(self._worker())

        logger.info(
            "任务管理器已启动",
            extra={"component": "task_manager", "recovered_tasks": len(recovered)},
        )

    async def shutdown(self):
        """关闭任务管理器"""
        self._running = False

        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        # 关闭数据库连接（管理器 + 下载器各自的连接）
        if getattr(self.downloader, "db", None) is not None:
            await self.downloader.db.close()
        await self.db.close()

        logger.info("任务管理器已关闭", extra={"component": "task_manager"})

    def register_status_callback(self, callback: Callable[[DownloadTask], None]):
        """注册状态变更回调"""
        if callback not in self._status_callbacks:
            self._status_callbacks.append(callback)

    def unregister_status_callback(self, callback: Callable[[DownloadTask], None]):
        """注销状态变更回调"""
        if callback in self._status_callbacks:
            self._status_callbacks.remove(callback)

    def _notify_status_change(self, task: DownloadTask):
        """通知状态变更"""
        for callback in self._status_callbacks:
            try:
                callback(task)
            except Exception as e:
                logger.error(
                    "状态回调执行失败",
                    extra={"component": "task_manager", "error": str(e)},
                )

    async def _get_task_from_cache_or_db(self, task_id: str) -> Optional[DownloadTask]:
        """从缓存或数据库获取任务"""
        # 先查缓存
        if task_id in self._task_cache:
            return self._task_cache[task_id]

        # 再查数据库
        task_info = await self.db.get_task(task_id)
        if task_info:
            videos = await self.get_task_videos(task_id)  # 使用包装方法获取字典列表
            task = DownloadTask.from_db_task(task_info, videos)

            # 加入缓存
            self._add_to_cache(task)
            return task

        return None

    def _add_to_cache(self, task: DownloadTask):
        """添加任务到缓存（LRU）"""
        # 如果缓存已满，移除最早的
        if len(self._task_cache) >= self._cache_size:
            oldest = next(iter(self._task_cache))
            del self._task_cache[oldest]

        self._task_cache[task.id] = task

    async def create_mix_task(
        self, mix_id: str, mix_name: str = "", max_counts: Optional[int] = None
    ) -> Optional[DownloadTask]:
        """
        创建合集下载任务

        直接启动下载（简化流程）
        """
        # 使用downloader创建并执行任务
        task_id = await self.downloader.download_mix(
            mix_id, mix_name, max_counts, progress_callback=self._on_download_progress
        )

        # 获取任务信息
        task = await self._get_task_from_cache_or_db(task_id)

        if task:
            self._notify_status_change(task)

        return task

    def _on_download_progress(self, current: int, total: int, message: str):
        """下载进度回调"""
        # 这里可以更新WebSocket或其他实时通知
        pass

    async def retry_task(self, task_id: str) -> Optional[DownloadTask]:
        """重试任务（只重试失败的视频）"""
        result = await self.downloader.retry_failed_videos(task_id)

        # 刷新缓存
        task = await self._get_task_from_cache_or_db(task_id)
        if task:
            self._add_to_cache(task)
            self._notify_status_change(task)

        logger.info(
            "任务重试完成",
            extra={"component": "task_manager", "task_id": task_id, **result},
        )

        return task

    async def get_task(self, task_id: str) -> Optional[DownloadTask]:
        """获取任务"""
        return await self._get_task_from_cache_or_db(task_id)

    async def get_tasks(
        self,
        status: Optional[str] = None,
        task_type: Optional[TaskType] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """获取任务列表"""
        # 注意：task_type参数当前未使用，因为数据库schema可能没有该字段
        # 但为了兼容性，保留该参数
        db_tasks = await self.db.get_tasks(status=status, limit=limit, offset=offset)

        tasks = []
        for task_info in db_tasks:
            task_id = task_info.task_id

            # 优先使用缓存中的任务（可能包含更完整的信息）
            if task_id in self._task_cache:
                task = self._task_cache[task_id]
                # 更新数据库中的状态信息
                task.status = task_info.status
            else:
                # 缓存中没有，从数据库重建
                videos = await self.get_task_videos(task_id)  # 使用包装方法获取字典列表
                task = DownloadTask.from_db_task(task_info, videos)
                # 添加到缓存
                self._add_to_cache(task)

            tasks.append(task)

        # 获取总数
        stats = await self.db.get_statistics()
        total = stats.get("total", 0)

        return {"tasks": tasks, "total": total}

    async def delete_task(self, task_id: str) -> bool:
        """删除任务"""
        # 从缓存中移除
        if task_id in self._task_cache:
            del self._task_cache[task_id]

        # 从数据库删除
        await self.db.delete_task(task_id)
        return True

    async def cleanup_old_tasks(self, days: int = 30) -> int:
        """清理旧任务"""
        # 清理前先清空相关缓存
        cutoff = datetime.now() - __import__("datetime").timedelta(days=days)

        tasks_to_remove = [
            task_id
            for task_id, task in self._task_cache.items()
            if task.created_at
            and __import__("datetime").datetime.fromisoformat(task.created_at) < cutoff
            and task.status in ["completed", "failed", "cancelled"]
        ]

        for task_id in tasks_to_remove:
            del self._task_cache[task_id]

        # 清理数据库
        return await self.db.cleanup_old_tasks(days)

    async def get_task_videos(self, task_id: str) -> List[Dict[str, Any]]:
        """获取任务的所有视频"""
        videos = await self.db.get_task_videos(task_id)
        return [
            {
                "aweme_id": v.aweme_id,
                "desc": v.desc,
                "nickname": v.nickname,
                "create_time": v.create_time,
                "status": v.status,
                "file_path": v.file_path,
                "file_size": v.file_size,
                "error_message": v.error_message,
                "download_time": v.download_time,
            }
            for v in videos
        ]

    async def export_task_metadata(self, task_id: str) -> Dict[str, Any]:
        """导出任务元数据"""
        return await self.db.export_task_metadata(task_id)

    async def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        db_stats = await self.db.get_statistics()
        rate_limiter_stats = self.downloader.get_rate_limiter_stats()

        return {"tasks": db_stats, "rate_limiter": rate_limiter_stats}

    async def _worker(self):
        """后台工作线程（简化版）"""
        # 在新架构中，下载直接在create_mix_task中执行
        # 这个worker可以用于定时任务，如自动清理
        while self._running:
            try:
                # 每小时清理一次旧任务
                await asyncio.sleep(3600)
                await self.cleanup_old_tasks()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "Worker错误", extra={"component": "task_manager", "error": str(e)}
                )
                await asyncio.sleep(60)


# 全局任务管理器实例
task_manager = TaskManager()
