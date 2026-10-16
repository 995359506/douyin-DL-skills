"""
抖音下载器核心模块（重构版）

集成：
- 统一日志系统
- TaskDB数据库
- 风控限速器
- 断点续传支持
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
from datetime import datetime
from dataclasses import dataclass

# F2框架导入
from f2.apps.douyin.db import AsyncVideoDB
from f2.apps.douyin.utils import (
    AwemeIdFetcher,
    MixIdFetcher,
    create_or_rename_user_folder,
)
from f2.utils.conf_manager import ConfigManager

# 本项目模块
try:
    from .logger import get_logger, setup_logging
    from .db import TaskDB, TaskInfo
    from .rate_limiter import RateLimiter, RateLimitConfig
except ImportError:
    from logger import get_logger, setup_logging
    from db import TaskDB, TaskInfo
    from rate_limiter import RateLimiter, RateLimitConfig

logger = get_logger()

# 导入Cookie获取模块
try:
    from .cookie_manager import (
        get_douyin_cookie_auto,
        is_cookie_logged_in,
        load_local_cookie,
        build_douyin_gateway_headers,
    )
except ImportError:
    from cookie_manager import (
        get_douyin_cookie_auto,
        is_cookie_logged_in,
        load_local_cookie,
        build_douyin_gateway_headers,
    )

    def load_local_cookie():
        return None


@dataclass
class DownloadResult:
    """下载结果"""

    success: bool
    aweme_id: Optional[str] = None
    file_path: Optional[str] = None  # 相对路径
    file_size: Optional[int] = None
    message: str = ""
    status_code: Optional[int] = None
    is_skipped: bool = False
    is_resumed: bool = False  # 是否断点续传


class DouyinDownloader:
    """
    抖音下载器（重构版）

    特性：
    - 风控保护：串行下载 + 智能间隔
    - 断点续传：支持大视频中断恢复
    - 数据持久化：TaskDB记录所有状态
    - 自动恢复：启动时恢复未完成任务
    """

    def __init__(
        self,
        config_path: str = "./my_douyin.yaml",
        db_path: str = "./douyin_downloader.db",
        enable_rate_limit: bool = True,
    ):
        self.config_path = config_path
        try:
            self.config_manager = ConfigManager(config_path)
            douyin_config = self.config_manager.get_config("douyin")
            # Python 3.9 不支持 | 操作符，使用传统方式
            if douyin_config is None:
                self.douyin_conf = {}
            else:
                self.douyin_conf = douyin_config
        except Exception as e:
            logger.warning(
                f"无法加载配置文件，使用默认配置: {e}",
                extra={"component": "downloader"},
            )
            self.config_manager = None
            self.douyin_conf = {}

        # 初始化数据库
        self.db = TaskDB(db_path)

        # 初始化风控限速器
        if enable_rate_limit:
            rate_config = RateLimitConfig(
                base_interval=3.0, jitter_ratio=0.3, max_retries=3
            )
            self.rate_limiter = RateLimiter(rate_config)
        else:
            self.rate_limiter = None

        self._cookie: Optional[str] = None
        self._handler: Optional[DouyinHandler] = None

        # 创建专用线程池用于Playwright操作（Cookie获取）
        self._playwright_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="playwright"
        )

        logger.info(
            "下载器初始化完成",
            extra={
                "component": "downloader",
                "config_path": config_path,
                "db_path": db_path,
                "rate_limit": enable_rate_limit,
            },
        )

    def _get_download_root(self) -> Path:
        """获取下载根目录"""
        return Path(self.douyin_conf.get("path", "./Download"))

    def _build_kwargs(self, extra: Optional[Dict] = None) -> Dict:
        """构建F2 kwargs

        注意：必须包含 headers 字段，否则 F2 内部使用 | 操作符会失败
        """
        kwargs = {
            "cookie": self._cookie or "",
            "path": str(self._get_download_root()),
            "timeout": 10,
            "max_connections": 5,
            "headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
                "Referer": "https://www.douyin.com/",
            },
        }
        if extra:
            kwargs.update(extra)
        # 文件名长度控制：保留 aweme_id 前缀（匹配依赖）+ desc 截断 80 字符
        # 与 core._build_kwargs 保持一致；用户自定义模板保持原样
        current_naming = kwargs.get("naming")
        if not current_naming or current_naming in (
            "{create}_{desc}",
            "{aweme_id}_{desc}",
        ):
            kwargs["naming"] = "{aweme_id}_{desc:.80}"
        kwargs["headers"] = build_douyin_gateway_headers(kwargs.get("headers"), kwargs.get("cookie", ""))
        return kwargs

    @staticmethod
    def _build_disk_file_name(
        kwargs: Dict[str, Any],
        aweme_id: str,
        desc: str,
        nickname: str = "",
        create_time: str = "",
    ) -> str:
        """
        生成与 F2 磁盘实际产物一致的文件名（{naming}_video.mp4）

        仅覆盖视频作品（F2 下载视频追加 `_video` 后缀）；图文作品
        （aweme_type=68）的 `_image_N.webp` 不在此范围。
        """
        from f2.apps.douyin.utils import format_file_name

        naming = kwargs.get("naming", "{aweme_id}_{desc:.80}")
        base_name = format_file_name(
            naming,
            {
                "aweme_id": aweme_id,
                "desc": desc,
                "nickname": nickname,
                "create_time": create_time,
            },
        )
        return f"{base_name}_video.mp4"

    def _get_handler(self, kwargs: Dict) -> DouyinHandler:
        """创建Handler（每次都创建新的以使用最新的kwargs）"""
        # F2 构造 handler 模块时会获取 msToken；只在真正访问抖音时导入。
        from f2.apps.douyin.handler import DouyinHandler

        return DouyinHandler(kwargs)

    async def get_cookie(
        self, force_refresh: bool = False, headless: bool = True
    ) -> str:
        """
        获取抖音Cookie

        优先顺序：
        1. 检查内存缓存
        2. 从本地文件加载
        3. 通过浏览器自动获取

        Args:
            force_refresh: 是否强制刷新Cookie
            headless: 是否使用无头浏览器

        Returns:
            Cookie字符串
        """
        # 1. 检查内存缓存
        if self._cookie is not None and not force_refresh:
            if is_cookie_logged_in(self._cookie):
                logger.debug("使用内存缓存的Cookie", extra={"component": "downloader"})
                return self._cookie
            else:
                # 缓存的Cookie无效，重置
                self._cookie = None

        # 2. 尝试从本地加载
        if not force_refresh:
            local_cookie = load_local_cookie()
            if local_cookie and is_cookie_logged_in(local_cookie):
                self._cookie = local_cookie
                logger.info("已从本地加载有效Cookie", extra={"component": "downloader"})
                return self._cookie

        # 3. 通过浏览器自动获取
        logger.info("正在启动浏览器获取Cookie...", extra={"component": "downloader"})
        try:
            loop = asyncio.get_event_loop()
            new_cookie = await loop.run_in_executor(
                self._playwright_executor,
                lambda: get_douyin_cookie_auto(force_refresh, headless),
            )

            # 验证获取到的Cookie
            if not new_cookie:
                raise RuntimeError(
                    "未能获取到Cookie，可能是登录未完成或浏览器被提前关闭"
                )

            if not is_cookie_logged_in(new_cookie):
                raise RuntimeError("获取的Cookie无效，请确保已完成登录")

            self._cookie = new_cookie
            logger.info("Cookie获取成功", extra={"component": "downloader"})
            return self._cookie

        except Exception as e:
            logger.error(f"Cookie获取失败: {e}", extra={"component": "downloader"})
            # 重置Cookie缓存
            self._cookie = None
            raise

    async def refresh_cookie(self, headless: bool = True) -> str:
        """
        强制刷新Cookie

        Args:
            headless: 是否使用无头浏览器

        Returns:
            新的Cookie字符串
        """
        self._cookie = None
        cookie = await self.get_cookie(force_refresh=True, headless=headless)
        # 添加短暂延迟，确保Cookie已写入文件
        await asyncio.sleep(1)
        return self._cookie

    def _check_disk_space(self, required_mb: int = 100) -> bool:
        """检查磁盘空间"""
        root = self._get_download_root()
        try:
            free = shutil.disk_usage(root).free / (1024 * 1024)  # MB
            return free > required_mb
        except Exception:
            return True  # 如果检查失败，假设空间充足

    def _get_part_file_path(self, file_path: Path) -> Path:
        """获取临时文件路径（断点续传）"""
        return file_path.with_suffix(file_path.suffix + ".part")

    async def _update_task_progress_async(self, task_id: str, current: int, total: int):
        """异步更新任务进度（用于同步回调中）"""
        try:
            await self.db.update_task_counts(task_id, downloaded=current)
        except Exception as e:
            logger.warning(
                f"更新进度失败: {e}",
                extra={"component": "downloader", "task_id": task_id},
            )

    @staticmethod
    def _serialize_for_sqlite(data: Dict) -> Dict:
        """
        将字典中的dict/list类型值序列化为JSON字符串，以便存入SQLite
        完全复用原版的逻辑
        """
        return {
            k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
            for k, v in data.items()
        }

    @staticmethod
    async def _filter_fields_for_sqlite(
        table: str, data: Dict, db_path: str = "douyin_videos.db"
    ) -> Dict:
        """
        过滤出数据库表中存在的字段
        完全复用原版的逻辑
        """
        import aiosqlite

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(f"PRAGMA table_info({table})")
            columns = {row[1] for row in await cursor.fetchall()}
            return {k: v for k, v in data.items() if k in columns}

    async def download_mix(
        self,
        mix_id: str,
        mix_name: str = "",
        max_counts: Optional[int] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> str:
        """
        下载合集（直接调用原版逻辑）

        Returns:
            task_id: 任务ID
        """
        # 1. 创建任务
        task_id = await self.db.create_task("mix", mix_id, mix_name)
        await self.db.update_task_status(task_id, "downloading")

        logger.info(
            "开始下载合集",
            extra={
                "component": "downloader",
                "task_id": task_id,
                "mix_id": mix_id,
                "mix_name": mix_name,
            },
        )

        try:
            # 2. 直接调用原版的 download_mix_by_id 逻辑
            # 导入原版Skill
            from .core import DouyinDownloadSkill

            original_skill = DouyinDownloadSkill(self.config_path)

            # 包装进度回调以更新数据库（原版使用同步回调）
            def wrapped_progress_callback(current: int, total: int, message: str):
                # 使用 asyncio.create_task 来运行异步数据库更新
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(
                        self._update_task_progress_async(task_id, current, total)
                    )
                except Exception:
                    pass  # 忽略数据库更新错误

                if progress_callback:
                    progress_callback(current, total, message)

            # 调用原版的下载方法
            result = await original_skill.download_mix_by_id(
                mix_id=mix_id,
                mix_name=mix_name,
                progress_callback=wrapped_progress_callback,
                max_counts=max_counts,
            )

            # 3. 根据原版结果更新我们的数据库
            downloaded = result.get("downloaded", 0)
            skipped = result.get("skipped", 0)

            await self.db.update_task_counts(
                task_id,
                downloaded=downloaded,
                failed=0,  # 原版不返回失败数
            )

            # 获取下载路径
            download_path = result.get("download_path", "")
            if download_path:
                # 更新base_path使用update_task_counts
                await self.db.update_task_counts(
                    task_id,
                    downloaded=downloaded,
                )

            # 4. 完成任务
            final_status = "completed" if result.get("success") else "failed"
            await self.db.update_task_status(
                task_id, final_status, result.get("message", "")
            )

            logger.info(
                "合集下载完成",
                extra={
                    "component": "downloader",
                    "task_id": task_id,
                    "result": result,
                },
            )

            return task_id

        except Exception as e:
            logger.error(
                "合集下载失败",
                extra={"component": "downloader", "task_id": task_id, "error": str(e)},
                exc_info=True,
            )
            await self.db.update_task_status(task_id, "failed", str(e))
            raise

    async def _download_single_video(
        self,
        task_id: str,
        video: Dict[str, Any],
        mix_path: Path,
        handler: DouyinHandler,
        kwargs: Dict[str, Any],
    ) -> DownloadResult:
        """
        下载单个视频（使用F2框架的下载器）

        简化版本：直接调用F2下载器
        """
        aweme_id = str(video.get("aweme_id", ""))
        desc = video.get("desc", "")
        nickname = video.get("nickname", "")
        create_time = video.get("create_time", "")

        # 1. 添加到数据库
        from .db import TaskVideoInfo

        video_info = TaskVideoInfo(
            id=0,  # 自动生成的ID
            task_id=task_id,
            aweme_id=aweme_id,
            desc=desc,
            nickname=nickname,
            sec_user_id=None,
            create_time=create_time,
            status="pending",
            file_path=None,
            file_size=None,
            error_message=None,
            download_time=None,
        )
        await self.db.add_video(video_info)

        # 2. 检查磁盘空间
        if not self._check_disk_space(50):
            return DownloadResult(
                success=False,
                aweme_id=aweme_id,
                message="磁盘空间不足",
            )

        # 3. 生成文件名（使用F2的命名规则）
        file_name = self._build_disk_file_name(kwargs, aweme_id, desc, nickname, create_time)
        file_path = mix_path / file_name

        # 4. 检查是否已存在
        if file_path.exists():
            file_size = file_path.stat().st_size
            await self.db.update_video_status(
                task_id=task_id,
                aweme_id=aweme_id,
                status="skipped",
                file_path=str(file_path.relative_to(self._get_download_root())),
                file_size=file_size,
            )
            return DownloadResult(
                success=True,
                aweme_id=aweme_id,
                file_path=str(file_path.relative_to(self._get_download_root())),
                file_size=file_size,
                is_skipped=True,
                message="文件已存在",
            )

        # 5. 使用F2下载器下载
        try:
            # 使用F2的下载器（传入已经构建好的kwargs）
            f2_downloader = handler.downloader

            # 设置较大的timeout用于下载
            download_kwargs = kwargs.copy()
            download_kwargs["timeout"] = 60

            # 创建下载任务
            await f2_downloader.create_download_tasks(
                download_kwargs, [video], mix_path
            )

            # 检查文件是否下载成功
            if file_path.exists():
                file_size = file_path.stat().st_size

                # 更新数据库
                await self.db.update_video_status(
                    task_id=task_id,
                    aweme_id=aweme_id,
                    status="downloaded",
                    file_path=str(file_path.relative_to(self._get_download_root())),
                    file_size=file_size,
                )

                return DownloadResult(
                    success=True,
                    aweme_id=aweme_id,
                    file_path=str(file_path.relative_to(self._get_download_root())),
                    file_size=file_size,
                    message="下载成功",
                )
            else:
                # 文件不存在，可能下载失败
                return DownloadResult(
                    success=False,
                    aweme_id=aweme_id,
                    message="下载后文件不存在",
                )

        except Exception as e:
            logger.error(
                "视频下载失败",
                extra={
                    "component": "downloader",
                    "aweme_id": aweme_id,
                    "error": str(e),
                },
            )

            await self.db.update_video_status(
                task_id, aweme_id, "failed", error_message=str(e)
            )

            return DownloadResult(
                success=False,
                aweme_id=aweme_id,
                message=str(e),
            )

        # 2. 检查磁盘空间
        if not self._check_disk_space(50):
            return DownloadResult(
                success=False,
                aweme_id=aweme_id,
                message="磁盘空间不足",
            )

        # 3. 生成文件名（使用F2的命名规则）
        file_name = self._build_disk_file_name(kwargs, aweme_id, desc, nickname, create_time)
        file_path = mix_path / file_name

        # 4. 检查是否已存在
        if file_path.exists():
            file_size = file_path.stat().st_size
            await self.db.update_video_status(
                task_id=task_id,
                aweme_id=aweme_id,
                status="skipped",
                file_path=str(file_path.relative_to(self._get_download_root())),
                file_size=file_size,
            )
            return DownloadResult(
                success=True,
                aweme_id=aweme_id,
                file_path=str(file_path.relative_to(self._get_download_root())),
                file_size=file_size,
                is_skipped=True,
                message="文件已存在",
            )

        # 5. 使用F2下载器下载（完全复用原版逻辑）
        try:
            # 获取当前kwargs并设置timeout
            kwargs = self._build_kwargs({"timeout": 60})

            # 使用F2的下载器
            f2_downloader = handler.downloader

            # 创建下载任务（这是原版的核心逻辑）
            await f2_downloader.create_download_tasks(kwargs, [video], mix_path)

            # 检查文件是否下载成功
            if file_path.exists():
                file_size = file_path.stat().st_size

                # 更新数据库
                self.db.update_video_status(
                    task_id,
                    aweme_id,
                    "downloaded",
                    file_path=str(file_path.relative_to(self._get_download_root())),
                    file_size=file_size,
                )

                # 添加到F2数据库（去重）
                try:
                    async with AsyncVideoDB("douyin_videos.db") as vdb:
                        v_serialized = self._serialize_for_sqlite(video)
                        v_filtered = await self._filter_fields_for_sqlite(
                            "video_info", v_serialized
                        )
                        await vdb.add_video_info(
                            ignore_fields=["images_video"], **v_filtered
                        )
                except Exception as e:
                    logger.warning(
                        f"添加到视频数据库失败: {e}",
                        extra={"component": "downloader", "aweme_id": aweme_id},
                    )

                return DownloadResult(
                    success=True,
                    aweme_id=aweme_id,
                    file_path=str(file_path.relative_to(self._get_download_root())),
                    file_size=file_size,
                    message="下载成功",
                )
            else:
                # 文件不存在，可能下载失败
                return DownloadResult(
                    success=False,
                    aweme_id=aweme_id,
                    message="下载后文件不存在",
                )

        except Exception as e:
            logger.error(
                "视频下载失败",
                extra={
                    "component": "downloader",
                    "aweme_id": aweme_id,
                    "error": str(e),
                },
            )

            self.db.update_video_status(
                task_id, aweme_id, "failed", error_message=str(e)
            )

            return DownloadResult(
                success=False,
                aweme_id=aweme_id,
                message=str(e),
            )

        # 2. 检查磁盘空间
        if not self._check_disk_space(50):
            return DownloadResult(
                success=False, aweme_id=aweme_id, message="磁盘空间不足"
            )

        # 3. 生成文件名
        file_name = self._build_disk_file_name(kwargs, aweme_id, desc, nickname, create_time)  # 与F2磁盘产物一致
        file_path = mix_path / file_name
        part_path = self._get_part_file_path(file_path)

        # 4. 检查是否已存在
        if file_path.exists():
            file_size = file_path.stat().st_size
            await self.db.update_video_status(
                task_id=task_id,
                aweme_id=aweme_id,
                status="skipped",
                file_path=str(file_path.relative_to(self._get_download_root())),
                file_size=file_size,
            )
            return DownloadResult(
                success=True,
                aweme_id=aweme_id,
                file_path=str(file_path.relative_to(self._get_download_root())),
                file_size=file_size,
                is_skipped=True,
                message="文件已存在",
            )

        # 5. 检查断点续传
        resume_from = 0
        is_resumed = False
        if part_path.exists():
            resume_from = part_path.stat().st_size
            is_resumed = True
            logger.info(
                "断点续传",
                extra={
                    "component": "downloader",
                    "aweme_id": aweme_id,
                    "resume_from": resume_from,
                },
            )

        # 6. 执行下载
        try:
            # 这里需要调用F2的实际下载逻辑
            # 简化版：假设使用httpx下载
            import httpx

            video_url = (
                video.get("video", {}).get("play_addr", {}).get("url_list", [""])[0]
            )
            if not video_url:
                raise ValueError("无法获取视频URL")

            headers = {}
            if resume_from > 0:
                headers["Range"] = f"bytes={resume_from}-"

            async with httpx.AsyncClient() as client:
                async with client.stream("GET", video_url, headers=headers) as response:
                    if response.status_code not in [200, 206]:
                        return DownloadResult(
                            success=False,
                            aweme_id=aweme_id,
                            status_code=response.status_code,
                            message=f"下载失败: HTTP {response.status_code}",
                        )

                    # 获取总大小
                    total_size = int(response.headers.get("content-length", 0))
                    if resume_from > 0:
                        total_size += resume_from

                    # 写入文件
                    mode = "ab" if resume_from > 0 else "wb"
                    downloaded_size = resume_from

                    with open(part_path, mode) as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)
                            downloaded_size += len(chunk)

                    # 下载完成，重命名
                    part_path.rename(file_path)

                # 更新数据库
                await self.db.update_video_status(
                    task_id=task_id,
                    aweme_id=aweme_id,
                    status="downloaded",
                    file_path=str(file_path.relative_to(self._get_download_root())),
                    file_size=file_size,
                )

                return DownloadResult(
                    success=True,
                    aweme_id=aweme_id,
                    file_path=str(file_path.relative_to(self._get_download_root())),
                    file_size=downloaded_size,
                    is_resumed=is_resumed,
                )

        except Exception as e:
            logger.error(
                "视频下载失败",
                extra={
                    "component": "downloader",
                    "aweme_id": aweme_id,
                    "error": str(e),
                },
            )

            self.db.update_video_status(
                task_id, aweme_id, "failed", error_message=str(e)
            )

            return DownloadResult(success=False, aweme_id=aweme_id, message=str(e))

    async def retry_failed_videos(self, task_id: str) -> Dict[str, int]:
        """
        重试失败的视频

        Returns:
            {"retried": 重试数, "success": 成功数, "failed": 失败数}
        """
        task = await self.db.get_task(task_id)
        if not task:
            return {"retried": 0, "success": 0, "failed": 0}

        # 获取所有失败的视频
        failed_videos = await self.db.get_task_videos(task_id, status="failed")

        if not failed_videos:
            return {"retried": 0, "success": 0, "failed": 0}

        logger.info(
            "开始重试失败视频",
            extra={
                "component": "downloader",
                "task_id": task_id,
                "failed_count": len(failed_videos),
            },
        )

        cookie = await self.get_cookie()
        kwargs = self._build_kwargs({"cookie": cookie})
        handler = self._get_handler(kwargs)

        download_root = self._get_download_root()
        mix_path = download_root / task.base_path if task.base_path else download_root

        retried = 0
        success = 0
        failed = 0

        # 构建kwargs用于下载
        download_kwargs = self._build_kwargs({"timeout": 60})

        for video_info in failed_videos:
            # 风控限速
            if self.rate_limiter:
                can_proceed = await self.rate_limiter.acquire()
                if not can_proceed:
                    break

            # 重新下载
            result = await self._download_single_video(
                task_id,
                {
                    "aweme_id": video_info.aweme_id,
                    "desc": video_info.desc,
                    "nickname": video_info.nickname,
                    "create_time": video_info.create_time,
                },
                mix_path,
                handler,
                download_kwargs,
            )

            retried += 1
            if result.success:
                success += 1
            else:
                failed += 1

        # 更新任务统计
        await self.db.update_task_counts(
            task_id, downloaded=task.downloaded_count + success, failed=failed
        )

        return {"retried": retried, "success": success, "failed": failed}

    async def recover_interrupted_tasks(self) -> List[str]:
        """
        恢复中断的任务

        Returns:
            恢复的任务ID列表
        """
        # 获取所有处于downloading状态的任务（可能是中断的）
        interrupted = await self.db.get_tasks(status="downloading")

        recovered = []
        for task in interrupted:
            # 标记为失败，允许重试
            await self.db.update_task_status(
                task.task_id, "failed", "服务中断，需要重试"
            )
            recovered.append(task.task_id)

        if recovered:
            logger.info(
                f"恢复 {len(recovered)} 个中断任务",
                extra={"component": "downloader", "recovered_tasks": recovered},
            )

        return recovered

    async def get_task_detail(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务详情"""
        return await self.db.export_task_metadata(task_id)

    def get_rate_limiter_stats(self) -> Optional[Dict[str, Any]]:
        """获取风控限速器统计"""
        if self.rate_limiter:
            return self.rate_limiter.get_stats()
        return None

    async def cleanup_old_tasks(self, days: int = 30) -> int:
        """清理旧任务"""
        return await self.db.cleanup_old_tasks(days)
