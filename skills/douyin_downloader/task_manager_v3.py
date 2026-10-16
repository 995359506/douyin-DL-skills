"""
任务管理器 V3 - 参考F2项目设计

完全异步化，支持完整的任务和视频信息管理
"""

import asyncio
import uuid
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field, asdict

try:
    from .logger import get_logger
    from .db import TaskDB, TaskInfo, TaskVideoInfo
    from .downloader import DouyinDownloader
except ImportError:
    from logger import get_logger
    from db import TaskDB, TaskInfo, TaskVideoInfo
    from downloader import DouyinDownloader

logger = get_logger()


class TaskStatus(Enum):
    """任务状态"""

    PENDING = "pending"
    QUEUED = "pending"  # 兼容旧版
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    COMPLETE = "completed"  # 兼容旧版
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(Enum):
    """任务类型"""

    SINGLE = "single"
    SINGLE_VIDEO = "single"  # 兼容旧版
    MIX = "mix"
    MIX_VIDEO = "mix"  # 兼容旧版
    MIX_USER = "mix_user"
    POST = "post"  # 用户主页作品


@dataclass
class DownloadTask:
    """下载任务 - 完整版"""

    id: str
    type: str
    status: str
    video_name: str  # 主视频标题（单视频）或合集名称
    file_name: str
    original_desc: str
    video_info: Dict[str, Any] = field(default_factory=dict)
    url: Optional[str] = None
    mix_id: Optional[str] = None
    mix_name: Optional[str] = None
    download_path: Optional[str] = None
    base_path: Optional[str] = None
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
    download_options: Dict[str, Any] = field(default_factory=dict)  # 存储下载选项：max_counts, interval等
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[Dict[str, str]] = None
    retry_count: int = 0
    summary_status: str = "none"
    # When the list API merges historical single-video attempts by aweme_id,
    # the canonical row carries a compact audit trail for the UI. Detail
    # endpoints build a task directly from one DB record and therefore keep
    # these fields empty, preserving their existing contract.
    history_attempts: List[Dict[str, Any]] = field(default_factory=list)
    history_count: int = 0
    history_failed_count: int = 0
    # 批量任务中的逐条作品明细。列表页用它展开成独立作品卡片，
    # 父任务本身仍保留用于重试、取消和批量统计。
    videos: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_db(
        cls, task_info: TaskInfo, videos: List[TaskVideoInfo]
    ) -> "DownloadTask":
        """从数据库记录创建任务对象"""
        # 确定video_name和video_info
        video_info = {}
        if task_info.type in {"single", "batch_item"} and videos:
            # 单视频（包括批量画廊的隐藏单条引用）：使用第一个视频的desc
            video = videos[0]
            video_name = video.desc or f"视频 {video.aweme_id}"
            aweme_id = video.aweme_id
            video_info = {
                "aweme_id": video.aweme_id,
                "desc": video.desc,
                "nickname": video.nickname,
                "cover": video.cover,
                "sec_user_id": video.sec_user_id,
                "create_time": video.create_time,
            }
        elif task_info.type == "mix":
            # 合集：使用合集名称
            video_name = task_info.mix_name or f"合集 {task_info.mix_id}"
            aweme_id = task_info.mix_id or ""
            video_info = {"mix_id": task_info.mix_id}
        elif task_info.type == "post":
            # 用户作品：使用用户名称
            video_name = task_info.mix_name or f"用户 {task_info.mix_id}"
            aweme_id = task_info.mix_id or ""
            video_info = {"sec_user_id": task_info.mix_id}
        else:
            video_name = "未知视频"
            aweme_id = ""

        # 计算进度
        total = max(len(videos), task_info.total_count or 0)
        downloaded = max(
            sum(1 for v in videos if v.status == "downloaded"),
            task_info.downloaded_count or 0,
        )
        skipped = max(
            sum(1 for v in videos if v.status == "skipped"),
            task_info.skipped_count or 0,
        )
        failed = max(
            sum(1 for v in videos if v.status == "failed"),
            task_info.failed_count or 0,
        )

        error_details: Optional[Dict[str, str]] = None
        if task_info.error_message:
            try:
                parsed_error = json.loads(task_info.error_message)
                if isinstance(parsed_error, dict):
                    error_details = {
                        "stage": str(parsed_error.get("stage", "download")),
                        "code": str(parsed_error.get("code", "DOWNLOAD_FAILED")),
                        "reason": str(parsed_error.get("reason", "下载失败")),
                        "action": str(parsed_error.get("action", "检查后重试")),
                    }
            except (TypeError, ValueError, json.JSONDecodeError):
                error_details = {
                    "stage": "download",
                    "code": "LEGACY_ERROR",
                    "reason": str(task_info.error_message),
                    "action": "检查登录和网络状态后重试",
                }

        def item_error_reason(raw_error: Optional[str]) -> str:
            if not raw_error:
                return "下载失败"
            try:
                parsed = json.loads(raw_error)
                if isinstance(parsed, dict):
                    return str(parsed.get("reason") or "下载失败")
            except (TypeError, ValueError):
                pass
            return str(raw_error)

        failed_items = [
            {
                "aweme_id": video.aweme_id,
                "name": video.desc or video.aweme_id,
                "reason": item_error_reason(video.error_message),
            }
            for video in videos
            if video.status == "failed"
        ]

        video_items = [
            {
                "aweme_id": video.aweme_id,
                "desc": video.desc or "",
                "nickname": video.nickname or "",
                "sec_user_id": video.sec_user_id,
                "create_time": video.create_time,
                "status": video.status,
                "file_path": video.file_path,
                "file_size": video.file_size,
                "error_message": video.error_message,
                "download_time": video.download_time,
                "video_play_addr": video.video_play_addr,
                "cover": video.cover,
                "duration": video.duration,
            }
            for video in videos
        ]

        # 中间进度：下载中优先使用数据库记录的实时进度，
        # 完成/失败时使用统计结果（100% 或实际比例）
        if task_info.status in ("completed", "failed"):
            pct = int((downloaded + skipped) / max(total, 1) * 100)
        else:
            pct = (
                task_info.progress_percentage
                if task_info.progress_percentage is not None
                else int((downloaded + skipped) / max(total, 1) * 100)
            )

        return cls(
            id=task_info.task_id,
            type=task_info.type,
            status=task_info.status,
            video_name=video_name,
            file_name=(
                Path(videos[0].file_path).name
                if task_info.type in {"single", "batch_item"} and videos and videos[0].file_path
                else (f"{aweme_id}.mp4" if aweme_id else "")
            ),
            original_desc=video_name,
            video_info=video_info,
            mix_id=task_info.mix_id,
            mix_name=task_info.mix_name,
            download_path=task_info.result_path or task_info.base_path,
            base_path=task_info.base_path,
            progress={
                "current": downloaded + skipped,
                "total": total,
                "percentage": pct,
                "current_video_name": task_info.current_video_name,
            },
            result={
                "success": task_info.status == "completed",
                "downloaded": downloaded,
                "skipped": skipped,
                "failed": failed,
                "message": (
                    error_details["reason"]
                    if error_details
                    else (
                        f"已完成：下载 {downloaded}，跳过 {skipped}，失败 {failed}"
                        if task_info.status == "completed"
                        else ""
                    )
                ),
                "failed_items": failed_items,
                "completed_at": task_info.completed_at,
            },
            download_options=json.loads(task_info.download_options) if task_info.download_options else {},
            created_at=task_info.created_at or datetime.now().isoformat(),
            completed_at=task_info.completed_at,
            error=error_details,
            history_attempts=task_info.history_attempts,
            history_count=task_info.history_count,
            history_failed_count=task_info.history_failed_count,
            videos=video_items,
        )


