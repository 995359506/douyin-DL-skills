"""
抖音视频下载器 Skill - 核心模块
基于F2框架实现，100%复用F2内置API

参考文档: https://f2.wiki/guide/apps/douyin/overview
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# 复用F2内置模块
from f2.apps.douyin.dl import DouyinDownloader
from f2.apps.douyin.db import AsyncVideoDB
from f2.apps.douyin.utils import (
    AwemeIdFetcher,
    SecUserIdFetcher,
    MixIdFetcher,
    create_or_rename_user_folder,
)
from f2.utils.conf_manager import ConfigManager
from f2.dl.base_downloader import BaseDownloader
from typing import Callable

# 导入统一日志系统
try:
    from .logger import get_logger
except ImportError:
    from logger import get_logger

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


def clean_filename_for_display(filename: str, max_length: int = 200, max_segments: int = 4) -> str:
    """
    智能清理文件名，移除元数据块，保留人类可读的有效内容

    Args:
        filename: 原始文件名（通常为 "{aweme_id}_{desc}"）
        max_length: 最大字符数
        max_segments: 按下划线分段时保留的最大段数（desc 通常由多个下划线拼接，
                      如 A_B_C_D_E 取前 4 段 A_B_C_D）；不含下划线时保持原样
    """
    if not filename:
        logger.warning("文件名输入为空，返回默认值", extra={"component": "core"})
        return "未知视频"

    content = filename
    original_length = len(content)

    # 第1步：移除ID前缀
    content = re.sub(r"^\d+_", "", content)

    # 第2步：移除重复内容
    if "......" in content:
        parts = content.split("......")
        content = parts[0]

    # 第3步：移除元数据块
    content = re.sub(r"(__[A-Za-z]+__[\u4e00-\u9fa5]+_*)+", "", content)

    # 移除话题标签
    content = re.sub(r"#[\w\u4e00-\u9fa5]+_", "", content)
    content = re.sub(r"#[\w\u4e00-\u9fa5]+$", "", content)

    # 移除_video标记
    content = re.sub(r"_video$", "", content)
    content = re.sub(r"_video_", "_", content)

    # 规范化下划线
    content = re.sub(r"_+", "_", content)
    content = content.strip("_")

    # 第4步：按段截取（desc 通常由下划线拼接，取前 max_segments 段）
    if max_segments > 0:
        segments = content.split("_")
        if len(segments) > max_segments:
            content = "_".join(segments[:max_segments])

    # 检查是否为空
    if not content or len(content.strip()) == 0:
        logger.warning(
            "文件名清理后为空，返回原始内容",
            extra={"component": "core", "original": filename[:50]},
        )
        # 从原始内容提取：移除ID前缀后按段截取
        raw = re.sub(r"^\d+_", "", filename)
        raw = re.sub(r"_+", "_", raw).strip("_")
        if max_segments > 0:
            raw_segments = raw.split("_")
            if len(raw_segments) > max_segments:
                raw = "_".join(raw_segments[:max_segments])
        result = raw[:max_length] if len(raw) > max_length else raw
        return result if result else "未知视频"

    # 截断
    if len(content) > max_length:
        truncated = content[:max_length]
        last_sep = truncated.rfind("_")
        if last_sep > max_length * 0.7:
            truncated = truncated[:last_sep]
        content = truncated + "..."

    logger.info(
        f"[clean_filename] 完成: {original_length} -> {len(content)}字符, 结果={content[:80]}..."
    )
    return content


def _safe_directory_component(value: str, fallback: str) -> str:
    """Keep remote/user-provided names inside one local path component."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (value or "").strip())
    cleaned = re.sub(r"\.{2,}", "_", cleaned).strip(" ._")
    return (cleaned[:120] or fallback)


class ProgressTrackingDownloader(BaseDownloader):
    """
    支持进度跟踪的下载器

    继承自F2的BaseDownloader，添加进度回调功能
    """

    def __init__(self, kwargs: dict, progress_callback: Optional[Callable] = None):
        super().__init__(kwargs)
        self.progress_callback = progress_callback
        self._download_stats = {
            "total_bytes": 0,
            "downloaded_bytes": 0,
            "current_file": "",
        }

    async def _download_chunks(self, request, file, content_length, task_id):
        """
        重写分块下载方法，添加进度回调
        """
        import httpx

        try:
            response = await self.aclient.send(request, stream=True)
            self._download_stats["total_bytes"] = content_length

            async for chunk in response.aiter_bytes():
                await file.write(chunk)
                self._download_stats["downloaded_bytes"] += len(chunk)

                # 调用进度回调
                if self.progress_callback:
                    self._call_progress_callback()

        except Exception as e:
            logger.error(f"下载块失败: {e}")
            raise

    def _call_progress_callback(self):
        """调用进度回调函数"""
        if self.progress_callback:
            try:
                current = self._download_stats["downloaded_bytes"]
                total = self._download_stats["total_bytes"]
                current_file = self._download_stats.get("current_file", "")

                self.progress_callback(current, total, current_file)
            except Exception as e:
                logger.error(f"进度回调失败: {e}")


class DouyinDownloadSkill:
    """
    抖音视频下载器 Skill

    基于F2框架，提供以下功能：
    - 单视频下载
    - 用户合集列表获取
    - 合集视频批量下载
    - Cookie自动管理
    """

    def __init__(self, config_path: str = "./my_douyin.yaml"):
        """
        初始化 DouyinDownloadSkill

        Args:
            config_path: F2配置文件路径，默认为"./my_douyin.yaml"
        """
        self.config_path = config_path
        self.config_manager = ConfigManager(config_path)
        self.douyin_conf = self.config_manager.get_config("douyin") or {}
        self._cookie: Optional[str] = None
        self._handler: Optional[DouyinHandler] = None
        # 创建专用线程池用于Playwright操作
        self._playwright_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="playwright"
        )

    def _build_kwargs(self, extra: Optional[Dict] = None) -> Dict:
        """
        构建F2所需的配置参数

        Args:
            extra: 额外参数覆盖默认配置

        Returns:
            配置参数字典
        """
        kwargs = dict(self.douyin_conf)
        if extra:
            kwargs.update(extra)

        # 确保proxies为字典格式
        if not isinstance(kwargs.get("proxies"), dict):
            kwargs["proxies"] = {"http://": None, "https://": None}

        # 设置默认配置
        kwargs.setdefault("mode", "one")
        kwargs.setdefault("path", "Download")
        kwargs.setdefault("folderize", True)
        # 文件名长度控制：保留 aweme_id 前缀（存在性检查/文件查找依赖前缀匹配），
        # desc 截断到 80 字符，避免长描述导致路径接近系统上限。
        # 旧默认命名模板（{create}_{desc} / {aweme_id}_{desc}）自动升级为截断版；
        # 用户自定义的其他 naming 模板保持原样。
        current_naming = kwargs.get("naming")
        if not current_naming or current_naming in (
            "{create}_{desc}",
            "{aweme_id}_{desc}",
        ):
            kwargs["naming"] = "{aweme_id}_{desc:.80}"
        kwargs.setdefault("music", True)
        kwargs.setdefault("cover", True)
        kwargs.setdefault("desc", True)
        kwargs.setdefault("interval", "all")
        kwargs.setdefault("timeout", 60)
        kwargs.setdefault("max_retries", 5)
        kwargs.setdefault("max_connections", 5)
        kwargs.setdefault(
            "headers",
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
                "Referer": "https://www.douyin.com/",
            },
        )

        kwargs["headers"] = build_douyin_gateway_headers(kwargs.get("headers"), kwargs.get("cookie", ""))
        return kwargs

    async def get_cookie(
        self, force_refresh: bool = False, headless: bool = True
    ) -> str:
        """
        获取抖音Cookie

        Args:
            force_refresh: 是否强制刷新Cookie
            headless: 是否使用无头浏览器

        Returns:
            Cookie字符串
        """
        # 检查缓存的Cookie是否有效
        if self._cookie is not None and not force_refresh:
            if is_cookie_logged_in(self._cookie):
                return self._cookie
            else:
                # 缓存的Cookie无效，重置
                self._cookie = None

        # 尝试从本地加载
        if not force_refresh:
            local_cookie = load_local_cookie()
            if local_cookie and is_cookie_logged_in(local_cookie):
                self._cookie = local_cookie
                logger.info("[DY-SKILL] 已从本地加载有效Cookie")
                return self._cookie

        # 通过浏览器自动获取 - 使用专用线程池避免greenlet线程问题
        logger.info("[DY-SKILL] 正在启动扫码登录浏览器...")
        logger.info("[DY-SKILL] 提示: 请在5分钟内完成扫码登录，或关闭浏览器窗口取消")

        future = None
        try:
            loop = asyncio.get_event_loop()

            # 创建可取消的任务
            future = loop.run_in_executor(
                self._playwright_executor,
                lambda: get_douyin_cookie_auto(force_refresh, headless),
            )

            # 添加超时机制，防止浏览器关闭后无限等待
            new_cookie = await asyncio.wait_for(future, timeout=330)

            # 验证获取到的Cookie
            if not new_cookie:
                raise RuntimeError(
                    "未能获取到Cookie，可能是登录未完成或浏览器被提前关闭"
                )

            if not is_cookie_logged_in(new_cookie):
                raise RuntimeError("获取的Cookie无效，请确保已完成登录")

            self._cookie = new_cookie
            logger.info("[DY-SKILL] Cookie获取成功")
            return self._cookie

        except asyncio.CancelledError:
            # 用户取消操作（Ctrl+C 或程序终止）
            logger.warning("[DY-SKILL] Cookie获取被用户取消")
            if future and not future.done():
                future.cancel()
            self._cookie = None
            raise RuntimeError("操作已取消")

        except asyncio.TimeoutError:
            logger.error("[DY-SKILL] Cookie获取超时: 5分钟内未完成登录")
            if future and not future.done():
                future.cancel()
            self._cookie = None
            raise RuntimeError("获取Cookie超时: 请在5分钟内完成登录，或关闭浏览器取消")

        except Exception as e:
            logger.error(f"[DY-SKILL] Cookie获取失败: {e}")
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
        import asyncio

        await asyncio.sleep(1)
        return cookie

    def _get_handler(self, kwargs: Dict) -> DouyinHandler:
        """
        获取或创建 DouyinHandler 实例

        Args:
            kwargs: 配置参数

        Returns:
            DouyinHandler实例
        """
        # F2 构造 handler 模块时会获取 msToken；只在真正访问抖音时导入。
        from f2.apps.douyin.handler import DouyinHandler

        # 登录刷新后不能继续复用携带旧 Cookie 的详情客户端。
        if self._handler is None or self._handler.kwargs.get("cookie") != kwargs.get("cookie"):
            self._handler = DouyinHandler(kwargs)
        return self._handler

    @staticmethod
    def _serialize_for_sqlite(data: Dict) -> Dict:
        """
        将字典中的dict/list类型值序列化为JSON字符串，以便存入SQLite

        Args:
            data: 原始数据字典

        Returns:
            处理后的数据字典
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

        Args:
            table: 表名
            data: 待过滤的数据
            db_path: 数据库路径

        Returns:
            仅包含表中字段的数据字典
        """
        import aiosqlite

        async with aiosqlite.connect(db_path) as db:
            async with db.execute(f"PRAGMA table_info({table})") as cursor:
                columns = [row[1] async for row in cursor]
        return {k: v for k, v in data.items() if k in columns}

    @staticmethod
    def _is_video_file_exists(
        aweme_data: Dict, user_path: Path, naming_rule: str = "{aweme_id}_{desc}"
    ) -> bool:
        """
        检查媒体文件是否已存在（视频或图文）

        图文作品（aweme_type=68）下载的是 webp/jpeg 图片与实况 mp4，
        因此同时匹配视频与图片后缀。

        Args:
            aweme_data: 作品数据
            user_path: 搜索路径
            naming_rule: 命名规则

        Returns:
            是否存在
        """
        aweme_id = str(aweme_data.get("aweme_id", ""))
        media_exts = "mp4|mov|mkv|flv|webp|jpeg|jpg|png|avif"
        pattern = re.compile(
            rf"^{re.escape(aweme_id)}_?.*\.({media_exts})$", re.IGNORECASE
        )

        if not user_path.exists():
            return False

        for f in user_path.rglob("*"):
            if f.is_file() and pattern.match(f.name):
                return True
        return False

    @staticmethod
    def _find_downloaded_file(aweme_data: Dict, user_path: Path) -> str:
        """
        在下载目录中查找该作品实际下载的主文件（视频或图文）

        按文件类型优先级选择（避免选中封面/音乐等附属文件）：
          1. F2 视频产物 ``*_video.(mp4|mov|mkv|flv)``（普通视频与实况照片）
          2. 任意视频文件 ``.(mp4|mov|mkv|flv)``
          3. 图文产物 ``*_image_*.`` 图片（排除 ``*_cover.*`` 封面）

        同一优先级内取 mtime 最新；mtime 相同时按文件名排序，
        避免 rglob 遍历顺序导致的不确定结果（下载完成瞬间文件 mtime 可能同秒）。

        Args:
            aweme_data: 作品数据
            user_path: 搜索路径

        Returns:
            找到的文件名，未找到返回空字符串
        """
        aweme_id = str(aweme_data.get("aweme_id", ""))
        if not aweme_id or not user_path.exists():
            return ""
        video_exts = "mp4|mov|mkv|flv"
        image_exts = "webp|jpeg|jpg|png|avif"

        # (优先级, -mtime, 文件名) —— 优先级最小、mtime 最大、文件名最小者胜出
        candidates: List[tuple] = []
        for f in user_path.rglob("*"):
            if not f.is_file():
                continue
            name = f.name
            # 1. F2 视频产物 *_video.<video_ext>
            if re.search(
                rf"^{re.escape(aweme_id)}.*_video\.({video_exts})$", name, re.IGNORECASE
            ):
                candidates.append((0, -f.stat().st_mtime, name))
            # 2. 任意视频文件
            elif re.search(
                rf"^{re.escape(aweme_id)}_?.*\.({video_exts})$", name, re.IGNORECASE
            ):
                candidates.append((1, -f.stat().st_mtime, name))
            # 3. 图文图片（优先 *_image_N.* 内容图，其次其他图片如封面兜底）
            elif re.search(
                rf"^{re.escape(aweme_id)}.*_image_\d+\.({image_exts})$",
                name,
                re.IGNORECASE,
            ):
                candidates.append((2, -f.stat().st_mtime, name))
            elif re.search(
                rf"^{re.escape(aweme_id)}_?.*\.({image_exts})$", name, re.IGNORECASE
            ):
                candidates.append((3, -f.stat().st_mtime, name))

        if not candidates:
            return ""
        return min(candidates)[2]

    @staticmethod
    def _default_file_name(aweme_data: Dict, aweme_id: str) -> str:
        """
        根据作品类型返回默认文件名（未找到实际文件时的兜底）

        图文作品（aweme_type=68）下载的是图片/实况文件，视频作品是 mp4。

        Args:
            aweme_data: 作品数据
            aweme_id: 作品ID

        Returns:
            默认文件名
        """
        aweme_type = aweme_data.get("aweme_type")
        if aweme_type == 68:
            return f"{aweme_id}_image_1.webp"
        return f"{aweme_id}.mp4"

    async def download_single_video(
        self,
        video_url: str,
        progress_callback: Optional[Callable] = None,
        file_progress_callback: Optional[Callable[[int, str], None]] = None,
        download_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        下载单个视频

        Args:
            video_url: 视频链接
            progress_callback: 进度回调函数(current: int, total: int, message: str)
            file_progress_callback: 文件级进度回调函数(percentage: int, current_file: str)

        Returns:
            包含下载状态和视频信息的字典
        """
        result = {"success": False, "aweme_id": None, "message": ""}

        try:
            cookie = await self.get_cookie()
            kwargs = self._build_kwargs({"cookie": cookie, **({"path": download_path} if download_path else {})})

            # 为合集列表获取设置较小的分页间隔（2秒），提高速度
            # 下载时会临时修改为60秒
            kwargs["timeout"] = 2

            handler = self._get_handler(kwargs)

            # 从 URL 中提取视频ID
            aweme_id = await AwemeIdFetcher.get_aweme_id(video_url)
            if not aweme_id:
                raise ValueError(f"无法从URL提取视频ID: {video_url}")

            # 进度：获取视频信息
            if progress_callback:
                progress_callback(20, 100, "正在获取视频信息...")

            logger.info(f"[DY-SKILL] 开始获取视频数据，aweme_id: {aweme_id}")
            video = await handler.fetch_one_video(aweme_id)
            aweme_data = video._to_dict()
            video_desc = aweme_data.get("desc", "")

            logger.info(
                f"[DY-SKILL] 视频数据获取成功，aweme_id: {aweme_id}, 描述: {video_desc}"
            )

            user_path = Path(kwargs["path"])

            # 双重校验：数据库和本地文件
            async with AsyncVideoDB("douyin_videos.db") as vdb:
                video_info = await vdb.get_video_info(str(aweme_id))
                file_exists = self._is_video_file_exists(
                    aweme_data, user_path, kwargs.get("naming", "{aweme_id}_{desc}")
                )

                if video_info and file_exists:
                    result["success"] = True
                    result["message"] = f"视频已存在，跳过下载"
                    result["aweme_id"] = aweme_id
                    result["video_name"] = clean_filename_for_display(
                        f"{aweme_id}_{video_desc}"
                    )
                    result["file_name"] = self._find_downloaded_file(
                        aweme_data, user_path
                    ) or self._default_file_name(aweme_data, aweme_id)
                    result["original_desc"] = video_desc
                    result["download_path"] = str(user_path)
                    logger.info(f"[DY-SKILL] 视频 {aweme_id} [{video_desc}] 已存在")
                    return result
                elif video_info and not file_exists:
                    logger.info(
                        f"[DY-SKILL] 视频 {aweme_id} [{video_desc}] 文件缺失，将重新下载"
                    )

            # 执行下载
            # 进度：开始下载
            if progress_callback:
                progress_callback(40, 100, "正在下载视频...")

            logger.info(f"[DY-SKILL] 开始下载视频，aweme_id: {aweme_id}")
            downloader = DouyinDownloader(kwargs)
            # 安装 F2 进度桥，捕获文件级中间进度（被并发任务占用时放弃）
            bridge = None
            if file_progress_callback:
                from .f2_progress import F2ProgressBridge

                bridge = F2ProgressBridge()
                if not bridge.install(file_progress_callback):
                    bridge = None
            try:
                await downloader.create_download_tasks(kwargs, aweme_data, user_path)
            finally:
                if bridge is not None:
                    bridge.uninstall()

            # 进度：保存数据
            if progress_callback:
                progress_callback(80, 100, "正在保存...")

            # 保存到数据库
            async with AsyncVideoDB("douyin_videos.db") as vdb:
                aweme_data_serialized = self._serialize_for_sqlite(aweme_data)
                aweme_data_filtered = await self._filter_fields_for_sqlite(
                    "video_info", aweme_data_serialized
                )
                await vdb.add_video_info(
                    ignore_fields=["images_video"], **aweme_data_filtered
                )

            result["success"] = True
            result["message"] = f"视频下载成功"
            # 添加视频名称和文件名信息（用于前端展示）
            raw_name = f"{aweme_id}_{video_desc}"
            cleaned_name = clean_filename_for_display(raw_name)
            logger.info(
                f"[DY-SKILL] 清理视频名称: 原始长度={len(raw_name)}, 清理后长度={len(cleaned_name)}, 内容={cleaned_name[:100]}..."
            )
            result["video_name"] = cleaned_name if cleaned_name else video_desc[:100]
            result["file_name"] = self._find_downloaded_file(
                aweme_data, user_path
            ) or self._default_file_name(aweme_data, aweme_id)
            result["aweme_id"] = aweme_id
            result["original_desc"] = video_desc
            # 添加下载路径
            result["download_path"] = str(user_path)
            logger.info(
                f"[DY-SKILL] 视频下载完成，aweme_id: {aweme_id}, video_name={result['video_name'][:50]}..."
            )

        except Exception as e:
            result["message"] = f"下载失败: {str(e)}"
            logger.error(f"[DY-SKILL] {result['message']}")

        return result

    async def get_mix_info_by_video_url(self, video_url: str) -> Dict[str, Any]:
        """
        根据视频URL获取所属合集信息

        Args:
            video_url: 视频链接

        Returns:
            包含mix_id, mix_name, mix_desc, total_videos的字典
        """
        result = {
            "success": False,
            "mix_id": None,
            "mix_name": "",
            "mix_desc": "",
            "total_videos": 0,
            "message": "",
        }

        try:
            cookie = await self.get_cookie()
            kwargs = self._build_kwargs({"cookie": cookie})
            handler = self._get_handler(kwargs)

            aweme_id = await AwemeIdFetcher.get_aweme_id(video_url)
            logger.info(f"[DY-SKILL] 获取到aweme_id: {aweme_id}")

            video = await handler.fetch_one_video(aweme_id)
            logger.info(
                f"[DY-SKILL] 获取到视频数据, mix_id={video.mix_id}, mix_name={video.mix_name}"
            )

            result["mix_id"] = video.mix_id
            result["mix_name"] = video.mix_name
            result["mix_desc"] = video.mix_desc

            # 获取合集中的视频数量 - 暂时跳过以避免超时
            # 可以在下载时动态显示进度
            if video.mix_id:
                result["total_videos"] = "查看详情"

            result["success"] = True
            result["message"] = "合集信息获取成功"
            logger.info(f"[DY-SKILL] 合集信息获取成功: mix_id={result['mix_id']}")

        except Exception as e:
            result["message"] = f"获取合集信息失败: {str(e)}"
            logger.error(f"[DY-SKILL] {result['message']}")
            import traceback

            logger.error(traceback.format_exc())

        return result

    async def download_mix_by_id(
        self,
        mix_id: str,
        mix_name: str = "",
        progress_callback: Optional[Callable] = None,
        file_progress_callback: Optional[Callable[[int, str], None]] = None,
        max_counts: Optional[int] = None,
        interval: str = "all",
    ) -> Dict[str, Any]:
        """
        根据合集ID下载合集视频

        Args:
            mix_id: 合集ID
            mix_name: 合集名称（用于目录命名）
            progress_callback: 进度回调函数(current: int, total: int, current_video: str)
            file_progress_callback: 文件级进度回调函数(percentage: int, current_file: str)
            max_counts: 最大下载数量，None表示全量下载
            interval: 日期区间，格式如 "2024-01-01|2024-12-31" 或 "all"

        Returns:
            下载结果状态
        """
        result = {"success": False, "downloaded": 0, "skipped": 0, "message": ""}

        try:
            cookie = await self.get_cookie()
            kwargs = self._build_kwargs({"cookie": cookie})

            # 为合集列表获取设置较小的分页间隔（2秒），提高速度
            # 下载时会临时修改为60秒
            kwargs["timeout"] = 2

            # 设置日期区间
            kwargs["interval"] = interval

            handler = self._get_handler(kwargs)

            user_path = (
                Path(kwargs["path"])
                / f"{mix_id}_{_safe_directory_component(mix_name, '合集')}"
                if mix_name
                else Path(kwargs["path"]) / str(mix_id)
            )

            downloaded_count = 0
            skipped_count = 0
            total_processed = 0

            # 通知开始
            if progress_callback:
                total_hint = f"最多 {max_counts} 个" if max_counts else "全量"
                progress_callback(0, 100, f"准备下载 ({total_hint})...")

            page_count = 0
            has_more_pages = True
            estimated_total = max_counts if max_counts else 100  # 初始估计值

            async for page in handler.fetch_user_mix_videos(
                mix_id, max_counts=max_counts
            ):
                video_list = page._to_list()
                page_count += 1

                # 检查是否还有更多页
                has_more_pages = getattr(page, "has_more", False)

                if not video_list:
                    if not has_more_pages:
                        break
                    continue

                # 更新估计总数：如果还有更多页，估计总数应该比当前处理数大
                current_processed = downloaded_count + skipped_count
                if has_more_pages and not max_counts:
                    # 根据已处理的页数估算总数（每页约10-20个视频）
                    estimated_total = max(current_processed + len(video_list) * 2, 100)
                else:
                    estimated_total = max(
                        current_processed + len(video_list), estimated_total
                    )

                # 去重检查
                async with AsyncVideoDB("douyin_videos.db") as vdb:
                    to_download = []

                    for v in video_list:
                        video_desc = v.get("desc", "")
                        video_id = str(v.get("aweme_id"))

                        video_info = await vdb.get_video_info(video_id)
                        file_exists = self._is_video_file_exists(
                            v, user_path, kwargs.get("naming", "{aweme_id}_{desc}")
                        )

                        if video_info and file_exists:
                            skipped_count += 1
                            logger.info(f"[DY-SKILL] 合集视频 {video_id} 已存在，跳过")

                            # 更新进度（跳过视频也要更新进度）
                            if progress_callback:
                                current = downloaded_count + skipped_count
                                progress_callback(
                                    current,
                                    estimated_total,
                                    f"跳过: {video_desc[:30]}...",
                                )
                            continue

                        to_download.append(v)

                    if to_download:
                        downloader = handler.downloader
                        # 下载前将 timeout 设置为较大的值，避免大视频下载超时
                        kwargs["timeout"] = 60
                        # 安装 F2 进度桥，捕获文件级中间进度（被并发任务占用时放弃）
                        bridge = None
                        if file_progress_callback:
                            from .f2_progress import F2ProgressBridge

                            bridge = F2ProgressBridge()
                            if not bridge.install(file_progress_callback):
                                bridge = None
                        try:
                            await downloader.create_download_tasks(
                                kwargs, to_download, user_path
                            )
                        finally:
                            if bridge is not None:
                                bridge.uninstall()
                        # 恢复为较小的值，加快下一页获取速度
                        kwargs["timeout"] = 2

                        for v in to_download:
                            v_serialized = self._serialize_for_sqlite(v)
                            v_filtered = await self._filter_fields_for_sqlite(
                                "video_info", v_serialized
                            )
                            await vdb.add_video_info(
                                ignore_fields=["images_video"], **v_filtered
                            )
                            downloaded_count += 1

                            # 更新进度
                            if progress_callback:
                                video_desc = v.get("desc", "")[:30]
                                current = downloaded_count + skipped_count
                                progress_callback(
                                    current,
                                    estimated_total,
                                    f"正在下载: {video_desc}...",
                                )

            result["success"] = True
            result["downloaded"] = downloaded_count
            result["skipped"] = skipped_count
            result["download_path"] = str(user_path)

            # 最终进度更新为100%
            if progress_callback:
                total_final = downloaded_count + skipped_count
                progress_callback(total_final, total_final, "下载完成")

            # 检查是否达到了下载限制
            total_processed = downloaded_count + skipped_count
            if max_counts and total_processed >= max_counts:
                result["message"] = (
                    f"本批次下载完成: 成功 {downloaded_count}, 跳过 {skipped_count} "
                    f"(已限制最多 {max_counts} 个视频，合集可能还有更多视频)"
                )
                result["has_more"] = True
                result["batch_info"] = {
                    "processed": total_processed,
                    "limit": max_counts,
                    "message": "本合集视频数量超过限制，建议分批下载或使用合集ID直接下载剩余部分",
                }
            else:
                result["message"] = (
                    f"下载完成: 成功 {downloaded_count}, 跳过 {skipped_count}"
                    f"{' (全量下载)' if not max_counts else ''}"
                )
                result["has_more"] = False

            logger.info(f"[DY-SKILL] 合集 {mix_id} 下载完成: {result['message']}")

        except Exception as e:
            result["message"] = f"合集下载失败: {str(e)}"
            logger.error(f"[DY-SKILL] {result['message']}")

        return result

    async def download_mix_videos(
        self, user_home_url: str, mix_id: str
    ) -> Dict[str, Any]:
        """
        下载指定用户的指定合集视频（保留原有接口兼容性）

        Args:
            user_home_url: 用户主页链接（用于获取用户信息）
            mix_id: 合集ID

        Returns:
            下载结果状态
        """
        try:
            cookie = await self.get_cookie()
            kwargs = self._build_kwargs({"cookie": cookie})
            handler = self._get_handler(kwargs)

            sec_user_id = await SecUserIdFetcher.get_sec_user_id(user_home_url)
            user_profile = await handler.fetch_user_profile(sec_user_id)
            user_nickname = user_profile.nickname

            return await self.download_mix_by_id(mix_id, user_nickname)

        except Exception as e:
            return {"success": False, "message": f"下载失败: {str(e)}"}

    async def download_mixes_by_ids(
        self, mix_ids_input: str, mix_name: str = ""
    ) -> Dict[str, Any]:
        """
        根据批量合集ID下载合集视频
        支持使用空格、逗号、换行、分号等分隔符输入多个合集ID

        Args:
            mix_ids_input: 合集ID列表字符串，支持多种分隔符
            mix_name: 合集名称（可选，用于所有合集的目录命名前缀）

        Returns:
            包含每个合集下载结果的字典
        """
        import re

        # 解析多个合集ID（支持空格、逗号、换行、分号等分隔符）
        mix_ids = re.split(r"[,;\s]+", mix_ids_input.strip())
        mix_ids = [mid.strip() for mid in mix_ids if mid.strip()]

        if not mix_ids:
            return {"success": False, "message": "未解析到有效的合集ID", "results": []}

        results = []
        total_downloaded = 0
        total_skipped = 0

        for idx, mix_id in enumerate(mix_ids, 1):
            logger.info(f"[DY-SKILL] 开始下载第 {idx}/{len(mix_ids)} 个合集: {mix_id}")

            # 为每个合集单独调用下载方法
            mix_result = await self.download_mix_by_id(mix_id, mix_name)
            results.append(
                {
                    "mix_id": mix_id,
                    "success": mix_result.get("success", False),
                    "downloaded": mix_result.get("downloaded", 0),
                    "skipped": mix_result.get("skipped", 0),
                    "message": mix_result.get("message", ""),
                }
            )

            if mix_result.get("success"):
                total_downloaded += mix_result.get("downloaded", 0)
                total_skipped += mix_result.get("skipped", 0)

        success_count = sum(1 for r in results if r["success"])

        return {
            "success": success_count == len(mix_ids),
            "message": f"批量下载完成: {success_count}/{len(mix_ids)} 个合集成功, 共下载 {total_downloaded} 个视频, 跳过 {total_skipped} 个",
            "total_mixes": len(mix_ids),
            "success_count": success_count,
            "total_downloaded": total_downloaded,
            "total_skipped": total_skipped,
            "results": results,
        }


    async def download_user_posts(
        self,
        user_url: str,
        progress_callback: Optional[Callable] = None,
        file_progress_callback: Optional[Callable[[int, str], None]] = None,
        max_counts: Optional[int] = None,
        interval: str = "all",
    ) -> Dict[str, Any]:
        """
        下载用户发布的全部作品

        Args:
            user_url: 用户主页链接
            progress_callback: 进度回调函数(current: int, total: int, current_video: str)
            file_progress_callback: 文件级进度回调函数(percentage: int, current_file: str)
            max_counts: 最大下载数量，None表示全量下载
            interval: 日期区间，格式如 "2024-01-01|2024-12-31" 或 "all"

        Returns:
            下载结果状态，包含videos列表
        """
        result = {"success": False, "downloaded": 0, "skipped": 0, "message": "", "videos": []}

        try:
            cookie = await self.get_cookie()
            kwargs = self._build_kwargs({"cookie": cookie})

            # 为列表获取设置较小的分页间隔（2秒），提高速度
            # 下载时会临时修改为60秒
            kwargs["timeout"] = 2

            # Preserve the request-scoped date interval instead of replacing it
            # with the config default.
            kwargs["interval"] = interval or "all"

            handler = self._get_handler(kwargs)

            # 从URL提取sec_user_id
            sec_user_id = await SecUserIdFetcher.get_sec_user_id(user_url)
            logger.info(f"[DY-SKILL] 获取到sec_user_id: {sec_user_id}")

            # 获取用户信息
            user_profile = await handler.fetch_user_profile(sec_user_id)
            user_nickname = user_profile.nickname
            logger.info(f"[DY-SKILL] 用户信息: {user_nickname}")

            # 设置用户目录路径
            safe_nickname = _safe_directory_component(user_nickname, "用户")
            user_path = Path(kwargs["path"]) / f"{safe_nickname}_{sec_user_id[:8]}"
            user_path.mkdir(parents=True, exist_ok=True)

            downloaded_count = 0
            skipped_count = 0
            page_count = 0
            has_more_pages = True
            estimated_total = max_counts if max_counts else 100  # 初始估计值

            # 通知开始
            if progress_callback:
                total_hint = f"最多 {max_counts} 个" if max_counts else "全量"
                progress_callback(0, 100, f"准备下载用户作品 ({total_hint})...")

            # 使用F2的fetch_user_post_videos获取用户作品
            async for page in handler.fetch_user_post_videos(
                sec_user_id=sec_user_id,
                page_counts=20,
                max_counts=max_counts,
            ):
                video_list = page._to_list()
                page_count += 1

                # 检查是否还有更多页
                has_more_pages = getattr(page, "has_more", False)

                if not video_list:
                    if not has_more_pages:
                        break
                    continue

                # 更新估计总数
                current_processed = downloaded_count + skipped_count
                if has_more_pages and not max_counts:
                    # 根据已处理的页数估算总数（每页约10-20个视频）
                    estimated_total = max(current_processed + len(video_list) * 2, 100)
                else:
                    estimated_total = max(
                        current_processed + len(video_list), estimated_total
)

                # 去重检查
                async with AsyncVideoDB("douyin_videos.db") as vdb:
                    to_download = []

                    for v in video_list:
                        video_desc = v.get("desc", "")
                        video_id = str(v.get("aweme_id"))

                        video_info = await vdb.get_video_info(video_id)
                        file_exists = self._is_video_file_exists(
                            v, user_path, kwargs.get("naming", "{aweme_id}_{desc}")
)

                        if video_info and file_exists:
                            skipped_count += 1
                            logger.info(f"[DY-SKILL] 用户作品 {video_id} 已存在，跳过")

                            # 收集跳过的视频信息
                            result["videos"].append({
                                "aweme_id": str(v.get("aweme_id", "")),
                                "desc": v.get("desc", ""),
                                "nickname": user_nickname,
                                "sec_user_id": sec_user_id,
                                "create_time": v.get("create_time", datetime.now().isoformat()),
                                "status": "skipped"
})

                            # 更新进度（跳过视频也要更新进度）
                            if progress_callback:
                                current = downloaded_count + skipped_count
                                progress_callback(
                                    current,
                                    estimated_total,
                                    f"跳过: {video_desc[:30]}...",
)
                            continue

                        to_download.append(v)

                    if to_download:
                        downloader = handler.downloader
                        # 下载前将 timeout 设置为较大的值，避免大视频下载超时
                        kwargs["timeout"] = 60
                        # 安装 F2 进度桥，捕获文件级中间进度（被并发任务占用时放弃）
                        bridge = None
                        if file_progress_callback:
                            from .f2_progress import F2ProgressBridge

                            bridge = F2ProgressBridge()
                            if not bridge.install(file_progress_callback):
                                bridge = None
                        try:
                            await downloader.create_download_tasks(
                                kwargs, to_download, user_path
                            )
                        finally:
                            if bridge is not None:
                                bridge.uninstall()
                        # 恢复为较小的值，加快下一页获取速度
                        kwargs["timeout"] = 2

                        for v in to_download:
                            v_serialized = self._serialize_for_sqlite(v)
                            v_filtered = await self._filter_fields_for_sqlite(
                                "video_info", v_serialized
)
                            await vdb.add_video_info(
                                ignore_fields=["images_video"], **v_filtered
)
                            downloaded_count += 1

                            # 收集下载的视频信息
                            result["videos"].append({
                                "aweme_id": str(v.get("aweme_id", "")),
                                "desc": v.get("desc", ""),
                                "nickname": user_nickname,
                                "sec_user_id": sec_user_id,
                                "create_time": v.get("create_time", datetime.now().isoformat()),
                                "status": "downloaded"
})

                            # 更新进度
                            if progress_callback:
                                video_desc = v.get("desc", "")[:30]
                                current = downloaded_count + skipped_count
                                progress_callback(
                                    current,
                                    estimated_total,
                                    f"正在下载: {video_desc}...",
)

            result["success"] = True
            result["downloaded"] = downloaded_count
            result["skipped"] = skipped_count
            result["download_path"] = str(user_path)

            # 最终进度更新为100%
            if progress_callback:
                total_final = downloaded_count + skipped_count
                progress_callback(total_final, total_final, "下载完成")

            # 检查是否达到了下载限制
            total_processed = downloaded_count + skipped_count
            if max_counts and total_processed >= max_counts:
                result["message"] = (
                    f"本批次下载完成: 成功 {downloaded_count}, 跳过 {skipped_count} "
                    f"(已限制最多 {max_counts} 个视频，用户可能还有更多作品)"
)
                result["has_more"] = True
                result["batch_info"] = {
                    "processed": total_processed,
                    "limit": max_counts,
                    "message": "该用户作品数量超过限制，建议分批下载",
}
            else:
                result["message"] = (
                    f"下载完成: 成功 {downloaded_count}, 跳过 {skipped_count} "
                    f"用户: {user_nickname}"
)
                result["has_more"] = False

            logger.info(f"[DY-SKILL] 用户 {user_nickname} 作品下载完成: {result['message']}")

        except Exception as e:
            result["message"] = f"用户作品下载失败: {str(e)}"
            logger.error(f"[DY-SKILL] {result['message']}")

        return result