class TaskManager:
    """
    任务管理器 V3 - 完全异步化

    参考F2项目设计，支持完整的任务生命周期管理
    """

    def __init__(
        self,
        db_path: str = "./douyin_downloader.db",
        config_path: str = "./my_douyin.yaml",
    ):
        self.db = TaskDB(db_path)
        self.config_path = config_path
        self.downloader = DouyinDownloader(config_path, db_path)
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._status_callbacks: List[Callable[[DownloadTask], None]] = []

        logger.info("任务管理器初始化完成")

    @property
    def tasks(self) -> Dict[str, DownloadTask]:
        """兼容旧版API - 始终返回空字典"""
        return {}

    async def initialize(self) -> None:
        """初始化任务管理器"""
        await self.db.connect()
        recovered_downloads = await self.downloader.recover_interrupted_tasks()
        recovered_jobs = await self.db.recover_interrupted_jobs()
        if recovered_downloads or any(recovered_jobs.values()):
            logger.warning(
                "启动恢复完成: 下载任务=%s, AI作业=%s",
                len(recovered_downloads), recovered_jobs,
            )
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("任务管理器已启动")

    async def shutdown(self) -> None:
        """关闭任务管理器"""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        if getattr(self.downloader, "db", None) is not None:
            await self.downloader.db.close()
        await self.db.close()
        logger.info("任务管理器已关闭")

    # ==================== 任务CRUD ====================

    async def create_single_task(
        self,
        aweme_id: str,
        video_info: Dict[str, Any],
        download_path: str = "Download",
    ) -> DownloadTask:
        """
        创建单视频下载任务

        Args:
            aweme_id: 视频ID
            video_info: 视频信息（包含desc, nickname等）
            download_path: 下载路径
        """
        # 1. 创建任务记录
        task_id = await self.db.create_task(
            task_type="single",
            base_path=download_path,
        )

        # 2. 保存视频信息到videos表
        await self.db.add_video(
            TaskVideoInfo(
                id=0,  # 自动生成
                task_id=task_id,
                aweme_id=aweme_id,
                desc=video_info.get("desc", ""),
                nickname=video_info.get("nickname", ""),
                sec_user_id=video_info.get("sec_user_id"),
                create_time=video_info.get("create_time", datetime.now().isoformat()),
                status="pending",
                file_path=None,
                file_size=None,
                error_message=None,
                download_time=None,
                video_play_addr=video_info.get("video_play_addr"),
                cover=video_info.get("cover"),
                duration=video_info.get("duration"),
            )
        )

        # 3. 获取完整的任务对象
        task = await self.get_task(task_id)
        if task:
            logger.info(f"创建单视频任务: {task_id}, aweme_id: {aweme_id}")

        return task

    async def create_mix_task(
        self,
        mix_id: str,
        mix_name: str = "",
        videos: Optional[List[Dict]] = None,
        download_path: str = "Download",
        max_counts: Optional[int] = None,
        interval: str = "all",
    ) -> DownloadTask:
        """
        创建合集下载任务

        Args:
            mix_id: 合集ID
            mix_name: 合集名称
            videos: 视频列表
            download_path: 下载路径
            max_counts: 最大下载数量
            interval: 日期区间
        """
        # 构建下载选项
        download_options = {"max_counts": max_counts, "interval": interval or "all"}
        
        # 1. 创建任务记录
        task_id = await self.db.create_task(
            task_type="mix",
            mix_id=mix_id,
            mix_name=mix_name,
            base_path=download_path,
            download_options=download_options,
        )

        # 2. 批量保存视频信息
        if videos:
            for video in videos:
                await self.db.add_video(
                    TaskVideoInfo(
                        id=0,
                        task_id=task_id,
                        aweme_id=video["aweme_id"],
                        desc=video.get("desc", ""),
                        nickname=video.get("nickname", ""),
                        sec_user_id=video.get("sec_user_id"),
                        create_time=video.get(
                            "create_time", datetime.now().isoformat()
                        ),
                        status="pending",
                        file_path=None,
                        file_size=None,
                        error_message=None,
                        download_time=None,
                        video_play_addr=video.get("video_play_addr"),
                        cover=video.get("cover"),
                        duration=video.get("duration"),
                    )
                )

            # 更新任务总数
            await self.db.update_task_counts(task_id, total=len(videos))

        # 3. 获取完整的任务对象
        task = await self.get_task(task_id)
        if task:
            logger.info(
                f"创建合集任务: {task_id}, mix_id: {mix_id}, videos: {len(videos) if videos else 0}"
            )

        return task

    async def create_post_task(
        self,
        sec_user_id: str,
        user_name: str = "",
        user_url: str = "",
        max_counts: Optional[int] = None,
        interval: str = "all",
        download_path: str = "Download",
    ) -> DownloadTask:
        """
        创建用户主页作品下载任务

        Args:
            sec_user_id: 用户ID
            user_name: 用户名称
            user_url: 用户主页链接
            max_counts: 最大下载数量
            interval: 日期区间
            download_path: 下载路径
        """
        # 构建下载选项
        download_options = {"max_counts": max_counts, "interval": interval or "all"}
        
        # 1. 创建任务记录
        task_id = await self.db.create_task(
            task_type="post",
            mix_id=sec_user_id,  # 使用 mix_id 存储 sec_user_id
            mix_name=user_name,
            base_path=download_path,
            download_options=download_options,
        )
        # 2. 获取完整的任务对象
        task = await self.get_task(task_id)
        if task:
            logger.info(
                f"创建用户作品任务: {task_id}, sec_user_id: {sec_user_id}, user_name: {user_name}"
            )

        return task

    # 兼容旧版API
    def create_task(
        self,
        task_type: TaskType,
        video_name: str,
        file_name: str,
        original_desc: str,
        video_info: Dict[str, Any],
        url: Optional[str] = None,
        download_path: str = "Download",
        mix_id: Optional[str] = None,
        mix_name: Optional[str] = None,
        max_counts: Optional[int] = None,
        interval: str = "all",
    ) -> DownloadTask:
        """
        创建任务（同步接口，兼容旧版）

        注意：这是一个同步包装，实际异步操作在后台执行
        """
        # 创建临时任务对象（仅用于缓存）
        task_id = str(uuid.uuid4())
        task = DownloadTask(
            id=task_id,
            type=task_type.value,
            status="pending",
            video_name=video_name,
            file_name=file_name,
            original_desc=original_desc,
            video_info=video_info,
            url=url,
            download_path=download_path,
            base_path=download_path,
            mix_id=mix_id,
            mix_name=mix_name,
        )

        # 异步保存到数据库（不等待）
        aweme_id = video_info.get("aweme_id") if video_info else None
        if aweme_id:
            asyncio.create_task(
                self._async_create_task(
                    task_id, task_type, aweme_id, video_name, download_path
                )
            )

        logger.info(f"创建任务（同步）: {task_id}")
        return task

    async def _async_create_task(
        self,
        task_id: str,
        task_type: TaskType,
        aweme_id: str,
        video_name: str,
        download_path: str,
    ) -> None:
        """异步保存任务到数据库"""
        try:
            # 创建任务记录
            await self.db.create_task(
                task_type=task_type.value,
                base_path=download_path,
                task_id=task_id,
            )

            # 保存视频信息
            await self.db.add_video(
                TaskVideoInfo(
                    id=0,
                    task_id=task_id,
                    aweme_id=aweme_id,
                    desc=video_name,
                    nickname="",
                    sec_user_id=None,
                    create_time=datetime.now().isoformat(),
                    status="pending",
                    file_path=None,
                    file_size=None,
                    error_message=None,
                    download_time=None,
                )
            )
        except Exception as e:
            logger.error(f"异步保存任务失败: {task_id}, 错误: {e}")

    async def get_task(self, task_id: str) -> Optional[DownloadTask]:
        """获取任务 - 直接从数据库读取"""
        task_info = await self.db.get_task(task_id)
        if task_info:
            videos = await self.db.get_task_videos(task_id)
            # A detail lookup is by task ID; history aggregation belongs to the list API.
            return DownloadTask.from_db(task_info, videos)
        return None

    async def get_tasks(
        self,
        status: Optional[str] = None,
        task_type: Optional[TaskType] = None,
        limit: int = 100,
        offset: int = 0,
        keyword: Optional[str] = None,
        author: Optional[str] = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> Dict[str, Any]:
        """获取列表；归并、筛选、总数和分页均由数据库统一完成。"""
        task_type_value = task_type.value if task_type else None
        db_tasks = await self.db.get_tasks(
            status,
            task_type_value,
            limit,
            offset,
            keyword=keyword,
            author=author,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        tasks = []
        for task_info in db_tasks:
            videos = await self.db.get_task_videos(task_info.task_id)
            task = DownloadTask.from_db(task_info, videos)
            if task.type in {"post", "mix", "mix_user"} and task.videos:
                feature_statuses = await self.db.get_batch_video_feature_statuses(task.id)
                for item in task.videos:
                    item.update(feature_statuses.get(str(item.get("aweme_id")), {"summary_status": "none"}))
            tasks.append(task)
        summary_statuses = await self.db.get_task_summary_statuses(
            [task.id for task in tasks if task.type == "single"]
        )
        for task in tasks:
            if task.type == "single":
                task.summary_status = summary_statuses.get(task.id, "none")
        total = await self.db.count_tasks(status, task_type_value, keyword=keyword, author=author)
        authors = await self.db.get_task_authors(status, task_type_value, keyword=keyword)

        return {
            "tasks": tasks,
            "total": total,
            "authors": authors,
        }

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        """更新任务状态"""
        await self.db.update_task_status(task_id, status, error_message)

        # 通知回调 - 直接从数据库获取最新数据
        task = await self.get_task(task_id)
        if task:
            for callback in self._status_callbacks:
                try:
                    callback(task)
                except Exception as e:
                    logger.error(f"状态回调失败: {e}")

    async def update_task_progress(
        self,
        task_id: str,
        percentage: int,
        current_video_name: str = "",
    ) -> None:
        """
        更新任务下载进度（中间状态），并广播给订阅者

        进度百分比采用单调递增保护：新的百分比小于数据库当前值时忽略
        （只更新当前文件名），避免作品级估算与文件级进度互相覆盖导致回退。
        """
        try:
            # 读取当前进度，防止百分比回退
            current_task = await self.db.get_task(task_id)
            current_pct = current_task.progress_percentage if current_task else None
            if (
                current_pct is not None
                and percentage is not None
                and percentage < current_pct
            ):
                percentage = None

            await self.db.update_task_progress(task_id, percentage, current_video_name)
        except Exception as e:
            logger.warning(f"更新任务进度失败: {task_id}, 错误: {e}")
            return

        # 通知回调（WebSocket 广播实时进度）
        task = await self.get_task(task_id)
        if task:
            for callback in self._status_callbacks:
                try:
                    callback(task)
                except Exception as e:
                    logger.error(f"状态回调失败: {e}")

    async def update_video_status(
        self,
        task_id: str,
        aweme_id: str,
        status: str,
        file_path: Optional[str] = None,
        file_size: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """更新视频状态"""
        await self.db.update_video_status(
            task_id, aweme_id, status, file_path, file_size, error_message
        )

    async def delete_task(self, task_id: str) -> bool:
        """删除任务"""
        return await self.db.delete_task(task_id)

    # ==================== 下载控制 ====================

    async def start_download(self, task_id: str) -> bool:
        """Deprecated: only the web layer may schedule real downloads."""
        logger.error(
            "start_download is disabled; use the validated web scheduling endpoints: %s",
            task_id,
        )
        return False

    # ==================== 工具方法 ====================

    async def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        return await self.db.get_statistics()

    async def cleanup_old_tasks(self, days: int = 30) -> int:
        """清理旧任务"""
        return await self.db.cleanup_old_tasks(days)

    def register_status_callback(
        self, callback: Callable[[DownloadTask], None]
    ) -> None:
        """注册状态回调"""
        if callback not in self._status_callbacks:
            self._status_callbacks.append(callback)

    def unregister_status_callback(
        self, callback: Callable[[DownloadTask], None]
    ) -> None:
        """注销状态回调"""
        if callback in self._status_callbacks:
            self._status_callbacks.remove(callback)

    def create_tasks_batch(
        self, urls: list, task_type: TaskType = TaskType.SINGLE
    ) -> list:
        """批量创建任务（同步接口，兼容旧版）"""
        tasks = []
        for url in urls:
            task_id = str(uuid.uuid4())
            task = DownloadTask(
                id=task_id,
                type=task_type.value,
                status="pending",
                video_name="",
                file_name="",
                original_desc="",
                video_info={},
                url=url,
                download_path="",
                base_path="",
            )
            tasks.append(task)
        return tasks

    async def retry_task(self, task_id: str) -> Optional[DownloadTask]:
        """Prepare only failed/cancelled work for an endpoint-controlled retry."""
        task = await self.get_task(task_id)
        if not task:
            return None
        failed_videos = await self.db.get_task_videos(task_id, status="failed")
        if task.status not in {"failed", "cancelled"} and not (task.status == "completed" and failed_videos):
            return None
        await self.db.execute(
            """
            UPDATE task_videos
            SET status='pending', error_message=NULL
            WHERE task_id=? AND status='failed'
            """,
            (task_id,),
        )
        await self.db.commit()
        await self.db.update_task_counts(task_id, failed=0)
        await self.db.update_task_progress(task_id, percentage=0, current_video_name="等待重试失败项")
        await self.update_task_status(task_id, "pending")
        return await self.get_task(task_id)


    async def restart_task(self, task_id: str) -> Optional[DownloadTask]:
        """Continue a task while preserving downloaded/skipped item records."""
        task = await self.get_task(task_id)
        if not task:
            return None
        if task.status == "downloading":
            return None
        await self.update_task_status(task_id, "pending")
        return await self.get_task(task_id)

    async def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        task = await self.get_task(task_id)
        if not task or task.status in {"completed", "failed", "cancelled"}:
            return False
        await self.update_task_status(task_id, "cancelled")
        return True

    # ==================== 后台工作线程 ====================

    async def _worker(self) -> None:
        """后台工作线程"""
        while self._running:
            try:
                # 每小时清理一次旧任务
                await asyncio.sleep(3600)
                await self.cleanup_old_tasks()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker错误: {e}")
                await asyncio.sleep(60)


# 全局任务管理器实例
task_manager = TaskManager()
