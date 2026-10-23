"""
抖音视频下载器 Skill - Web API 服务

提供HTTP API接口供前端调用
基于FastAPI实现

启动命令:
    python -m skills.douyin_downloader.web --port 8080
    python skills/douyin_downloader/web.py --port 8080
"""

import sys
import os
import asyncio
import argparse
import re
import time
import uuid
import ipaddress
import subprocess
import socket
from functools import lru_cache
from typing import Any, Dict, Optional, Coroutine, List, Callable, Literal
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, quote
from html import escape as html_escape

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 直接导入，让Python显示真实的错误
from fastapi import (
    FastAPI,
    HTTPException,
    BackgroundTasks,
    Request,
    Header,
    WebSocket,
    WebSocketDisconnect,
    Query,
)
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from pydantic import BaseModel, ValidationError, Field, field_validator
from fastapi.exceptions import RequestValidationError
import uvicorn
import json

try:
    from .core import (
        DouyinDownloadSkill,
        clean_filename_for_display,
    )
    from .task_manager_v3 import (
        task_manager,
        TaskType,
        TaskStatus,
    )
    from .db import TaskVideoInfo
    from .scan_manager import ScanAccessError, normalize_scan_item, scan_manager
    from .transcription_service import TranscriptionService, new_job_id, serialize_error
    from .video_summary_service import VideoSummaryService, new_summary_job_id, serialize_summary_error, normalize_comment_ideas
    from .distill_service import DistillService, DistillSynthesis, DistillError, DistillMarkdown, _source_is_current, _summary_invalid_reason
    from .download_queue import PersistentDownloadQueue
    from .download_queue_api import create_download_queue_router
    from .content_service import ContentService
    from .content_api import create_content_router
    from .batch_results import BatchResults
    from .batch_results_api import create_batch_results_router
    from .product_runtime import ProductRuntime
    from .usage_ledger import UsageLedger, configure_default_ledger, model_request_context, current_request_context, ModelControlError
    from .usage_api import create_usage_router
except ImportError:
    from core import (
        DouyinDownloadSkill,
        clean_filename_for_display,
    )
    from task_manager_v3 import (
        task_manager,
        TaskType,
        TaskStatus,
    )
    from db import TaskVideoInfo
    from scan_manager import ScanAccessError, normalize_scan_item, scan_manager
    from transcription_service import TranscriptionService, new_job_id, serialize_error
    from video_summary_service import VideoSummaryService, new_summary_job_id, serialize_summary_error, normalize_comment_ideas
    from distill_service import DistillService, DistillSynthesis, DistillError, DistillMarkdown, _source_is_current, _summary_invalid_reason
    from download_queue import PersistentDownloadQueue
    from download_queue_api import create_download_queue_router
    from content_service import ContentService
    from content_api import create_content_router
    from batch_results import BatchResults
    from batch_results_api import create_batch_results_router
    from product_runtime import ProductRuntime
    from usage_ledger import UsageLedger, configure_default_ledger, model_request_context, current_request_context, ModelControlError
    from usage_api import create_usage_router

# 创建FastAPI应用
app = FastAPI(
    title="抖音视频下载器 API",
    description="基于F2框架的抖音视频下载服务",
    version="1.0.0",
)

LAN_MODE = os.getenv("DOUYIN_LAN_MODE", "0") == "1"
LAN_CIDR = os.getenv("DOUYIN_LAN_CIDR", "192.168.0.0/16")
try:
    LAN_NETWORK = ipaddress.ip_network(LAN_CIDR, strict=False)
except ValueError:
    LAN_NETWORK = ipaddress.ip_network("192.168.0.0/16")


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in {"localhost"}


def _is_allowed_lan_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host) in LAN_NETWORK
    except ValueError:
        return False


# 配置CORS
LISTEN_PORT = 8080
LAN_ORIGIN_PATTERN = r"^https?://(?:127\.0\.0\.1|localhost|10\.(?:\d{1,3}\.){2}\d{1,3}|192\.168\.(?:\d{1,3}\.)\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.(?:\d{1,3}\.)\d{1,3}):{port}$"
LAN_ORIGIN_REGEX = LAN_ORIGIN_PATTERN.replace("{port}", str(LISTEN_PORT)) if LAN_MODE else None
ALLOWED_LOCAL_ORIGINS = {f"http://127.0.0.1:{LISTEN_PORT}", f"http://localhost:{LISTEN_PORT}"}
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_LOCAL_ORIGINS),
    allow_origin_regex=LAN_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    TrustedHostMiddleware,
    # Starlette matches the hostname portion of Host; the uvicorn port is
    # already fixed by the launch command.
    allowed_hosts=["127.0.0.1", "localhost"] + (["*"] if LAN_MODE else []),
)

def configure_origin_port(port: int) -> None:
    global LISTEN_PORT, LAN_ORIGIN_REGEX, ALLOWED_LOCAL_ORIGINS
    LISTEN_PORT = port
    LAN_ORIGIN_REGEX = LAN_ORIGIN_PATTERN.replace("{port}", str(port)) if LAN_MODE else None
    ALLOWED_LOCAL_ORIGINS = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            middleware.kwargs["allow_origins"] = sorted(ALLOWED_LOCAL_ORIGINS)
            middleware.kwargs["allow_origin_regex"] = LAN_ORIGIN_REGEX
            app.middleware_stack = None
            break


@app.get("/api/connection/status")
async def connection_status():
    return ApiResponse(success=True, data={"allowed_local_origins": sorted(ALLOWED_LOCAL_ORIGINS)}, message="连接配置可用")


@app.middleware("http")
async def lan_access_guard(request: Request, call_next):
    """Allow direct access only from loopback or the configured private subnet."""
    if LAN_MODE and request.url.path.startswith("/api"):
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host) and not _is_allowed_lan_host(client_host):
            return JSONResponse(status_code=403, content={"success": False, "message": "仅允许配置的局域网访问"})
    return await call_next(request)

# 全局Skill实例
skill: Optional[DouyinDownloadSkill] = None
running_download_tasks: Dict[str, asyncio.Task] = {}
auto_summary_requested: set[str] = set()
_cookie_refresh_task: Optional[asyncio.Task] = None
_cookie_refresh_state: Dict[str, Any] = {
    "status": "idle",
    "refreshing": False,
    "message": "",
    "error": None,
}
# ponytail: one process-wide queue; add per-batch limits only if separate priorities are needed.
download_slots = asyncio.Semaphore(2)
running_transcription_tasks: Dict[str, asyncio.Task] = {}
running_summary_tasks: Dict[str, asyncio.Task] = {}
summary_batches: Dict[str, Dict[str, Any]] = {}
running_summary_batches: Dict[str, asyncio.Task] = {}
transcript_batches: Dict[str, Dict[str, Any]] = {}
running_transcript_batches: Dict[str, asyncio.Task] = {}
transcription_service = TranscriptionService()
video_summary_service = VideoSummaryService()
distill_service: Optional[DistillService] = None
download_queue: Optional[PersistentDownloadQueue] = None
content_service: Optional[ContentService] = None
batch_results: Optional[BatchResults] = None
usage_ledger: Optional[UsageLedger] = None
product_runtime: Optional[ProductRuntime] = None

INTERVAL_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})\|(\d{4}-\d{2}-\d{2})$")
MIX_ID_PATTERN = re.compile(r"^\d{5,32}$")


def _validate_douyin_url(value: str, *, require_user_page: bool = False) -> str:
    """Validate and normalize a Douyin URL without making a network request."""
    value = (value or "").strip()
    if not value or len(value) > 2048:
        raise ValueError("请输入有效的抖音链接")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed_host = host == "douyin.com" or host.endswith(".douyin.com")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("抖音链接端口无效") from exc
    allowed_port = port is None or (
        (parsed.scheme == "http" and port == 80)
        or (parsed.scheme == "https" and port == 443)
    )
    if parsed.scheme not in {"http", "https"} or not allowed_host:
        raise ValueError("仅支持 douyin.com 的 http/https 链接")
    if parsed.username or parsed.password or not allowed_port:
        raise ValueError("抖音链接不能包含登录凭据或非标准端口")
    if require_user_page and host != "v.douyin.com" and "/user/" not in parsed.path:
        raise ValueError("请输入抖音用户主页链接")
    return value


def _validate_interval(value: str) -> str:
    value = (value or "all").strip()
    if value == "all":
        return value
    match = INTERVAL_PATTERN.fullmatch(value)
    if not match:
        raise ValueError("日期区间格式应为 YYYY-MM-DD|YYYY-MM-DD")
    try:
        start = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        end = datetime.strptime(match.group(2), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("日期区间包含无效日期") from exc
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    return value


def _validate_display_name(value: str) -> str:
    value = (value or "").strip()
    if len(value) > 120:
        raise ValueError("名称不能超过 120 个字符")
    if re.search(r"[/\\\x00-\x1f]", value) or ".." in value:
        raise ValueError("名称不能包含路径分隔符、控制字符或连续点号")
    return value


def _task_error(stage: str, code: str, reason: str, action: str) -> str:
    """Serialize a stable, user-facing task error contract for SQLite storage."""
    return json.dumps(
        {"stage": stage, "code": code, "reason": reason, "action": action},
        ensure_ascii=False,
    )


# Douyin may temporarily reject a media request while metadata and the login
# session are still valid. Keep this policy at the web-task boundary so every
# entry point that downloads one or more videos gets the same behavior without
# changing the F2 downloader internals.
try:
    DOWNLOAD_AUTO_RETRY_MAX = max(0, int(os.getenv("DOUYIN_DOWNLOAD_AUTO_RETRIES", "3")))
except ValueError:
    DOWNLOAD_AUTO_RETRY_MAX = 3
try:
    DOWNLOAD_AUTO_RETRY_DELAY = max(0.0, float(os.getenv("DOUYIN_DOWNLOAD_AUTO_RETRY_DELAY", "10")))
except ValueError:
    DOWNLOAD_AUTO_RETRY_DELAY = 10.0
DOWNLOAD_AUTO_RETRY_STATUS_CODES = {403, 408, 429, 500, 502, 503, 504}


def _download_status_code(value: Any) -> Optional[int]:
    """Extract an HTTP status code from a result or exception without leaking payloads."""
    if isinstance(value, dict):
        direct = value.get("status_code")
        if isinstance(direct, int):
            return direct
        if isinstance(direct, str) and direct.isdigit():
            return int(direct)
        for key in ("message", "error", "reason"):
            nested = _download_status_code(value.get(key))
            if nested is not None:
                return nested
        return None

    direct = getattr(value, "status_code", None)
    if isinstance(direct, int):
        return direct
    text = str(value or "")
    match = re.search(r"(?:status\s*code|http)\s*[:=]?\s*(\d{3})", text, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _is_download_auto_retryable(value: Any) -> bool:
    status_code = _download_status_code(value)
    return status_code in DOWNLOAD_AUTO_RETRY_STATUS_CODES


async def _download_with_auto_retry(
    operation: Callable[[], Any],
    *,
    task_id: str,
    progress_percentage: int,
    label: str,
    pause_on_rejection: bool = False,
) -> Dict[str, Any]:
    """Run one download operation with a bounded, user-visible anti-risk retry policy."""
    attempts = DOWNLOAD_AUTO_RETRY_MAX + 1
    last_result: Dict[str, Any] = {
        "success": False,
        "message": "下载失败",
    }
    retries_performed = 0

    for attempt in range(attempts):
        try:
            result = await operation()
            last_result = result if isinstance(result, dict) else {
                "success": bool(result),
                "message": str(result),
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # F2 normally returns a result dict, but preserving status_code here
            # also handles adapters that raise an HTTP-like exception directly.
            last_result = {
                "success": False,
                "status_code": getattr(exc, "status_code", None),
                "message": str(exc) or type(exc).__name__,
            }

        if last_result.get("success") or not _is_download_auto_retryable(last_result):
            break
        if pause_on_rejection and _download_status_code(last_result) in {401, 403, 429}:
            break
        if attempt >= attempts - 1:
            break

        status_code = _download_status_code(last_result) or 403
        retry_number = attempt + 1
        delay = DOWNLOAD_AUTO_RETRY_DELAY * (2 ** attempt if pause_on_rejection else 1)
        delay_text = f"{delay:g} 秒"
        await _update_task_progress(
            task_id,
            progress_percentage,
            f"{label}：HTTP {status_code}，请求被拒绝或暂不可用，等待 {delay_text} 后自动重试（{retry_number}/{DOWNLOAD_AUTO_RETRY_MAX}）",
        )
        retries_performed = retry_number
        await asyncio.sleep(delay)

    if not last_result.get("success") and _is_download_auto_retryable(last_result) and retries_performed:
        status_code = _download_status_code(last_result) or 403
        last_result = dict(last_result)
        last_result["message"] = (
            f"{last_result.get('message') or f'HTTP {status_code}'}；"
            f"系统已自动重试 {retries_performed} 次仍未成功"
        )
        last_result["status_code"] = status_code
    return last_result


def _cookie_state() -> Dict[str, bool]:
    """Return minimal cookie state; never return cookie content or length."""
    try:
        from .cookie_manager import load_local_cookie, is_cookie_logged_in
    except ImportError:
        from cookie_manager import load_local_cookie, is_cookie_logged_in

    cookie = load_local_cookie()
    return {
        "exists": bool(cookie),
        "valid": bool(cookie and is_cookie_logged_in(cookie)),
    }


def _require_valid_cookie() -> None:
    state = _cookie_state()
    if not state["valid"]:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "COOKIE_REQUIRED",
                "message": "登录状态无效，请先在 Cookie 管理中扫码登录",
                "action": "刷新 Cookie 后重试",
            },
        )


def _check_download_queue_config(auto_summary: bool) -> None:
    """入队只检查本地配置，不预取视频详情或下载媒体。"""
    if skill is None:
        raise HTTPException(status_code=503, detail="下载服务尚未初始化")
    _require_valid_cookie()
    if auto_summary:
        try:
            video_summary_service.validate_config()
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail={"code": "SUMMARY_CONFIG_INVALID", "message": "视频总结模型配置不完整", "action": "检查模型服务地址、模型和 API Key 配置后重试"}) from exc


async def _prepare_queued_download(aweme_id: str, author: str, profile_id: str, *, local_only: bool = False) -> Optional[str]:
    """开始处理单条时才创建任务；按作品编号复用已有记录。"""
    rows = await task_manager.db.fetch_all(
        "SELECT t.task_id, t.type, v.status FROM tasks t JOIN task_videos v ON v.task_id=t.task_id "
        "WHERE t.type IN ('single','batch_item','post','mix','mix_user') AND v.aweme_id=? "
        "ORDER BY CASE WHEN EXISTS (SELECT 1 FROM video_summary_jobs j WHERE j.task_id=t.task_id "
        "AND j.status='completed' AND NULLIF(j.summary_json,'') IS NOT NULL) THEN 0 "
        "WHEN EXISTS (SELECT 1 FROM distill_items d WHERE d.task_id=t.task_id "
        "AND NULLIF(d.summary_json,'') IS NOT NULL) THEN 0 "
        "WHEN EXISTS (SELECT 1 FROM video_summary_jobs j WHERE j.task_id=t.task_id "
        "AND j.status IN ('queued','running')) THEN 1 ELSE 2 END, "
        "CASE t.status WHEN 'completed' THEN 0 WHEN 'downloading' THEN 1 "
        "WHEN 'pending' THEN 2 ELSE 3 END, t.created_at DESC",
        (aweme_id,),
    )
    conflict = None
    busy_parent = False
    for task_id, task_type, video_status in rows:
        task = await task_manager.get_task(task_id)
        if not task:
            continue
        handle = running_download_tasks.get(task_id)
        active = task.status == "downloading" or (handle is not None and not handle.done())
        if active and task_type in {"single", "batch_item"}:
            continue
        if active and video_status != "downloaded":
            busy_parent = True
            continue
        try:
            path = await _resolve_task_video_path(task, "复用下载", aweme_id=aweme_id, require_completed=False)
        except HTTPException as exc:
            if exc.status_code == 404:
                continue
            if exc.status_code == 409:
                conflict = exc
                continue
            raise
        if task_type in {"post", "mix", "mix_user"}:
            # 旧批量记录只创建隐藏引用，不重复展示一张单视频卡片。
            await task_manager.db.update_video_status(task_id, aweme_id, "downloaded", file_path=str(path), file_size=path.stat().st_size)
            return await _materialize_batch_video_task_reference(f"{task_id}::{aweme_id}")
        return task_id
    if conflict:
        raise conflict
    if busy_parent:
        raise HTTPException(status_code=409, detail={
            "code": "DOWNLOAD_QUEUE_PARENT_BUSY", "message": "该作品仍在旧批量任务中下载，未重复提交",
            "action": "等待旧批量任务结束后继续队列",
        })
    if local_only:
        # 预检不能创建空任务、读取 Cookie 或请求抖音；无记录时只查本地孤立文件。
        if rows or skill is None:
            return None
        root = Path(skill.douyin_conf.get("path", "Download")).expanduser().resolve()
        try:
            await asyncio.to_thread(_find_task_video_file, root, [], [aweme_id])
        except (FileNotFoundError, RuntimeError):
            return None
    for task_id, task_type, _ in rows:
        if task_type in {"single", "batch_item"}:
            return task_id
    if skill is None:
        raise RuntimeError("下载服务尚未初始化")
    task = await task_manager.create_single_task(
        aweme_id=aweme_id,
        video_info={"aweme_id": aweme_id, "desc": f"视频 {aweme_id}", "nickname": author, "sec_user_id": profile_id or None},
        download_path=skill.douyin_conf.get("path", "Download"),
    )
    return task.id


async def _try_reuse_queued_download(task: Any, aweme_id: str, auto_summary: bool) -> Optional[Dict[str, Any]]:
    """只复用本地文件，可提交独立总结作业，绝不发起抖音下载。"""
    task_id = task.id
    running = running_download_tasks.get(task_id)
    if task.status != "downloading" and (running is None or running.done()):
        try:
            path = await _resolve_task_video_path(task, "复用下载", aweme_id=aweme_id, require_completed=False)
        except HTTPException as exc:
            if exc.status_code != 404:
                return {"status": "failed", "task_id": task_id, "error": {
                    "code": "VIDEO_FILE_AMBIGUOUS", "reason": "本地文件无法唯一匹配，已暂停以免重复下载",
                    "action": "在任务记录中补充精确文件路径，或保留唯一视频文件后继续队列",
                }}
        else:
            await task_manager.db.update_video_status(task_id, aweme_id, "downloaded", file_path=str(path), file_size=path.stat().st_size)
            await task_manager.db.update_task_counts(task_id, total=1, downloaded=1, skipped=0, failed=0)
            await task_manager.db.update_task_progress(task_id, percentage=100, current_video_name="已复用本地视频，跳过下载")
            if task.status != "completed":
                await task_manager.update_task_status(task_id, "completed")
            if auto_summary:
                await _start_auto_video_summary(task_id)
            return {"status": "completed", "task_id": task_id, "skipped": True}
    return None


async def _preflight_queued_download(aweme_id: str, author: str, profile_id: str, auto_summary: bool) -> Optional[str]:
    """冷却期间提前查重；没有确定的本地文件时留待正常出队处理。"""
    try:
        task_id = await _prepare_queued_download(aweme_id, author, profile_id, local_only=True)
        task = await task_manager.get_task(task_id) if task_id else None
        result = await _try_reuse_queued_download(task, aweme_id, auto_summary) if task else None
    except HTTPException:
        return None
    return task_id if result and result.get("skipped") is True else None


async def _execute_queued_download(task_id: str, aweme_id: str, auto_summary: bool) -> Dict[str, Any]:
    """复用现有下载与自动总结；下载完成不等待模型分析结束。"""
    task = await task_manager.get_task(task_id)
    if task is None:
        return {"status": "missing", "task_id": task_id}
    reused = await _try_reuse_queued_download(task, aweme_id, auto_summary)
    if reused is not None:
        return reused
    try:
        _check_download_queue_config(auto_summary)
    except HTTPException as exc:
        cookie_missing = exc.status_code == 401
        error = {"code": "COOKIE_REQUIRED" if cookie_missing else "DOWNLOAD_QUEUE_CONFIG_INVALID",
                 "reason": "抖音登录状态无效" if cookie_missing else "下载或视频总结配置未就绪",
                 "action": "请在看板点击扫码登录，再继续队列" if cookie_missing else "检查本地下载与视频总结配置后继续队列"}
        await _mark_task_failed(task_id, stage="download", aweme_id=aweme_id, **error)
        return {"status": "failed", "task_id": task_id, "error": error}

    running = running_download_tasks.get(task_id)
    if running is None or running.done():
        if task.status == "completed":
            # 本地媒体已缺失时复用记录重新下载，不沿用上次的 100% 进度。
            await task_manager.db.update_task_counts(task_id, total=1, downloaded=0, skipped=0, failed=0)
            await task_manager.db.update_task_progress(task_id, percentage=0, current_video_name="等待重新下载缺失文件")
        elif task.status in {"failed", "cancelled"}:
            await task_manager.retry_task(task_id)
        _schedule_download(task_id, _download_video_task(
            task_id, aweme_id, task.base_path or skill.douyin_conf.get("path", "Download"),
            auto_summary=auto_summary, pause_on_rejection=True,
        ))
        running = running_download_tasks[task_id]
    elif auto_summary:
        auto_summary_requested.add(task_id)
    # 队列停机时先保存恢复位置；具体下载由统一关闭流程处理。
    try:
        await asyncio.shield(running)
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
    task = await task_manager.get_task(task_id)
    status = task.status if task else "failed"
    error = None
    if status != "completed":
        blocked = bool(task and task.error and _download_status_code(task.error) in {401, 403, 429})
        error = {"code": "DOWNLOAD_QUEUE_BLOCKED" if blocked else "DOWNLOAD_QUEUE_ITEM_FAILED",
                 "reason": "下载请求被拒绝或限速，已暂停整批" if blocked else "该视频下载未完成，已暂停整批",
                 "action": "检查登录状态，必要时扫码登录；等待后再继续，暂勿批量重试" if blocked else "在任务卡片查看错误，处理后点击继续队列重试该项"}
    return {"status": status, "task_id": task_id, "error": error}


app.include_router(create_download_queue_router(lambda: download_queue, _check_download_queue_config))
app.include_router(create_content_router(lambda: content_service))
app.include_router(create_usage_router(lambda: usage_ledger))
app.include_router(create_batch_results_router(lambda: batch_results,
    lambda kind, batch_id: product_runtime.resume(kind, batch_id),
    lambda kind, batch_id, task_id: product_runtime.retry(kind, batch_id, task_id)))


def _schedule_download(task_id: str, coroutine: Coroutine[Any, Any, None]) -> None:
    """Track a download coroutine so cancellation stops work instead of only changing UI state."""
    existing = running_download_tasks.get(task_id)
    if existing and not existing.done():
        coroutine.close()
        raise HTTPException(status_code=409, detail="任务正在执行")

    async def runner() -> None:
        started = False
        try:
            async with download_slots:
                started = True
                await coroutine
        except asyncio.CancelledError:
            if not started:
                coroutine.close()
            await task_manager.update_task_status(
                task_id,
                "cancelled",
                _task_error(
                    "download",
                    "TASK_CANCELLED",
                    "任务已由用户取消",
                    "可点击“继续未完成项”恢复任务",
                ),
            )
            raise
        finally:
            running_download_tasks.pop(task_id, None)

    running_download_tasks[task_id] = asyncio.create_task(runner())


async def _update_task_progress(task_id: str, percentage: int, message: str):
    """更新任务下载进度（中间状态）并广播"""
    try:
        await task_manager.update_task_progress(task_id, percentage, message)
    except Exception as e:
        print(f"[DY-WEB] 更新任务进度失败: {task_id[:8]}..., 错误: {e}")


async def _mark_task_failed(
    task_id: str,
    *,
    stage: str,
    code: str,
    reason: str,
    action: str,
    aweme_id: Optional[str] = None,
) -> None:
    """Persist structured task and item-level failure details."""
    error_payload = _task_error(stage, code, reason, action)
    if aweme_id:
        await task_manager.update_video_status(
            task_id, aweme_id, "failed", error_message=error_payload
        )
    await task_manager.db.update_task_counts(task_id, failed=1)
    await task_manager.update_task_status(task_id, "failed", error_payload)


async def _start_auto_video_summary(task_id: str) -> None:
    try:
        await create_video_summary(task_id, SummaryCreateRequest())
    except Exception as exc:
        try:
            existing = await task_manager.db.get_video_summary_job(task_id=task_id)
            if not existing:
                job_id = new_summary_job_id()
                await task_manager.db.create_video_summary_job(job_id, task_id, video_summary_service.config.gateway_model, 0)
                await task_manager.db.update_video_summary_job(
                    job_id,
                    status="failed",
                    stage="failed",
                    error_json=json.dumps({
                        "stage": "preparing_video",
                        "code": "SUMMARY_AUTO_START_FAILED",
                        "reason": "下载已完成，但自动视频总结未能启动",
                        "action": "在下载记录中点击视频总结重试",
                    }, ensure_ascii=False),
                )
        except Exception as record_exc:
            print(f"[DY-WEB] 自动总结错误记录失败: task_id={task_id[:8]}..., 类型={type(record_exc).__name__}")
        print(f"[DY-WEB] 自动视频总结启动失败: task_id={task_id[:8]}..., 类型={type(exc).__name__}")


async def _download_video_task(task_id: str, aweme_id: str, download_path: str, auto_summary: bool = False, pause_on_rejection: bool = False):
    """后台下载视频任务"""
    try:
        if skill is None:
            print(f"[DY-WEB] Skill未初始化，无法下载")
            await _mark_task_failed(
                task_id,
                stage="initialize",
                code="SKILL_NOT_READY",
                reason="下载服务尚未初始化",
                action="稍后刷新页面并重试",
                aweme_id=aweme_id,
            )
            return

        print(f"[DY-WEB] 开始后台下载任务: {task_id[:8]}..., aweme_id: {aweme_id}")

        # 更新任务状态为downloading
        await task_manager.update_task_status(task_id, "downloading")

        # 定义文件级进度回调（来自 F2 进度桥，显示字节级下载进度）
        def file_progress_callback(percentage: int, current_file: str):
            try:
                asyncio.create_task(
                    _update_task_progress(task_id, percentage, current_file)
                )
            except RuntimeError:
                pass  # 事件循环已关闭，忽略进度更新

        # 调用skill下载视频
        result = await _download_with_auto_retry(
            lambda: skill.download_single_video(
                video_url=f"https://www.douyin.com/video/{aweme_id}",
                file_progress_callback=file_progress_callback,
                download_path=download_path,
            ),
            task_id=task_id,
            progress_percentage=0,
            label="单视频下载",
            pause_on_rejection=pause_on_rejection,
        )

        # 更新任务结果
        if result.get("success"):
            # 更新视频信息和状态
            video_name = result.get("video_name", "") or result.get("original_desc", "")
            if video_name:
                # 更新数据库中的视频desc
                await task_manager.db.update_video_status(
                    task_id=task_id,
                    aweme_id=aweme_id,
                    status="downloaded",
                    file_path=result.get("file_name"),
                )
                # 更新视频desc（标题）
                await task_manager.db.execute(
                    "UPDATE task_videos SET desc = ? WHERE task_id = ? AND aweme_id = ?",
                    (video_name, task_id, aweme_id),
                )
                await task_manager.db.commit()
                print(f"[DY-WEB] 更新视频标题: {video_name[:50]}...")

            await task_manager.db.update_task_counts(
                task_id, total=1, downloaded=1, skipped=0, failed=0
            )
            await task_manager.update_task_status(task_id, "completed")
            print(f"[DY-WEB] 下载成功: {task_id[:8]}...")
            if auto_summary or task_id in auto_summary_requested:
                await _start_auto_video_summary(task_id)
        else:
            await _mark_task_failed(
                task_id,
                stage="download",
                code="SINGLE_DOWNLOAD_FAILED",
                reason=result.get("message", "单视频下载失败"),
                action="检查链接与登录状态后重试失败项",
                aweme_id=aweme_id,
            )
            print(f"[DY-WEB] 下载失败: {task_id[:8]}...")

    except Exception as e:
        print(f"[DY-WEB] 下载任务异常: {task_id[:8]}..., 错误: {e}")
        await _mark_task_failed(
            task_id,
            stage="download",
            code="SINGLE_DOWNLOAD_EXCEPTION",
            reason=str(e) or type(e).__name__,
            action="查看网络与登录状态后重试失败项",
            aweme_id=aweme_id,
        )
    finally:
        auto_summary_requested.discard(task_id)


async def _download_mix_task(
    task_id: str,
    mix_id: str,
    mix_name: str,
    download_path: str,
    max_counts: Optional[int] = None,
    interval: str = "all",
):
    """后台下载合集任务"""
    try:
        if skill is None:
            print(f"[DY-WEB] Skill未初始化，无法下载合集")
            await _mark_task_failed(
                task_id,
                stage="initialize",
                code="SKILL_NOT_READY",
                reason="下载服务尚未初始化",
                action="稍后刷新页面并继续未完成项",
            )
            return

        print(f"[DY-WEB] 开始后台下载合集任务: {task_id[:8]}..., mix_id: {mix_id}")

        # 更新任务状态为downloading
        await task_manager.update_task_status(task_id, "downloading")

        # 定义进度回调函数（同步函数，避免coroutine never awaited警告）
        def progress_callback(current: int, total: int, message: str):
            try:
                pct = int(current / max(total, 1) * 100)
                asyncio.create_task(_update_task_progress(task_id, pct, message))
            except RuntimeError:
                pass  # 事件循环已关闭，忽略进度更新

        # 定义文件级进度回调（来自 F2 进度桥，显示字节级下载进度）
        def file_progress_callback(percentage: int, current_file: str):
            try:
                asyncio.create_task(
                    _update_task_progress(task_id, percentage, current_file)
                )
            except RuntimeError:
                pass  # 事件循环已关闭，忽略进度更新

        # 调用skill下载合集
        result = await skill.download_mix_by_id(
            mix_id=mix_id,
            mix_name=mix_name,
            progress_callback=progress_callback,
            file_progress_callback=file_progress_callback,
            max_counts=max_counts,
            interval=interval,
        )

        # 更新任务结果
        if result.get("success"):
            downloaded = int(result.get("downloaded", 0) or 0)
            skipped = int(result.get("skipped", 0) or 0)
            failed = int(result.get("failed", 0) or 0)
            await task_manager.db.update_task_counts(
                task_id,
                total=downloaded + skipped + failed,
                downloaded=downloaded,
                skipped=skipped,
                failed=failed,
            )
            if result.get("download_path"):
                await task_manager.db.update_task_result_path(
                    task_id, str(result["download_path"])
                )
            await task_manager.update_task_status(task_id, "completed")
            print(
                f"[DY-WEB] 合集下载成功: {task_id[:8]}..., 下载{result.get('downloaded', 0)}个, 跳过{result.get('skipped', 0)}个"
            )
        else:
            await _mark_task_failed(
                task_id,
                stage="download",
                code="MIX_DOWNLOAD_FAILED",
                reason=result.get("message", "合集下载失败"),
                action="检查合集状态后继续未完成项；已完成文件不会删除",
            )
            print(f"[DY-WEB] 合集下载失败: {task_id[:8]}...")

    except Exception as e:
        import traceback

        print(f"[DY-WEB] 合集下载任务异常: {task_id[:8]}..., 错误: {e}")
        print(traceback.format_exc())
        await _mark_task_failed(
            task_id,
            stage="download",
            code="MIX_DOWNLOAD_EXCEPTION",
            reason=str(e) or type(e).__name__,
            action="检查网络与登录状态后继续未完成项",
        )


# WebSocket连接管理器
class WebSocketManager:
    """管理WebSocket连接，处理任务状态广播"""

    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        """接受新连接"""
        await websocket.accept()
        self.connections.append(websocket)
        print(f"[WebSocket] 新连接建立，当前连接数: {len(self.connections)}")

        # Do not push an arbitrary first 100 rows: the client owns pagination/filtering.
        total = await task_manager.db.count_tasks()
        await websocket.send_json(
            {
                "type": "init",
                "data": {
                    "sync_required": True,
                    "total": total,
                },
            }
        )

    def disconnect(self, websocket: WebSocket):
        """断开连接"""
        if websocket in self.connections:
            self.connections.remove(websocket)
            print(f"[WebSocket] 连接断开，当前连接数: {len(self.connections)}")

    async def broadcast(self, message: dict):
        """广播消息给所有连接"""
        disconnected = []
        for connection in self.connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                print(f"[WebSocket] 发送消息失败: {e}")
                disconnected.append(connection)

        # 清理断开的连接
        for conn in disconnected:
            self.disconnect(conn)

    def on_task_status_change(self, task):
        """任务状态变更回调"""
        if self.connections:
            # 使用asyncio.create_task来异步广播
            message = {"type": "task_update", "data": task.to_dict()}
            asyncio.create_task(self.broadcast(message))


# 创建全局WebSocket管理器实例
ws_manager = WebSocketManager()


# 全局异常处理 - 捕获验证错误
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """处理请求验证错误，返回详细的错误信息"""
    errors = []
    for error in exc.errors():
        error_msg = {
            "field": error.get("loc", []),
            "message": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        errors.append(error_msg)

    print(f"[ERROR] Validation failed for {request.url.path}: {errors}")

    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "message": "请求数据验证失败",
            "errors": errors,
        },
    )


# 数据模型
class DownloadRequest(BaseModel):
    """下载请求模型"""

    url: str
    auto_summary: bool = False

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_douyin_url(value)

    class Config:
        json_schema_extra = {"example": {"url": "https://v.douyin.com/xxxxx"}}


class DownloadMixRequest(BaseModel):
    """下载合集请求模型

    支持单个合集ID或批量合集ID（使用空格、逗号、换行、分号等分隔符）
    """

    mix_id: str = Field(..., min_length=5, max_length=4096)
    mix_name: str = Field("", max_length=120)
    max_counts: Optional[int] = Field(
        20, ge=1, le=500, description="最大下载数量，None表示明确确认全量下载"
    )
    interval: str = Field(
        "all", description="日期区间，格式如 '2024-01-01|2024-12-31' 或 'all'"
    )

    @field_validator("mix_id")
    @classmethod
    def validate_mix_ids(cls, value: str) -> str:
        ids = [item for item in re.split(r"[,;\s]+", value.strip()) if item]
        if not ids or any(not MIX_ID_PATTERN.fullmatch(item) for item in ids):
            raise ValueError("合集 ID 仅支持 5–32 位数字，可用空格、逗号或换行分隔")
        if len(ids) > 50:
            raise ValueError("单次最多提交 50 个合集 ID")
        # Preserve order while preventing duplicate jobs within one request.
        return ",".join(dict.fromkeys(ids))

    @field_validator("mix_name")
    @classmethod
    def validate_mix_name(cls, value: str) -> str:
        return _validate_display_name(value)

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, value: str) -> str:
        return _validate_interval(value)

    class Config:
        json_schema_extra = {
            "example": {
                "mix_id": "123456, 789012",
                "mix_name": "合集名称",
                "max_counts": 20,
                "interval": "all",
            }
        }


class RefreshCookieRequest(BaseModel):
    """刷新Cookie请求模型"""

    headless: bool = False

    class Config:
        json_schema_extra = {"example": {"headless": False}}


class ApiResponse(BaseModel):
    """API响应模型"""

    success: bool
    data: Optional[Dict[str, Any]] = None
    message: str = ""


def _attachment_header(filename: str) -> str:
    """生成兼容中文文件名的 RFC 5987 Content-Disposition。"""
    # Starlette 的响应头按 latin-1 编码，不能直接放中文；filename*= 提供
    # UTF-8 文件名，ASCII fallback 兼容旧客户端，同时避免引号/换行注入。
    safe_name = re.sub(r'["\\\r\n]+', "_", str(filename or "download"))
    fallback = safe_name.encode("ascii", "ignore").decode("ascii") or "download"
    encoded = quote(safe_name, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


async def _distill_find_existing(item: Dict[str, Any]) -> tuple[Optional[str], Optional[Path]]:
    """按 aweme_id 查找可复用的已下载任务和文件。"""
    aweme_id = str(item.get("aweme_id") or "")
    row = await task_manager.db.fetch_one(
        "SELECT task_id, file_path, file_size FROM task_videos WHERE aweme_id=? AND status='downloaded' ORDER BY download_time DESC, id DESC LIMIT 1",
        (aweme_id,),
    )
    configured_root = Path(skill.douyin_conf.get("path", "Download")).expanduser().resolve() if skill else Path("Download").resolve()
    requested_root = Path(str(item.get("_save_root") or configured_root)).expanduser().resolve()
    if not row:
        return None, None
    try:
        path = None
        for root in dict.fromkeys([requested_root, configured_root]):
            try:
                path = await asyncio.to_thread(_find_task_video_file, root, [row[1]], [aweme_id])
                break
            except (FileNotFoundError, RuntimeError):
                continue
        if path is None:
            return None, None
    except (FileNotFoundError, RuntimeError):
        return None, None
    return row[0], path


async def _distill_find_reusable_snapshot(aweme_id: str) -> Optional[Dict[str, Any]]:
    """跨蒸馏运行复用仍对应当前文件的成功分析结果。"""
    rows = await task_manager.db.fetch_all(
        "SELECT run_id FROM distill_items WHERE aweme_id=? AND item_status IN ('ready','written','degraded') ORDER BY updated_at DESC LIMIT 20",
        (str(aweme_id),),
    )
    for (run_id,) in rows:
        item = await task_manager.db.get_distill_item(run_id, str(aweme_id))
        if not item or not item.get("local_path"):
            continue
        path = Path(item["local_path"])
        run = await task_manager.db.get_distill_run(run_id)
        if path.exists() and _source_is_current(item, path, (run or {}).get("completed_at") or item.get("updated_at")):
            return item
    return None


async def _distill_download_item(item: Dict[str, Any]) -> Path:
    """蒸馏专用下载回调；缺失时创建普通单视频任务并等待完成。"""
    if skill is None:
        raise DistillError("DISTILL_SKILL_NOT_READY", "下载服务尚未初始化", "稍后重试")
    task_id, existing_path = await _distill_find_existing(item)
    if existing_path and existing_path.exists():
        return existing_path
    meta = item.get("metadata") or {}
    download_path = str(Path(str(item.get("_save_root") or skill.douyin_conf.get("path", "Download"))).expanduser().resolve())
    if not task_id:
        task = await task_manager.create_single_task(
            str(item["aweme_id"]),
            {"desc": meta.get("title") or meta.get("desc") or "", "nickname": meta.get("nickname") or "", "sec_user_id": meta.get("sec_user_id"), "create_time": meta.get("published_at") or meta.get("create_time") or ""},
            download_path,
        )
        task_id = task.id
        item["task_id"] = task_id
        await task_manager.db.update_distill_item(item["run_id"], item["aweme_id"], task_id=task_id)
        await task_manager.db.update_task_status(task_id, "queued")
        _schedule_download(task_id, _download_video_task(task_id, str(item["aweme_id"]), download_path))
    for _ in range(720):
        task = await task_manager.get_task(task_id)
        if task and task.status == TaskStatus.COMPLETED.value:
            videos = await task_manager.db.get_task_videos(task_id)
            values = [video.file_path for video in videos if video.file_path]
            roots = [Path(download_path), Path(skill.douyin_conf.get("path", "Download")).expanduser().resolve()]
            for root in dict.fromkeys(roots):
                try:
                    return await asyncio.to_thread(_find_task_video_file, root, values, [str(item["aweme_id"])])
                except (FileNotFoundError, RuntimeError):
                    continue
            raise DistillError("DISTILL_VIDEO_NOT_FOUND", "下载完成但找不到本地视频文件", "检查蒸馏保存目录后重试")
        if task and task.status == TaskStatus.FAILED.value:
            if _download_status_code(task.error) == 403:
                if not _cookie_state()["valid"]:
                    raise DistillError("DISTILL_DOWNLOAD_403", "下载被拒绝（HTTP 403），登录状态已失效", "请在 Cookie 管理中重新扫码登录后继续")
                raise DistillError("DISTILL_DOWNLOAD_403", "下载被拒绝（HTTP 403），可能触发平台限速", "请等待并降低并发，暂勿继续提交")
            raise DistillError("DISTILL_DOWNLOAD_FAILED", task.error.get("reason") if task.error else "视频下载失败", "检查登录状态和网络后重试")
        await asyncio.sleep(1)
    raise DistillError("DISTILL_DOWNLOAD_TIMEOUT", "等待视频下载超时", "稍后恢复蒸馏并重试")


async def _distill_transcribe_item(job_id: str, path: Path, progress: Any) -> Dict[str, Any]:
    transcription_service.validate_config()
    return await transcription_service.run(job_id, path, progress)


async def _distill_summarize_item(job_id: str, path: Path, progress: Any) -> Dict[str, Any]:
    video_summary_service.validate_config()
    try:
        result = await video_summary_service.summarize_video(job_id, path, progress)
        result["_time_basis"] = (result.get("_processing") or {}).get("time_basis", "unknown_segment_basis")
        return result
    finally:
        video_summary_service._job_attempts.pop(job_id, None)


@app.on_event("startup")
async def startup_event():
    """服务启动时初始化Skill"""
    global skill, distill_service, download_queue, content_service, batch_results, usage_ledger, product_runtime
    skill = DouyinDownloadSkill()
    print("[DY-WEB] DouyinDownloadSkill 初始化完成")

    # 初始化任务管理器
    await task_manager.initialize()
    print("[DY-WEB] TaskManager 初始化完成")
    usage_ledger = UsageLedger(task_manager.db.db_path)
    await usage_ledger.initialize()
    await usage_ledger.recover_after_restart()
    configure_default_ledger(usage_ledger)
    batch_results = BatchResults(task_manager.db.db_path, usage_ledger)
    await batch_results.initialize()
    product_runtime = ProductRuntime(sys.modules[__name__], batch_results, usage_ledger)
    content_service = ContentService(task_manager.db, resolve_media=product_runtime.resolve_media, generator=product_runtime.generate_topic)
    await content_service.initialize()

    # 注册WebSocket状态回调
    task_manager.register_status_callback(ws_manager.on_task_status_change)
    print("[DY-WEB] WebSocket状态回调已注册")
    await scan_manager.initialize()
    print("[DY-WEB] 用户作品元数据扫描器已初始化")
    output_root = Path(skill.douyin_conf.get("path", "Download")).expanduser().resolve()
    distill_service = DistillService(
        task_manager.db,
        _distill_download_item,
        _distill_transcribe_item,
        _distill_summarize_item,
        DistillSynthesis(model="gpt-5.6-luna", timeout=float(os.getenv("DOUYIN_DISTILL_SYNTHESIS_TIMEOUT", "300"))),
        output_root,
    )
    recovered = await distill_service.recover_after_restart()
    if recovered:
        print(f"[DY-WEB] 已将 {recovered} 个蒸馏运行标记为暂停")
    download_queue = PersistentDownloadQueue(
        task_manager.db.db_path, _prepare_queued_download, _execute_queued_download,
        reuse_item=_preflight_queued_download,
    )
    await download_queue.initialize()
    await download_queue.start()


@app.on_event("shutdown")
async def shutdown_event():
    """服务关闭时清理"""
    global download_queue
    if download_queue:
        await download_queue.shutdown()
        download_queue = None
    active_downloads = list(running_download_tasks.values())
    for download_task in active_downloads:
        if not download_task.done():
            download_task.cancel()
    if active_downloads:
        await asyncio.gather(*active_downloads, return_exceptions=True)
    active_transcriptions = list(running_transcription_tasks.values())
    for transcription_task in active_transcriptions:
        if not transcription_task.done():
            transcription_task.cancel()
    if active_transcriptions:
        await asyncio.gather(*active_transcriptions, return_exceptions=True)
    active_summaries = list(running_summary_tasks.values())
    for summary_task in active_summaries:
        if not summary_task.done():
            summary_task.cancel()
    if active_summaries:
        await asyncio.gather(*active_summaries, return_exceptions=True)
    active_batches = list(running_summary_batches.values())
    for batch_task in active_batches:
        batch_task.cancel()
    if active_batches:
        await asyncio.gather(*active_batches, return_exceptions=True)
    if distill_service:
        for run_id in list(distill_service.active):
            await distill_service.cancel(run_id)

    # 注销WebSocket回调
    task_manager.unregister_status_callback(ws_manager.on_task_status_change)
    print("[DY-WEB] WebSocket状态回调已注销")
    await scan_manager.shutdown()
    if batch_results:
        await batch_results.close()
    configure_default_ledger(None)

    # 关闭任务管理器
    await task_manager.shutdown()
    print("[DY-WEB] TaskManager 已关闭")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket端点 - 实时推送任务状态"""
    client = getattr(websocket, "client", None)
    client_host = getattr(client, "host", "") if client else ""
    if LAN_MODE and not _is_loopback(client_host) and not _is_allowed_lan_host(client_host):
        await websocket.close(code=1008, reason="LAN network not allowed")
        return
    origin = getattr(websocket, "headers", {}).get("origin")
    origin_host = urlparse(origin).hostname if origin else None
    if origin and origin not in ALLOWED_LOCAL_ORIGINS and not (LAN_MODE and origin_host and (_is_loopback(origin_host) or _is_allowed_lan_host(origin_host))):
        await websocket.close(code=1008, reason="Origin 与监听端口不一致或来源不被允许")
        return
    print("[WebSocket] 新的WebSocket连接")
    await websocket.accept()
    ws_manager.connections.append(websocket)
    print(f"[WebSocket] 连接已建立，当前连接数: {len(ws_manager.connections)}")

    try:
        # The HTTP list endpoint remains the single source for pagination/filtering.
        total = await task_manager.db.count_tasks()
        await websocket.send_json(
            {
                "type": "init",
                "data": {
                    "sync_required": True,
                    "total": total,
                },
            }
        )
        print("[WebSocket] 初始数据已发送")

        while True:
            # 使用超时接收消息，如果超时则继续循环（保持连接）
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                message = json.loads(data)
                print(f"[WebSocket] 收到消息: {message.get('type')}")

                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                elif message.get("type") == "get_tasks":
                    total = await task_manager.db.count_tasks()
                    await websocket.send_json(
                        {
                            "type": "init",
                            "data": {
                                "sync_required": True,
                                "total": total,
                            },
                        }
                    )
            except asyncio.TimeoutError:
                # 超时后继续循环，不发送额外消息
                print("[WebSocket] 等待消息超时，继续保持连接")
                continue
            except Exception as e:
                print(f"[WebSocket] 处理消息时出错: {e}")
                break

    except WebSocketDisconnect:
        print(f"[WebSocket] 客户端断开连接")
    except Exception as e:
        print(f"[WebSocket] 连接错误: {e}")
    finally:
        if websocket in ws_manager.connections:
            ws_manager.connections.remove(websocket)
        print(f"[WebSocket] 连接已关闭，剩余连接数: {len(ws_manager.connections)}")


@app.get("/")
async def root():
    """根路径 - 返回服务状态"""
    return {"service": "抖音视频下载器 API", "version": "1.0.0", "status": "running"}


@app.get("/api/status")
async def get_status():
    """获取服务状态"""
    return {
        "status": "ok",
        "skill_initialized": skill is not None,
        "download_path": skill.douyin_conf.get("path", "Download") if skill else None,
        "capabilities": {"task_file_deletion": True},
    }


@app.post("/api/download", response_model=ApiResponse)
async def download_video(request: DownloadRequest):
    """
    下载单个视频（异步任务模式）

    Args:
        request: 包含视频URL的请求

    Returns:
        创建的任务信息
    """
    print(f"[DEBUG] Download request received: {request}")
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")
    _require_valid_cookie()
    if request.auto_summary:
        try:
            video_summary_service.validate_config()
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail={"code": "SUMMARY_CONFIG_INVALID", "message": str(exc), "action": "检查视频总结模型配置，或关闭自动总结后仅下载视频"}) from exc

    try:
        from f2.apps.douyin.utils import AwemeIdFetcher

        aweme_id = await AwemeIdFetcher.get_aweme_id(request.url)
        if not aweme_id:
            raise HTTPException(status_code=400, detail="无法从URL中提取视频ID")

        # 检查是否已存在该视频的任务（从数据库查询）
        print(f"[DY-WEB] 检查视频重复: aweme_id={aweme_id}")
        existing_result = await task_manager.get_tasks(
            status=None, task_type=TaskType.SINGLE, limit=100000
        )
        existing_tasks = existing_result.get("tasks", [])

        existing_task = None
        for task in existing_tasks:
            task_aweme_id = task.video_info.get("aweme_id") if task.video_info else None
            if task_aweme_id == aweme_id:
                existing_task = task
                print(f"[DY-WEB] [OK] 找到重复任务: {task.video_name}")
                break

        if existing_task:
            # 如果任务已完成，提示用户已下载
            if existing_task.status == TaskStatus.COMPLETED.value:
                if request.auto_summary:
                    await _start_auto_video_summary(existing_task.id)
                return ApiResponse(
                    success=True,
                    data={
                        "task_id": existing_task.id,
                        "status": existing_task.status,
                        "video_name": existing_task.video_name,
                        "is_duplicate": True,
                    },
                    message=f"该视频已下载过：{existing_task.video_name}",
                )
            # 如果任务还在进行中，直接返回现有任务
            elif existing_task.status in [
                TaskStatus.PENDING.value,
                TaskStatus.QUEUED.value,
                TaskStatus.DOWNLOADING.value,
            ]:
                if request.auto_summary:
                    auto_summary_requested.add(existing_task.id)
                return ApiResponse(
                    success=True,
                    data={
                        "task_id": existing_task.id,
                        "status": existing_task.status,
                        "video_name": existing_task.video_name,
                        "is_duplicate": True,
                    },
                    message=f"该视频已在下载队列中：{existing_task.video_name}",
                )

        # 获取下载路径
        download_path = (
            skill.douyin_conf.get("path", "Download") if skill else "Download"
        )

        # 预获取视频信息以设置正确的标题
        try:
            kwargs = skill._build_kwargs({"cookie": await skill.get_cookie()})
            handler = skill._get_handler(kwargs)
            video = await handler.fetch_one_video(aweme_id)
            video_data = video._to_dict()
            video_desc = video_data.get("desc", "")
            video_nickname = video_data.get("nickname", "")
            print(f"[DY-WEB] 预获取视频信息成功: {aweme_id}, desc={video_desc[:30]}...")
        except Exception as e:
            print(f"[DY-WEB] 预获取视频信息失败: {e}, 使用默认标题")
            video_desc = f"视频 {aweme_id}"
            video_nickname = ""

        # 使用新的V3接口创建任务（异步，保存视频信息到数据库）
        video_info = {
            "aweme_id": aweme_id,
            "desc": video_desc,
            "nickname": video_nickname,
        }

        task = await task_manager.create_single_task(
            aweme_id=aweme_id,
            video_info=video_info,
            download_path=download_path,
        )

        # 触发实际下载（后台异步执行）
        _schedule_download(
            task.id,
            _download_video_task(
                task_id=task.id,
                aweme_id=aweme_id,
                download_path=download_path,
                auto_summary=request.auto_summary,
            )
        )

        return ApiResponse(
            success=True,
            data={
                "task_id": task.id,
                "status": task.status,
                "message": "下载任务已创建",
            },
            message="下载任务已创建，完成后将自动生成视频总结" if request.auto_summary else "下载任务已创建，请在任务列表中查看进度",
        )
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[DY-WEB] Single download setup failed: {type(exc).__name__}")
        raise HTTPException(status_code=500, detail="下载任务创建失败，请检查链接后重试")


@app.post("/api/mix-info")
async def get_mix_info(request: DownloadRequest):
    """
    根据视频URL获取合集信息

    Args:
        request: 包含视频URL的请求

    Returns:
        合集信息
    """
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")
    _require_valid_cookie()

    try:
        result = await skill.get_mix_info_by_video_url(request.url)
        return ApiResponse(
            success=result.get("success", False),
            data=result,
            message=result.get("message", ""),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/download-mix", response_model=ApiResponse)
async def download_mix(request: DownloadMixRequest):
    """
    下载合集视频（异步任务模式）
    支持单个合集ID或批量合集ID（使用空格、逗号、换行、分号等分隔符）

    Args:
        request: 包含合集ID(s)和名称的请求

    Returns:
        创建的任务信息（单个或批量）
    """
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")
    _require_valid_cookie()

    try:
        import re

        # 解析多个合集ID
        mix_ids = re.split(r"[,;\s]+", request.mix_id.strip())
        mix_ids = [mid.strip() for mid in mix_ids if mid.strip()]

        if not mix_ids:
            raise HTTPException(status_code=400, detail="未解析到有效的合集ID")

        # 获取下载路径
        download_path = (
            skill.douyin_conf.get("path", "Download") if skill else "Download"
        )

        # 单个合集下载
        if len(mix_ids) == 1:
            mix_id = mix_ids[0]
            mix_name = request.mix_name or f"合集 {mix_id}"

            # 检查是否已存在该合集的任务（在下载中或队列中）
            print(f"[DY-WEB] 检查合集重复: mix_id={mix_id}")

            # 从数据库查询已存在的合集任务
            existing_result = await task_manager.get_tasks(
                status=None, task_type=TaskType.MIX, limit=100000
            )
            existing_tasks = existing_result.get("tasks", [])

            existing_mix_task = None
            for task in existing_tasks:
                task_mix_id = task.video_info.get("mix_id") if task.video_info else None
                if task_mix_id == mix_id and task.status in [
                    TaskStatus.PENDING.value,
                    TaskStatus.QUEUED.value,
                    TaskStatus.DOWNLOADING.value,
                ]:
                    existing_mix_task = task
                    print(f"[DY-WEB] [OK] 找到进行中的合集任务: {task.video_name}")
                    break

            if existing_mix_task:
                # 如果合集任务还在进行中，提示用户
                return ApiResponse(
                    success=True,
                    data={
                        "task_id": existing_mix_task.id,
                        "status": existing_mix_task.status,
                        "video_name": existing_mix_task.video_name,
                        "is_duplicate": True,
                    },
                    message=f"该合集已在下载队列中：{existing_mix_task.video_name}",
                )

            # 创建合集任务（异步保存到数据库）
            print(f"[DY-WEB] 创建合集任务: mix_id={mix_id}, name={mix_name}")
            task = await task_manager.create_mix_task(
                mix_id=mix_id,
                mix_name=mix_name,
                videos=None,  # 先不获取视频列表，下载时再获取
                download_path=download_path,
                max_counts=request.max_counts,
                interval=request.interval,
            )

            # 启动后台下载任务
            _schedule_download(
                task.id,
                _download_mix_task(
                    task_id=task.id,
                    mix_id=mix_id,
                    mix_name=mix_name,
                    download_path=download_path,
                    max_counts=request.max_counts,
                    interval=request.interval,
                )
            )

            return ApiResponse(
                success=True,
                data={
                    "task_id": task.id,
                    "status": task.status,
                    "video_name": task.video_name,
                    "message": "合集下载任务已创建",
                },
                message=f"合集下载任务已创建：{task.video_name}",
            )

        # 批量合集下载
        else:
            print(f"[DY-WEB] 批量下载 {len(mix_ids)} 个合集")
            skipped_mixes = []
            created_tasks = []

            # 查询已存在的合集任务
            existing_result = await task_manager.get_tasks(
                status=None, task_type=TaskType.MIX, limit=100000
            )
            existing_tasks = existing_result.get("tasks", [])

            for mix_id in mix_ids:
                mix_name = request.mix_name or f"合集 {mix_id}"

                # 检查是否已存在进行中的该合集任务
                is_existing = False
                for task in existing_tasks:
                    task_mix_id = (
                        task.video_info.get("mix_id") if task.video_info else None
                    )
                    if task_mix_id == mix_id and task.status in [
                        TaskStatus.PENDING.value,
                        TaskStatus.QUEUED.value,
                        TaskStatus.DOWNLOADING.value,
                    ]:
                        skipped_mixes.append({"mix_id": mix_id, "reason": "已在队列中"})
                        is_existing = True
                        break

                if is_existing:
                    continue

                # 创建合集任务
                task = await task_manager.create_mix_task(
                    mix_id=mix_id,
                    mix_name=mix_name,
                    videos=None,
                    download_path=download_path,
                    max_counts=request.max_counts,
                    interval=request.interval,
                )
                created_tasks.append(task)

                # 启动后台下载任务
                _schedule_download(
                    task.id,
                    _download_mix_task(
                        task_id=task.id,
                        mix_id=mix_id,
                        mix_name=mix_name,
                        download_path=download_path,
                        max_counts=request.max_counts,
                        interval=request.interval,
                    )
                )

            message_parts = []
            if created_tasks:
                message_parts.append(f"成功创建 {len(created_tasks)} 个下载任务")
            if skipped_mixes:
                message_parts.append(f"跳过 {len(skipped_mixes)} 个已在队列中的合集")

            return ApiResponse(
                success=True,
                data={
                    "task_ids": [t.id for t in created_tasks],
                    "tasks_count": len(created_tasks),
                    "skipped_count": len(skipped_mixes),
                    "skipped_mixes": skipped_mixes,
                },
                message="; ".join(message_parts)
                if message_parts
                else "批量下载任务已创建",
            )

    except HTTPException:
        raise
    except Exception as e:
        import traceback

        print(f"[DY-WEB] 下载合集错误: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/cookie/refresh")
async def refresh_cookie(request: RefreshCookieRequest):
    """Start a visible browser login without holding the HTTP request open."""
    global _cookie_refresh_task
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")

    if _cookie_refresh_task and not _cookie_refresh_task.done():
        return ApiResponse(
            success=True,
            data=_cookie_refresh_public_state(),
            message="扫码登录已在进行，请在抖音浏览器窗口完成扫码",
        )

    _cookie_refresh_state.update(
        status="starting",
        refreshing=True,
        message="正在打开抖音扫码登录窗口",
        error=None,
    )
    _cookie_refresh_task = asyncio.create_task(
        _run_cookie_refresh(skill, headless=request.headless)
    )
    return ApiResponse(
        success=True,
        data=_cookie_refresh_public_state(),
        message="正在打开抖音扫码登录窗口",
    )


def _cookie_refresh_public_state() -> Dict[str, Any]:
    return {
        "refresh_status": _cookie_refresh_state["status"],
        "refreshing": _cookie_refresh_state["refreshing"],
        "refresh_message": _cookie_refresh_state["message"],
        "refresh_error": _cookie_refresh_state["error"],
    }


async def _run_cookie_refresh(refresh_skill: DouyinDownloadSkill, *, headless: bool) -> None:
    _cookie_refresh_state.update(
        status="waiting",
        refreshing=True,
        message="登录窗口已启动，请扫码；登录完成后此页面会自动更新",
        error=None,
    )
    try:
        cookie = await refresh_skill.refresh_cookie(headless=headless)
        if not cookie:
            raise RuntimeError("登录未完成")
    except Exception as exc:
        if "BrowserType.launch" in str(exc) or "Executable doesn't exist" in str(exc):
            error = {
                "code": "COOKIE_BROWSER_LAUNCH_FAILED",
                "message": "无法启动扫码登录窗口",
                "action": "请确认已安装 Google Chrome，或 Playwright Chromium 后重试",
            }
        else:
            error = {
                "code": "COOKIE_REFRESH_FAILED",
                "message": "扫码登录未完成或登录状态未能保存",
                "action": "确认已在抖音窗口完成扫码；若窗口未打开，请检查本地服务日志后重试",
            }
        print(f"[DY-WEB] Cookie refresh failed: {type(exc).__name__}")
        _cookie_refresh_state.update(
            status="failed",
            refreshing=False,
            message=error["message"],
            error=error,
        )
    else:
        _cookie_refresh_state.update(
            status="succeeded",
            refreshing=False,
            message="扫码登录成功，登录状态已刷新",
            error=None,
        )


@app.get("/api/cookie/status")
async def check_cookie_status():
    """检查Cookie状态"""
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")

    try:
        state = {**_cookie_state(), **_cookie_refresh_public_state()}
        return ApiResponse(
            success=True,
            data=state,
            message="Cookie检查完成",
        )
    except Exception as exc:
        print(f"[DY-WEB] Cookie status check failed: {type(exc).__name__}")
        raise HTTPException(status_code=500, detail="Cookie 状态检查失败")


@app.get("/api/config/status", response_model=ApiResponse)
async def config_status():
    """Return a redacted, actionable view of model configuration."""
    try:
        from .transcription_service import KEYCHAIN_STATUS
    except ImportError:
        from transcription_service import KEYCHAIN_STATUS

    transcript_config = transcription_service.config
    summary_config = video_summary_service.config
    return ApiResponse(
        success=True,
        data={
            "transcript": {
                "base_url": transcript_config.base_url,
                "transcribe_model": transcript_config.transcribe_model,
                "polish_model": transcript_config.polish_model,
                "api_key_configured": bool(transcript_config.api_key),
                "dashscope_api_key_configured": bool(transcript_config.dashscope_api_key),
                "keychain": {
                    "transcript": KEYCHAIN_STATUS.get("douyin-downloader-llm", {"status": "not_checked"}),
                    "dashscope": KEYCHAIN_STATUS.get("douyin-downloader-dashscope", {"status": "not_checked"}),
                },
            },
            "summary": {
                "gateway": {
                    "base_url": summary_config.gateway_base_url,
                    "model": summary_config.gateway_model,
                    "api_key_configured": bool(summary_config.gateway_api_key),
                    "keychain": KEYCHAIN_STATUS.get("douyin-downloader-summary-aigateway", {"status": "not_checked"}),
                },
                "base_url": summary_config.base_url,
                "model": summary_config.model,
                "api_key_configured": bool(summary_config.api_key),
                "fallback": {
                    "base_url": summary_config.fallback_base_url,
                    "model": summary_config.fallback_model,
                    "api_key_configured": bool(summary_config.fallback_api_key),
                    "keychain": KEYCHAIN_STATUS.get("douyin-downloader-summary-fallback", {"status": "not_checked"}),
                },
                "heju": {
                    "base_url": summary_config.heju_base_url,
                    "model": summary_config.heju_model,
                    "api_key_configured": bool(summary_config.heju_api_key),
                    "keychain": KEYCHAIN_STATUS.get("douyin-downloader-summary-heju", {"status": "not_checked"}),
                },
                "keychain": KEYCHAIN_STATUS.get("douyin-downloader-text168", {"status": "not_checked"}),
            },
        },
        message="模型配置状态获取成功",
    )


@app.get("/api/ai/diagnostics", response_model=ApiResponse)
async def ai_diagnostics():
    """Return local AI prerequisites without exposing secrets or making model calls."""
    import shutil

    config = await config_status()
    data = config.data or {}
    ffmpeg_path = shutil.which("ffmpeg")
    return ApiResponse(
        success=True,
        data={
            "ffmpeg": {"ok": bool(ffmpeg_path), "path": ffmpeg_path},
            "transcript": data.get("transcript", {}),
            "summary": data.get("summary", {}),
            "network_check": "not_run",
            "note": "诊断只检查本机依赖和脱敏配置，不会上传视频或调用模型。",
        },
        message="AI 诊断信息获取成功",
    )


# ========== 任务管理 API ==========


class CreateTaskRequest(BaseModel):
    """创建任务请求"""

    type: str  # single, mix
    url: Optional[str] = None
    mix_id: Optional[str] = None
    mix_name: Optional[str] = Field(None, max_length=120)
    user_url: Optional[str] = None
    max_counts: Optional[int] = Field(
        20, ge=1, le=500, description="最大下载数量，None表示明确确认全量下载"
    )
    interval: str = Field(
        "all", description="日期区间，格式如 '2024-01-01|2024-12-31' 或 'all'"
    )

    @field_validator("url")
    @classmethod
    def validate_optional_url(cls, value: Optional[str]) -> Optional[str]:
        return _validate_douyin_url(value) if value else value

    @field_validator("user_url")
    @classmethod
    def validate_optional_user_url(cls, value: Optional[str]) -> Optional[str]:
        return _validate_douyin_url(value, require_user_page=True) if value else value

    @field_validator("type")
    @classmethod
    def validate_task_type(cls, value: str) -> str:
        if value not in {"single", "mix"}:
            raise ValueError("任务类型仅支持 single 或 mix")
        return value

    @field_validator("mix_id")
    @classmethod
    def validate_optional_mix_id(cls, value: Optional[str]) -> Optional[str]:
        if value and not MIX_ID_PATTERN.fullmatch(value.strip()):
            raise ValueError("合集 ID 仅支持 5–32 位数字")
        return value.strip() if value else value

    @field_validator("mix_name")
    @classmethod
    def validate_optional_mix_name(cls, value: Optional[str]) -> Optional[str]:
        return _validate_display_name(value) if value is not None else value

    @field_validator("interval")
    @classmethod
    def validate_task_interval(cls, value: str) -> str:
        return _validate_interval(value)


class TaskResponse(BaseModel):
    """任务响应"""

    success: bool
    data: Optional[Dict[str, Any]] = None
    message: str = ""


@app.get("/api/tasks", response_model=ApiResponse)
async def get_tasks(
    status: Optional[str] = None,
    type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=100000),
    offset: int = Query(0, ge=0),
    keyword: Optional[str] = Query(None, max_length=120),
    author: Optional[str] = None,
    sort_by: str = Query("created_at", pattern="^(created_at|completed_at|status|type|title)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
):
    """
    获取任务列表

    Args:
        status: 状态过滤 (pending, downloading, completed, failed, cancelled)
        type: 类型过滤 (single, mix, mix_user)
        limit: 返回数量限制
        offset: 偏移量
    """
    try:
        # 将字符串转换为枚举，再取value作为字符串传入
        task_status_enum = TaskStatus(status) if status else None
        task_type_enum = TaskType(type) if type else None

        # 提取字符串值用于get_tasks调用
        status_str = task_status_enum.value if task_status_enum else None

        print(
            f"[DY-WEB] 调用 get_tasks: status={status_str}, type={task_type_enum}, limit={limit}, offset={offset}"
        )
        result = await task_manager.get_tasks(
            status=status_str,
            task_type=task_type_enum,
            limit=limit,
            offset=offset,
            keyword=keyword.strip() if keyword else None,
            author=author,
            sort_by=sort_by,
            sort_order=sort_order,
        )

        tasks = result.get("tasks", [])
        total = result.get("total", 0)
        print(f"[DY-WEB] 获取到 {len(tasks)} 个任务, 总计 {total}")

        # 调试：检查第一个任务的video_name
        if tasks:
            first_task = tasks[0]
            print(f"[DY-WEB] 返回任务列表，第一个任务: id={first_task.id[:8]}...")
            print(f"[DY-WEB] 第一个任务类型: {first_task.type}")
            print(f"[DY-WEB] 第一个任务状态: {first_task.status}")
            print(f"[DY-WEB] 第一个任务video_name: '{first_task.video_name}'")

        return ApiResponse(
            success=True,
            data={
                "tasks": [task.to_dict() for task in tasks],
                "total": total,
                "authors": result.get("authors", []),
            },
            message="获取任务列表成功",
        )
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_TASK_FILTER",
                "message": "任务状态或类型筛选值无效",
                "action": "清除筛选条件后重试",
            },
        )
    except Exception as e:
        print(f"[DY-WEB] 获取任务列表失败: {e}")
        import traceback

        print(traceback.format_exc())
        return ApiResponse(
            success=False,
            data={"tasks": [], "total": 0},
            message=f"获取任务列表失败: {str(e)}",
        )


@lru_cache(maxsize=256)
def _local_video_thumbnail(path: str, modified_ns: int) -> bytes:
    """从已下载视频读取一帧，缓存限于本进程，不写入媒体文件。"""
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", "0.5", "-i", path,
         "-frames:v", "1", "-vf", "scale=320:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
        capture_output=True, timeout=8, check=False,
    )
    return result.stdout if result.returncode == 0 else b""


@app.get("/api/tasks/{task_id}/thumbnail")
async def get_task_thumbnail(task_id: str, aweme_id: Optional[str] = Query(None, min_length=5, max_length=32)):
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    video_path = await _resolve_task_video_path(task, "生成封面", aweme_id=aweme_id)
    try:
        image = await asyncio.to_thread(_local_video_thumbnail, str(video_path), video_path.stat().st_mtime_ns)
    except (OSError, subprocess.TimeoutExpired):
        image = b""
    if not image:
        raise HTTPException(status_code=404, detail="无法从本地视频生成封面")
    return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/tasks/{task_id}", response_model=ApiResponse)
async def get_task(task_id: str):
    """获取单个任务详情"""
    try:
        task = await task_manager.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        return ApiResponse(
            success=True, data=task.to_dict(), message="获取任务详情成功"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tasks/{task_id}/open-folder", response_model=ApiResponse)
async def open_task_folder(task_id: str):
    """Open a completed batch directory in the local Finder."""
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    folder = Path(task.download_path or task.base_path or "").expanduser().resolve()
    configured_root = Path(skill.douyin_conf.get("path", "Download")).expanduser().resolve() if skill else folder
    try:
        folder.relative_to(configured_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="目录不在下载根目录内")
    if not folder.exists():
        raise HTTPException(status_code=404, detail="下载目录不存在")
    if sys.platform != "darwin":
        raise HTTPException(status_code=501, detail="当前系统不支持自动打开文件夹，请复制目录路径")
    subprocess.Popen(["open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ApiResponse(success=True, data={"path": str(folder)}, message="已在 Finder 中打开下载目录")


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
MEDIA_SUFFIXES = VIDEO_SUFFIXES | {".flv", ".webp", ".jpeg", ".jpg", ".png", ".avif"}


def _safe_video_candidate(candidate: Path, root: Path) -> Optional[Path]:
    """Resolve one candidate and ensure it stays inside the download root."""
    try:
        resolved = candidate.expanduser().resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    try:
        if resolved.is_file() and resolved.suffix.lower() in VIDEO_SUFFIXES and resolved.stat().st_size > 0:
            return resolved
    except OSError:
        return None
    return None


def _find_task_video_file(root: Path, values: List[Optional[str]], aweme_ids: List[str]) -> Path:
    """Find a downloaded video by exact path/name, then by an anchored aweme ID.

    Older task records persist only the filename even though F2 stores each video
    below a nested directory. Recursive lookup is therefore required, but it must
    remain deterministic and safe: exact basename matches win, multiple matches
    are surfaced as a conflict, and ID fallback only accepts an ID followed by
    ``_`` or ``.`` (never a fuzzy prefix).
    """
    root = root.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError("下载目录不存在")

    # A stored relative/absolute path is the strongest signal. It is still
    # checked against the root to prevent traversal or symlink escapes.
    for value in values:
        if not value:
            continue
        candidate = Path(str(value)).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = _safe_video_candidate(candidate, root)
        if resolved:
            return resolved

    # The database may contain only a basename. Search recursively by exact
    # filename, never by a broad glob that silently picks the first result.
    exact_names: List[str] = []
    for value in values:
        if not value:
            continue
        name = Path(str(value)).name
        if name and name not in exact_names and Path(name).suffix.lower() in VIDEO_SUFFIXES:
            exact_names.append(name)

    for name in exact_names:
        matches: List[Path] = []
        for candidate in root.rglob(name):
            resolved = _safe_video_candidate(candidate, root)
            if resolved and resolved.name == name and resolved not in matches:
                matches.append(resolved)
        if len(matches) > 1:
            raise RuntimeError("VIDEO_FILE_AMBIGUOUS")
        if matches:
            return matches[0]

    # Last resort for legacy rows whose filename was not persisted: use the
    # video ID as an anchored filename prefix, with the same ambiguity guard.
    seen_ids: List[str] = []
    for raw_id in aweme_ids:
        aweme_id = str(raw_id or "").strip()
        if aweme_id and aweme_id not in seen_ids:
            seen_ids.append(aweme_id)
    for aweme_id in seen_ids:
        pattern = re.compile(rf"^{re.escape(aweme_id)}(?:_|\.)", re.IGNORECASE)
        matches = []
        for candidate in root.rglob("*"):
            if not pattern.match(candidate.name):
                continue
            resolved = _safe_video_candidate(candidate, root)
            if resolved and resolved not in matches:
                matches.append(resolved)
        if len(matches) > 1:
            raise RuntimeError("VIDEO_FILE_AMBIGUOUS")
        if matches:
            return matches[0]

    raise FileNotFoundError("VIDEO_FILE_NOT_FOUND")


def _find_media_candidates(search_root: Path, requested_name: str, requested_stem: str, aweme_id: str) -> List[Path]:
    """Bounded, deterministic media lookup used by the file endpoint."""
    if not search_root.exists() or not search_root.is_dir():
        return []
    exact_name_matches: List[Path] = []
    exact_stem_matches: List[Path] = []
    anchored_id_matches: List[Path] = []
    for candidate in search_root.rglob("*"):
        if not candidate.is_file() or candidate.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        exact_name = candidate.name == requested_name
        exact_stem = candidate.stem == requested_stem
        anchored_id = bool(aweme_id and re.match(rf"^{re.escape(aweme_id)}(?:_|\.|$)", candidate.name))
        if exact_name:
            exact_name_matches.append(candidate)
        elif exact_stem:
            exact_stem_matches.append(candidate)
        elif anchored_id:
            anchored_id_matches.append(candidate)

    # Prefer the persisted filename/stem.  A video directory also contains
    # *_music.mp3, *_cover.jpeg and *_desc.txt; those companion files share the
    # aweme prefix but must not make an otherwise exact video ambiguous.
    if exact_name_matches:
        return sorted(exact_name_matches, key=lambda item: str(item))
    if exact_stem_matches:
        return sorted(exact_stem_matches, key=lambda item: str(item))
    return sorted(anchored_id_matches, key=lambda item: str(item))


async def _resolve_task_video_path(task: Any, feature_name: str = "口播提取", aweme_id: Optional[str] = None, *, require_completed: bool = True) -> Path:
    """Resolve the task's local video without leaving the configured root."""
    if task.type not in {TaskType.SINGLE.value, "batch_item"} and not aweme_id:
        raise HTTPException(status_code=422, detail={"code": "VIDEO_FEATURE_SINGLE_ONLY", "message": f"{feature_name}目前只支持已完成的单视频任务", "action": "请在单视频任务上操作"})
    if require_completed and task.status != TaskStatus.COMPLETED.value:
        raise HTTPException(status_code=409, detail={"code": "VIDEO_FEATURE_TASK_NOT_READY", "message": f"视频尚未下载完成，暂时无法{feature_name}", "action": "等待任务完成后重试"})
    root = Path(skill.douyin_conf.get("path", "Download")).expanduser().resolve() if skill else Path("Download").resolve()
    videos = await task_manager.db.get_task_videos(task.id)
    if aweme_id:
        videos = [video for video in videos if video.aweme_id == aweme_id]
        if not videos:
            raise HTTPException(status_code=404, detail={"code": "VIDEO_ITEM_NOT_FOUND", "message": "批量任务中找不到该视频", "action": "刷新任务列表后重试"})
    values = [video.file_path for video in videos if video.file_path]
    values.extend([task.file_name, task.download_path, task.base_path])
    aweme_ids = [video.aweme_id for video in videos if video.aweme_id]
    aweme_id = (task.video_info or {}).get("aweme_id") if getattr(task, "video_info", None) else None
    if aweme_id:
        aweme_ids.append(str(aweme_id))
    try:
        # Recursive fallback is synchronous filesystem I/O; keep it off the
        # event loop so progress broadcasts and other requests remain live.
        return await asyncio.to_thread(_find_task_video_file, root, values, aweme_ids)
    except RuntimeError as exc:
        if str(exc) == "VIDEO_FILE_AMBIGUOUS":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "VIDEO_FILE_AMBIGUOUS",
                    "message": f"找到多个可能的视频文件，无法安全判断应进行{feature_name}的文件",
                    "action": "保留唯一文件后重试，或在任务记录中补充精确文件路径",
                },
            ) from exc
        raise
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "VIDEO_FILE_NOT_FOUND",
                "message": f"找不到该任务对应的本地视频文件，无法进行{feature_name}",
                "action": "确认视频文件仍在下载目录后重试",
            },
        ) from exc


async def _materialize_batch_video_task_reference(task_id: str) -> str:
    """将画廊里的虚拟作品 ID 映射为隐藏的单视频任务。

    批量任务的每个作品只在前端展开显示，数据库中仍只有一个父任务。
    口播/总结接口按任务 ID 工作，因此第一次点击时创建一个不进入列表的
    ``batch_item`` 引用，复用已有本地文件，不会再次下载或制造可见重复项。
    """
    if "::" not in task_id:
        return task_id

    parts = task_id.split("::")
    if len(parts) != 2 or not parts[0] or not re.fullmatch(r"\d{5,32}", parts[1]):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIDEO_ITEM_REFERENCE_INVALID",
                "message": "视频条目标识无效，无法打开总结或口播功能",
                "action": "刷新作品列表后重试",
            },
        )

    parent_id, aweme_id = parts
    parent = await task_manager.get_task(parent_id)
    if not parent or parent.type not in {"post", "mix", "mix_user"}:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "VIDEO_PARENT_TASK_NOT_FOUND",
                "message": "批量作品所属任务不存在",
                "action": "刷新任务列表后重试",
            },
        )

    videos = await task_manager.db.get_task_videos(parent_id)
    video = next((item for item in videos if str(item.aweme_id) == aweme_id), None)
    if not video or video.status != "downloaded" or not video.file_path:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VIDEO_FEATURE_TASK_NOT_READY",
                "message": "该作品尚未下载完成，暂时无法生成口播或视频总结",
                "action": "等待下载完成后重试",
            },
        )

    existing = await task_manager.db.fetch_one(
        "SELECT t.task_id FROM tasks t "
        "JOIN task_videos tv ON tv.task_id=t.task_id "
        "WHERE t.type='batch_item' AND t.mix_id=? AND tv.aweme_id=? "
        "ORDER BY t.created_at DESC LIMIT 1",
        (parent_id, aweme_id),
    )
    if existing:
        return str(existing[0])

    child_id = await task_manager.db.create_task(
        task_type="batch_item",
        mix_id=parent_id,
        mix_name=parent.mix_name,
        base_path=parent.base_path,
    )
    await task_manager.db.add_video(
        TaskVideoInfo(
            id=0,
            task_id=child_id,
            aweme_id=video.aweme_id,
            desc=video.desc or "",
            nickname=video.nickname or "",
            sec_user_id=video.sec_user_id,
            create_time=video.create_time,
            status="downloaded",
            file_path=video.file_path,
            file_size=video.file_size,
            error_message=None,
            download_time=video.download_time or datetime.now().isoformat(),
            video_play_addr=video.video_play_addr,
            cover=video.cover,
            duration=video.duration,
        )
    )
    await task_manager.db.update_task_counts(child_id, total=1, downloaded=1, skipped=0, failed=0)
    await task_manager.db.update_task_status(child_id, "completed")
    return child_id


async def _run_transcription_job(job_id: str, task_id: str, video_path: Path) -> None:
    async def progress(stage: str, percentage: int) -> None:
        await task_manager.db.update_transcript_job(job_id, stage=stage, progress=percentage)
        if batch_results:
            await batch_results.observe_execution(job_id, "transcript", stage)

    try:
        await task_manager.db.update_transcript_job(job_id, status="running", stage="queued", progress=1)
        result = await transcription_service.run(job_id, video_path, progress,
            partial_callback=lambda text: task_manager.db.update_transcript_job(job_id, original_transcript=text))
        await task_manager.db.update_transcript_job(
            job_id,
            status="completed",
            stage="completed",
            progress=100,
            original_transcript=result["original_transcript"],
            polished_text=result["polished_text"],
            segments_json=json.dumps(result.get("segments", []), ensure_ascii=False),
            error_json=None,
            completed_at=datetime.now().isoformat(),
        )
    except asyncio.CancelledError:
        await task_manager.db.update_transcript_job(job_id, status="cancelled", stage="cancelled", error_json=json.dumps(serialize_error(asyncio.CancelledError()), ensure_ascii=False))
        raise
    except ModelControlError as exc:
        await task_manager.db.update_transcript_job(job_id, status="paused", stage="budget_paused",
            error_json=json.dumps({"code": exc.code, "reason": str(exc), "action": "在批次结果中调整调用上限后继续，已完成的转写会复用"}, ensure_ascii=False))
    except Exception as exc:
        await task_manager.db.update_transcript_job(job_id, status="failed", stage="failed", error_json=json.dumps(serialize_error(exc), ensure_ascii=False))
    finally:
        if batch_results:
            latest = await task_manager.db.get_transcript_job(job_id=job_id)
            if latest:
                await batch_results.finish_execution(job_id, "transcript", latest["status"])
        running_transcription_tasks.pop(job_id, None)


class TranscriptCreateRequest(BaseModel):
    force: bool = False


class SummaryCreateRequest(BaseModel):
    force: bool = False


class AuthorSummaryRequest(BaseModel):
    author: str = Field(min_length=1, max_length=120)
    request_limit: Optional[int] = Field(default=None, ge=0, le=1000000)


class AssignAuthorRequest(AuthorSummaryRequest):
    task_ids: List[str] = Field(min_length=1, max_length=200)


@app.post("/api/task-authors/assign", response_model=ApiResponse)
async def assign_task_author(request: AssignAuthorRequest):
    """将选中的单视频记录归入指定作者分类，保留媒体和 AI 结果。"""
    author = request.author.strip()
    if not author or re.search(r"[\x00-\x1f]", author):
        raise HTTPException(status_code=422, detail="请输入有效的作者名")
    references = []
    for task_id in dict.fromkeys(request.task_ids):
        task = await task_manager.get_task(task_id)
        if not task or task.type != "single":
            raise HTTPException(status_code=422, detail="请选择独立的单视频记录进行作者归类")
        aweme_id = str(task.video_info.get("aweme_id") or "")
        if not aweme_id:
            raise HTTPException(status_code=422, detail="作品缺少视频 ID，无法归类")
        references.append(aweme_id)
    # 同一作品的历史记录同步归类，避免重试后再次回到未知作者。
    for aweme_id in references:
        await task_manager.db.execute(
            "UPDATE task_videos SET nickname=? WHERE aweme_id=? "
            "AND task_id IN (SELECT task_id FROM tasks WHERE type IN ('single','batch_item'))",
            (author, aweme_id),
        )
    await task_manager.db.commit()
    return ApiResponse(success=True, data={"count": len(references), "author": author}, message=f"已将 {len(references)} 条作品归到 {author}")


def _distill_artifact_job_id(item: Dict[str, Any], result_type: str) -> str:
    """Build a stable, read-only job-shaped ID for a distill snapshot."""
    return f"distill:{item['run_id']}:{item['aweme_id']}:{result_type}"


def _distill_transcript_job(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not item or not (item.get("original_transcript") or item.get("polished_text")):
        return None
    metadata = item.get("metadata") or {}
    return {
        "job_id": item.get("transcript_job_id") or _distill_artifact_job_id(item, "transcript"),
        "task_id": item.get("task_id"),
        "video_name": metadata.get("desc") or metadata.get("title") or item.get("aweme_id"),
        "author": metadata.get("nickname") or metadata.get("author"),
        "aweme_id": item.get("aweme_id"),
        "url": metadata.get("url") or metadata.get("share_url"),
        "status": "completed",
        "stage": "completed",
        "progress": 100,
        "original_transcript": item.get("original_transcript") or "",
        "polished_text": item.get("polished_text") or "",
        "segments": [],
        "error": None,
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "completed_at": item.get("updated_at"),
        "source": "distill",
        "source_run_id": item.get("run_id"),
    }


def _distill_summary_job(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not item or not item.get("summary"):
        return None
    return {
        "job_id": item.get("summary_job_id") or _distill_artifact_job_id(item, "summary"),
        "task_id": item.get("task_id"),
        "status": "completed",
        "stage": "completed",
        "progress": 100,
        "model": "gpt-5.6-luna",
        "input_bytes": item.get("source_size") or 0,
        "summary": item.get("summary"),
        "error": None,
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "completed_at": item.get("updated_at"),
        "prepared_bytes": None,
        "processing_mode": None,
        "segment_count": None,
        "source": "distill",
        "source_run_id": item.get("run_id"),
    }


def _parse_distill_artifact_job_id(job_id: str, result_type: str) -> Optional[tuple[str, str]]:
    parts = str(job_id or "").split(":")
    if len(parts) != 4 or parts[0] != "distill" or parts[3] != result_type:
        return None
    return parts[1], parts[2]


def _has_transcript_content(job: Optional[Dict[str, Any]]) -> bool:
    """判断普通口播作业是否已经有可展示的结果。"""
    return bool(job and (job.get("original_transcript") or job.get("polished_text")))


def _transcript_export_value(value: Any, fallback: str = "未生成") -> str:
    """将导出字段收敛为单行、无敏感内容的 Markdown 文本。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text or fallback


def _safe_transcript_filename(title: Any, task_id: str) -> str:
    """生成不允许路径穿越、长度受控的口播 Markdown 文件名。"""
    candidate = str(title or "").strip()
    candidate = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", candidate)
    candidate = re.sub(r"\.{2,}", "_", candidate).strip(" ._")
    candidate = re.sub(r"\s+", " ", candidate)
    candidate = candidate[:96] or str(task_id)
    return f"{candidate}-口播文案.md"


async def _transcript_export_metadata(job: Dict[str, Any]) -> Dict[str, str]:
    """只提取可公开展示的任务元数据，不把路径、Cookie 或诊断信息写入文件。"""
    task_id = str(job.get("task_id") or "")
    title = job.get("video_name") or job.get("title")
    author = job.get("author")
    aweme_id = job.get("aweme_id")
    source_url = job.get("url")
    task_getter = getattr(task_manager, "get_task", None)
    if task_getter and task_id:
        try:
            task = await task_getter(task_id)
        except Exception:
            task = None
        if task:
            title = title or getattr(task, "video_name", None)
            source_url = source_url or getattr(task, "url", None)
            info = getattr(task, "video_info", None) or {}
            if isinstance(info, dict):
                author = author or info.get("nickname")
                aweme_id = aweme_id or info.get("aweme_id")
                source_url = source_url or info.get("url")
                title = title or info.get("desc")
    if source_url and not str(source_url).strip().lower().startswith(("http://", "https://")):
        source_url = None
    return {
        "task_id": _transcript_export_value(task_id, "未生成"),
        "title": _transcript_export_value(title, task_id or "未生成"),
        "author": _transcript_export_value(author),
        "aweme_id": _transcript_export_value(aweme_id),
        "source_url": _transcript_export_value(source_url),
    }


def _render_transcript_markdown(job: Dict[str, Any], metadata: Dict[str, str]) -> str:
    """渲染口播导出的 Markdown，明确区分整理文案与原始转写。"""
    polished = str(job.get("polished_text") or "").strip() or "未生成"
    original = str(job.get("original_transcript") or "").strip() or "未生成"
    lines = [
        f"# {metadata['title']} · 口播文案",
        "",
        "## 基础信息",
        f"- 任务 ID：{metadata['task_id']}",
        f"- 作者：{metadata['author']}",
        f"- 作品 ID：{metadata['aweme_id']}",
        f"- 来源链接：{metadata['source_url']}",
        "",
        "## 整理文案",
        polished,
        "",
        "## 原始转写",
        original,
        "",
    ]
    return "\n".join(lines)


def _has_summary_content(job: Optional[Dict[str, Any]]) -> bool:
    """判断普通总结作业是否已经有可展示的结果。"""
    return bool(job and job.get("summary"))


@app.post("/api/tasks/{task_id}/transcript", response_model=ApiResponse)
async def create_transcript(task_id: str, request: TranscriptCreateRequest):
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")
    try:
        task_id = await _materialize_batch_video_task_reference(task_id)
        task = await task_manager.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "任务不存在"})
        existing = await task_manager.db.get_transcript_job(task_id=task_id)
        if existing and (existing["status"] in {"queued", "running"} or (existing["status"] == "paused" and not request.force)):
            if product_runtime:
                await product_runtime.track(task, "transcript", existing, "shared")
            return ApiResponse(success=True, data=existing, message="口播提取正在处理中")
        if existing and existing["status"] == "completed" and (existing.get("original_transcript") or existing.get("polished_text")) and not request.force:
            if product_runtime:
                await product_runtime.track(task, "transcript", existing, "reused")
            return ApiResponse(success=True, data=existing, message="已返回已有口播文案")
        if not request.force:
            reused = _distill_transcript_job(await task_manager.db.get_latest_distill_result_for_task(task_id, "transcript"))
            if reused:
                if product_runtime:
                    await product_runtime.track(task, "transcript", reused, "reused")
                return ApiResponse(success=True, data=reused, message="已返回已有口播文案（来自蒸馏结果）")
        video_path = await _resolve_task_video_path(task)
        transcription_service.validate_config()
        job_id = existing["job_id"] if existing else new_job_id()
        if existing:
            await task_manager.db.update_transcript_job(job_id, status="queued", stage="queued", progress=0, original_transcript="", polished_text="", segments_json="[]", error_json=None, completed_at=None)
        else:
            await task_manager.db.create_transcript_job(job_id, task_id)
        execution_id = await product_runtime.track(task, "transcript", {"job_id": job_id}, "new") if product_runtime else job_id
        context = current_request_context()
        with model_request_context(batch_id=context.batch_id or task_id, execution_id=execution_id, stage="transcript"):
            task_handle = asyncio.create_task(_run_transcription_job(job_id, task_id, video_path))
        running_transcription_tasks[job_id] = task_handle
        data = await task_manager.db.get_transcript_job(job_id)
        return ApiResponse(success=True, data=data, message="口播提取已加入处理队列")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"code": "TRANSCRIPT_CONFIG_INVALID", "message": str(exc), "action": "配置模型服务地址、模型和 API Key 后重试"}) from exc


@app.get("/api/tasks/{task_id}/transcript", response_model=ApiResponse)
async def get_transcript(task_id: str):
    task_id = await _materialize_batch_video_task_reference(task_id)
    job = await task_manager.db.get_transcript_job(task_id=task_id)
    if not job:
        job = _distill_transcript_job(await task_manager.db.get_latest_distill_result_for_task(task_id, "transcript"))
        if job:
            return ApiResponse(success=True, data=job, message="获取已有口播文案成功")
        return ApiResponse(success=True, data=None, message="尚未提取口播文案")
    # 进行中的普通作业必须原样返回，避免前端在轮询时看到蒸馏快照而误判。
    # 已失败/取消，或 completed 但没有正文时，再尝试复用蒸馏快照。
    if job.get("status") in {"queued", "running"}:
        return ApiResponse(success=True, data=job, message="获取口播提取状态成功")
    if job.get("status") == "completed" and _has_transcript_content(job):
        return ApiResponse(success=True, data=job, message="获取口播提取状态成功")
    fallback = _distill_transcript_job(await task_manager.db.get_latest_distill_result_for_task(task_id, "transcript"))
    if fallback:
        return ApiResponse(success=True, data=fallback, message="获取已有口播文案成功")
    return ApiResponse(success=True, data=job, message="获取口播提取状态成功")


@app.post("/api/transcripts/{job_id}/cancel", response_model=ApiResponse)
async def cancel_transcript(job_id: str):
    job = await task_manager.db.get_transcript_job(job_id=job_id)
    if not job:
        raise HTTPException(status_code=404, detail="口播提取作业不存在")
    transcription_service.cancel(job_id)
    task = running_transcription_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    await task_manager.db.update_transcript_job(job_id, status="cancelled", stage="cancelled", error_json=json.dumps(serialize_error(asyncio.CancelledError()), ensure_ascii=False))
    return ApiResponse(success=True, data=await task_manager.db.get_transcript_job(job_id=job_id), message="口播提取已取消")


@app.post("/api/transcripts/{job_id}/retry", response_model=ApiResponse)
async def retry_transcript(job_id: str):
    job = await task_manager.db.get_transcript_job(job_id=job_id)
    if not job:
        raise HTTPException(status_code=404, detail="口播提取作业不存在")
    return await create_transcript(job["task_id"], TranscriptCreateRequest(force=True))


@app.get("/api/transcripts/{job_id}/export")
async def export_transcript(job_id: str, format: str = Query("txt", pattern="^(txt|md|json)$")):
    job = await task_manager.db.get_transcript_job(job_id=job_id)
    if not job:
        parsed = _parse_distill_artifact_job_id(job_id, "transcript")
        if parsed:
            run_id, aweme_id = parsed
            item = await task_manager.db.get_distill_item(run_id, aweme_id)
            job = _distill_transcript_job(item)
    if not job or job["status"] != "completed" or not _has_transcript_content(job):
        raise HTTPException(status_code=404, detail="口播文案尚未完成")
    if format == "json":
        content = json.dumps(job, ensure_ascii=False, indent=2)
        media_type = "application/json"
        filename = f"{job['task_id']}-transcript.json"
    elif format == "md":
        metadata = await _transcript_export_metadata(job)
        content = _render_transcript_markdown(job, metadata)
        media_type = "text/markdown; charset=utf-8"
        filename = _safe_transcript_filename(metadata["title"], str(job.get("task_id") or job_id))
    else:
        content = job["polished_text"] or job["original_transcript"]
        media_type = "text/plain; charset=utf-8"
        filename = f"{job['task_id']}-口播文案.txt"
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": _attachment_header(filename)})


async def _run_video_summary_job(job_id: str, task_id: str, video_path: Path) -> None:
    async def progress(stage: str, percentage: int) -> None:
        await task_manager.db.update_video_summary_job(job_id, stage=stage, progress=percentage)
        if batch_results:
            await batch_results.observe_execution(job_id, "summary", stage)

    async def prepared(info: Any) -> None:
        await task_manager.db.update_video_summary_job(job_id, prepared_bytes=info.prepared_bytes, processing_mode=info.mode, segment_count=info.segment_count)

    try:
        await task_manager.db.update_video_summary_job(job_id, status="running", stage="queued", progress=1)
        result = await video_summary_service.summarize_video(job_id, video_path, progress, prepared)
        processing = result.pop("_processing", None) or {}
        result["_time_basis"] = processing.get("time_basis", "model_estimate")
        await task_manager.db.update_video_summary_job(
            job_id,
            status="completed",
            stage="completed",
            progress=100,
            model=processing.get("model") or video_summary_service.config.model,
            prepared_bytes=processing.get("prepared_bytes"),
            processing_mode=processing.get("mode"),
            segment_count=processing.get("segment_count"),
            provider_attempts_json=json.dumps(video_summary_service._job_attempts.pop(job_id, []), ensure_ascii=False),
            summary_json=json.dumps(result, ensure_ascii=False),
            error_json=None,
            completed_at=datetime.now().isoformat(),
        )
    except asyncio.CancelledError:
        video_summary_service._job_attempts.pop(job_id, None)
        await task_manager.db.update_video_summary_job(
            job_id,
            status="cancelled",
            stage="cancelled",
            error_json=json.dumps(serialize_summary_error(asyncio.CancelledError()), ensure_ascii=False),
        )
        raise
    except ModelControlError as exc:
        await task_manager.db.update_video_summary_job(job_id, status="paused", stage="budget_paused",
            provider_attempts_json=json.dumps(video_summary_service._job_attempts.pop(job_id, []), ensure_ascii=False),
            error_json=json.dumps({"code": exc.code, "reason": str(exc), "action": "在批次结果中调整调用上限后继续，已完成片段会复用"}, ensure_ascii=False))
    except Exception as exc:
        await task_manager.db.update_video_summary_job(
            job_id,
            status="failed",
            stage="failed",
            error_json=json.dumps(serialize_summary_error(exc), ensure_ascii=False),
            model=getattr(exc, "model", video_summary_service.config.model),
            provider_attempts_json=json.dumps(video_summary_service._job_attempts.pop(job_id, []), ensure_ascii=False),
        )
    finally:
        if batch_results:
            latest = await task_manager.db.get_video_summary_job(job_id=job_id)
            if latest:
                await batch_results.finish_execution(job_id, "summary", latest["status"])
        running_summary_tasks.pop(job_id, None)


@app.post("/api/tasks/{task_id}/summary", response_model=ApiResponse)
async def create_video_summary(task_id: str, request: SummaryCreateRequest):
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")
    try:
        task_id = await _materialize_batch_video_task_reference(task_id)
        task = await task_manager.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "任务不存在"})
        existing = await task_manager.db.get_video_summary_job(task_id=task_id)
        if existing and (existing["status"] in {"queued", "running"} or (existing["status"] == "paused" and not request.force)):
            if product_runtime:
                await product_runtime.track(task, "summary", existing, "shared")
            return ApiResponse(success=True, data=existing, message="视频总结正在处理中")
        if existing and existing["status"] == "completed" and existing.get("summary") and not request.force:
            if product_runtime:
                await product_runtime.track(task, "summary", existing, "reused")
            return ApiResponse(success=True, data=existing, message="已返回已有视频总结")
        if not request.force:
            reused = _distill_summary_job(await task_manager.db.get_latest_distill_result_for_task(task_id, "summary"))
            if reused:
                if product_runtime:
                    await product_runtime.track(task, "summary", reused, "reused")
                return ApiResponse(success=True, data=reused, message="已返回已有视频总结（来自蒸馏结果）")
        video_path = await _resolve_task_video_path(task, "视频总结")
        video_summary_service.validate_config()
        input_bytes = video_path.stat().st_size
        job_id = existing["job_id"] if existing else new_summary_job_id()
        if existing:
            await task_manager.db.update_video_summary_job(
                job_id,
                status="queued",
                stage="queued",
                progress=0,
                model=video_summary_service.config.model,
                input_bytes=input_bytes,
                prepared_bytes=None,
                processing_mode=None,
                segment_count=None,
                summary_json=None,
                error_json=None,
                completed_at=None,
            )
        else:
            await task_manager.db.create_video_summary_job(
                job_id,
                task_id,
                video_summary_service.config.model,
                input_bytes,
            )
        execution_id = await product_runtime.track(task, "summary", {"job_id": job_id}, "new") if product_runtime else job_id
        context = current_request_context()
        with model_request_context(batch_id=context.batch_id or task_id, execution_id=execution_id, stage="summary"):
            task_handle = asyncio.create_task(_run_video_summary_job(job_id, task_id, video_path))
        running_summary_tasks[job_id] = task_handle
        return ApiResponse(
            success=True,
            data=await task_manager.db.get_video_summary_job(job_id=job_id),
            message="视频总结已加入处理队列",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SUMMARY_CONFIG_INVALID",
                "message": str(exc),
                "action": "检查视频总结主接口或备用接口的地址、模型名称和 API Key 后重试",
            },
        ) from exc


async def _run_author_summary_batch(batch_id: str, task_ids: List[str]) -> None:
    batch = summary_batches[batch_id]
    slots = asyncio.Semaphore(5)

    async def process_video(task_id: str) -> None:
        async with slots:
            if batch["status"] == "paused":
                return
            batch["current_task_id"] = task_id
            try:
                with model_request_context(batch_kind="summary", batch_id=batch_id):
                    response = await create_video_summary(task_id, SummaryCreateRequest(force=False))
                job = response.data or {}
                if job.get("status") == "completed":
                    batch["reused"] += 1
                else:
                    job_id = job.get("job_id")
                    running = running_summary_tasks.get(job_id)
                    if running:
                        await running
                    latest = await task_manager.db.get_video_summary_job(task_id=task_id)
                    if latest and latest["status"] == "completed":
                        batch["completed"] += 1
                    elif latest and latest["status"] == "paused":
                        batch["status"] = "paused"
                    else:
                        batch["failed"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                batch["failed"] += 1
            batch["processed"] += 1

    workers = [asyncio.create_task(process_video(task_id)) for task_id in task_ids]
    try:
        await asyncio.gather(*workers)
        if batch["status"] != "paused":
            batch["status"] = "completed"
    except asyncio.CancelledError:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        batch["status"] = "interrupted"
        raise
    finally:
        batch["current_task_id"] = None
        if batch_results:
            await batch_results.set_status("summary", batch_id, batch["status"])
        running_summary_batches.pop(batch_id, None)


async def _run_author_transcript_batch(batch_id: str, task_ids: List[str]) -> None:
    batch = transcript_batches[batch_id]
    try:
        for task_id in task_ids:
            if batch["status"] == "paused":
                break
            batch["current_task_id"] = task_id
            try:
                with model_request_context(batch_kind="transcript", batch_id=batch_id):
                    response = await create_transcript(task_id, TranscriptCreateRequest(force=False))
                job = response.data or {}
                if job.get("status") == "completed" and _has_transcript_content(job):
                    batch["reused"] += 1
                else:
                    job_id = job.get("job_id")
                    running = running_transcription_tasks.get(job_id)
                    if running:
                        await running
                    latest = await task_manager.db.get_transcript_job(task_id=task_id)
                    if latest and latest["status"] == "completed" and _has_transcript_content(latest):
                        batch["completed"] += 1
                    elif latest and latest["status"] == "paused":
                        batch["status"] = "paused"
                    else:
                        batch["failed"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                batch["failed"] += 1
            batch["processed"] += 1
        if batch["status"] != "paused":
            batch["status"] = "completed"
    except asyncio.CancelledError:
        batch["status"] = "interrupted"
        raise
    finally:
        batch["current_task_id"] = None
        if batch_results:
            await batch_results.set_status("transcript", batch_id, batch["status"])
        running_transcript_batches.pop(batch_id, None)


async def _get_author_downloaded_video_tasks(author: str) -> List[Any]:
    """收集作者的已下载作品，包含批量任务里的单条视频。"""
    result = await task_manager.get_tasks(author=author, limit=100000)
    collected = {}
    for task in result["tasks"]:
        if task.type == "single" and task.status == "completed":
            collected[str(task.video_info.get("aweme_id") or task.id)] = task
    for parent in result["tasks"]:
        if parent.type not in {"post", "mix", "mix_user"}:
            continue
        for video in await task_manager.db.get_task_videos(parent.id):
            if video.status != "downloaded" or not video.file_path or video.aweme_id in collected:
                continue
            child_id = await _materialize_batch_video_task_reference(f"{parent.id}::{video.aweme_id}")
            child = await task_manager.get_task(child_id)
            if child:
                collected[video.aweme_id] = child
    tasks = list(collected.values())
    statuses = await task_manager.db.get_task_summary_statuses([task.id for task in tasks])
    for task in tasks:
        task.summary_status = statuses.get(task.id, "none")
    return tasks


@app.post("/api/summary-batches", response_model=ApiResponse)
async def create_author_summary_batch(request: AuthorSummaryRequest):
    """逐条总结该作者已下载的单视频，已有结果由单视频接口复用。"""
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")
    author = request.author.strip()
    if not author:
        raise HTTPException(status_code=422, detail="请选择具体作者")
    for batch in summary_batches.values():
        if batch["status"] == "running":
            if batch["author"] == author:
                return ApiResponse(success=True, data=batch, message="该作者的批量总结正在进行")
            raise HTTPException(status_code=409, detail="已有其他作者的批量总结正在进行，请稍后重试")
    if any(batch["status"] == "running" for batch in transcript_batches.values()):
        raise HTTPException(status_code=409, detail="已有作者口播批次正在进行，请完成后再开始视频总结")
    author_tasks = await _get_author_downloaded_video_tasks(author)
    if not author_tasks:
        raise HTTPException(status_code=422, detail="该作者没有已下载的单视频")
    task_ids = [task.id for task in author_tasks if task.summary_status in {"none", "failed"}]
    batch_id = uuid.uuid4().hex
    batch = {"batch_id": batch_id, "author": author, "status": "running" if task_ids else "completed", "total": len(task_ids),
             "processed": 0, "completed": 0, "reused": 0, "failed": 0,
             "skipped_summarized": sum(task.summary_status == "completed" for task in author_tasks),
             "skipped_running": sum(task.summary_status == "running" for task in author_tasks),
             "current_task_id": None}
    summary_batches[batch_id] = batch
    if product_runtime:
        await product_runtime.register_author("summary", batch, author_tasks, task_ids, request.request_limit)
    if task_ids:
        running_summary_batches[batch_id] = asyncio.create_task(_run_author_summary_batch(batch_id, task_ids))
    message = "作者视频逐条总结已开始" if task_ids else "没有新任务：已有总结或正在总结的视频均已跳过"
    return ApiResponse(success=True, data=batch, message=message)


@app.post("/api/summary-batches/preview", response_model=ApiResponse)
async def preview_author_summary_batch(request: AuthorSummaryRequest):
    author = request.author.strip()
    if not author:
        raise HTTPException(status_code=422, detail="请选择具体作者")
    tasks = await _get_author_downloaded_video_tasks(author)
    preview = {
        "author": author,
        "total": len(tasks),
        "to_summarize": sum(task.summary_status in {"none", "failed"} for task in tasks),
        "already_summarized": sum(task.summary_status == "completed" for task in tasks),
        "already_running": sum(task.summary_status == "running" for task in tasks),
        "previous_failures": sum(task.summary_status == "failed" for task in tasks),
    }
    return ApiResponse(success=True, data=preview, message="已统计待总结视频")


@app.get("/api/summary-batches", response_model=ApiResponse)
async def get_author_summary_batch(author: str = Query(..., min_length=1, max_length=120)):
    batch = next((item for item in reversed(list(summary_batches.values())) if item["author"] == author), None)
    return ApiResponse(success=True, data=batch, message="获取作者视频总结进度成功")


@app.get("/api/author-summaries/export")
async def export_author_summaries(author: str = Query(..., min_length=1, max_length=120)):
    """将作者已有的逐条总结汇总为便于 AI 读取的 Markdown。"""
    tasks = await _get_author_downloaded_video_tasks(author.strip())
    name = next((task.video_info.get("nickname") for task in tasks if task.video_info.get("nickname")), author.strip())
    sections = []
    missing = []
    for task in sorted(tasks, key=lambda item: (str(item.video_info.get("create_time") or ""), item.id)):
        response = await get_video_summary(task.id)
        job = response.data
        title = str(task.video_name or "未知视频")
        aweme_id = str(task.video_info.get("aweme_id") or "")
        if not job or job.get("status") != "completed" or not _has_summary_content(job):
            missing.append(f"- {title}（作品 ID：{aweme_id}）：尚无成功总结")
            continue
        exported = await export_video_summary(job["job_id"], "md")
        markdown = exported.body.decode("utf-8")
        # 每条视频下保留全部总结字段，将标题层级下移以适配作者文档。
        markdown = re.sub(r"^(#{1,5}) ", r"\1# ", markdown, flags=re.MULTILINE)
        sections.append(f"{markdown.splitlines()[0]}\n\n- 原视频标题：{title}\n- 作品 ID：{aweme_id}\n- 来源：https://www.douyin.com/video/{aweme_id}\n- 总结更新时间：{job.get('updated_at') or '未记录'}\n\n" + "\n".join(markdown.splitlines()[1:]))
    if not sections:
        raise HTTPException(status_code=409, detail="该作者尚无成功的视频总结，请先生成总结")
    content = f"# {name} · 视频总结汇总\n\n- 导出时间：{datetime.now().isoformat(timespec='seconds')}\n- 已汇总：{len(sections)} 条\n- 尚无成功总结：{len(missing)} 条\n\n以下按视频逐条整理，内容来自已有视频总结。\n\n" + "\n\n---\n\n".join(sections)
    if missing:
        content += "\n\n## 未收录作品\n\n" + "\n".join(missing)
    filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(name))
    filename = re.sub(r'\.{2,}', '_', filename).strip(' .')[:96] or "博主"
    return Response(content=content + "\n", media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": _attachment_header(filename + ".md")})


@app.post("/api/transcript-batches", response_model=ApiResponse)
async def create_author_transcript_batch(request: AuthorSummaryRequest):
    """逐条提取作者已下载单视频的口播，已有结果由单视频接口复用。"""
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")
    author = request.author.strip()
    if not author:
        raise HTTPException(status_code=422, detail="请选择具体作者")
    for batch in transcript_batches.values():
        if batch["status"] == "running":
            if batch["author"] == author:
                return ApiResponse(success=True, data=batch, message="该作者的口播批次正在进行")
            raise HTTPException(status_code=409, detail="已有其他作者的口播批次正在进行，请稍后重试")
    if any(batch["status"] == "running" for batch in summary_batches.values()):
        raise HTTPException(status_code=409, detail="已有作者视频总结正在进行，请完成后再开始口播提取")

    author_tasks = await _get_author_downloaded_video_tasks(author)
    if not author_tasks:
        raise HTTPException(status_code=422, detail="该作者没有已下载的单视频")
    statuses = await task_manager.db.get_task_transcript_statuses([task.id for task in author_tasks])
    task_ids = [task.id for task in author_tasks if statuses.get(task.id, "none") in {"none", "failed"}]
    batch_id = uuid.uuid4().hex
    batch = {
        "batch_id": batch_id,
        "author": author,
        "status": "running" if task_ids else "completed",
        "total": len(task_ids),
        "processed": 0,
        "completed": 0,
        "reused": 0,
        "failed": 0,
        "skipped_transcribed": sum(status == "completed" for status in statuses.values()),
        "skipped_running": sum(status == "running" for status in statuses.values()),
        "current_task_id": None,
    }
    transcript_batches[batch_id] = batch
    if product_runtime:
        await product_runtime.register_author("transcript", batch, author_tasks, task_ids, request.request_limit)
    if task_ids:
        running_transcript_batches[batch_id] = asyncio.create_task(_run_author_transcript_batch(batch_id, task_ids))
    message = "作者视频逐条口播提取已开始" if task_ids else "没有新任务：已有口播或正在提取的视频均已跳过"
    return ApiResponse(success=True, data=batch, message=message)


@app.post("/api/transcript-batches/preview", response_model=ApiResponse)
async def preview_author_transcript_batch(request: AuthorSummaryRequest):
    author = request.author.strip()
    if not author:
        raise HTTPException(status_code=422, detail="请选择具体作者")
    author_tasks = await _get_author_downloaded_video_tasks(author)
    statuses = await task_manager.db.get_task_transcript_statuses([task.id for task in author_tasks])
    preview = {
        "author": author,
        "total": len(author_tasks),
        "to_transcribe": sum(statuses.get(task.id, "none") in {"none", "failed"} for task in author_tasks),
        "already_transcribed": sum(statuses.get(task.id) == "completed" for task in author_tasks),
        "already_running": sum(statuses.get(task.id) == "running" for task in author_tasks),
        "previous_failures": sum(statuses.get(task.id) == "failed" for task in author_tasks),
    }
    return ApiResponse(success=True, data=preview, message="已统计待提取口播视频")


@app.get("/api/transcript-batches", response_model=ApiResponse)
async def get_author_transcript_batch(author: str = Query(..., min_length=1, max_length=120)):
    batch = next((item for item in reversed(list(transcript_batches.values())) if item["author"] == author), None)
    return ApiResponse(success=True, data=batch, message="获取作者口播提取进度成功")


@app.get("/api/tasks/{task_id}/summary", response_model=ApiResponse)
async def get_video_summary(task_id: str):
    task_id = await _materialize_batch_video_task_reference(task_id)
    job = await task_manager.db.get_video_summary_job(task_id=task_id)
    if not job:
        job = _distill_summary_job(await task_manager.db.get_latest_distill_result_for_task(task_id, "summary"))
        if job:
            return ApiResponse(success=True, data=job, message="获取已有视频总结成功")
        return ApiResponse(success=True, data=None, message="尚未生成视频总结")
    # 进行中的普通作业必须原样返回；失败/取消或空 completed 才回退到蒸馏快照。
    if job.get("status") in {"queued", "running"}:
        return ApiResponse(success=True, data=job, message="获取视频总结状态成功")
    if job.get("status") == "completed" and _has_summary_content(job):
        return ApiResponse(success=True, data=job, message="获取视频总结状态成功")
    fallback = _distill_summary_job(await task_manager.db.get_latest_distill_result_for_task(task_id, "summary"))
    if fallback:
        return ApiResponse(success=True, data=fallback, message="获取已有视频总结成功")
    return ApiResponse(success=True, data=job, message="获取视频总结状态成功")


@app.post("/api/summaries/{job_id}/cancel", response_model=ApiResponse)
async def cancel_video_summary(job_id: str):
    job = await task_manager.db.get_video_summary_job(job_id=job_id)
    if not job:
        raise HTTPException(status_code=404, detail="视频总结作业不存在")
    video_summary_service.cancel(job_id)
    task = running_summary_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    await task_manager.db.update_video_summary_job(
        job_id,
        status="cancelled",
        stage="cancelled",
        error_json=json.dumps(serialize_summary_error(asyncio.CancelledError()), ensure_ascii=False),
    )
    return ApiResponse(success=True, data=await task_manager.db.get_video_summary_job(job_id=job_id), message="视频总结已取消")


@app.post("/api/summaries/{job_id}/retry", response_model=ApiResponse)
async def retry_video_summary(job_id: str):
    job = await task_manager.db.get_video_summary_job(job_id=job_id)
    if not job:
        raise HTTPException(status_code=404, detail="视频总结作业不存在")
    return await create_video_summary(job["task_id"], SummaryCreateRequest(force=True))


@app.get("/api/summaries/{job_id}/export")
async def export_video_summary(job_id: str, format: str = Query("md", pattern="^(md|txt|json)$")):
    job = await task_manager.db.get_video_summary_job(job_id=job_id)
    if not job:
        parsed = _parse_distill_artifact_job_id(job_id, "summary")
        if parsed:
            run_id, aweme_id = parsed
            item = await task_manager.db.get_distill_item(run_id, aweme_id)
            job = _distill_summary_job(item)
    if not job or job["status"] != "completed" or not _has_summary_content(job):
        raise HTTPException(status_code=404, detail="视频总结尚未完成")
    summary = job["summary"]
    if format == "json":
        content = json.dumps(summary, ensure_ascii=False, indent=2)
        media_type = "application/json"
        filename = f"{job['task_id']}-video-summary.json"
    else:
        lines = [f"# {summary.get('title') or '视频总结'}", "", summary.get("one_sentence", ""), "", "## 内容概览", summary.get("overview", "")]
        if summary.get("key_points"):
            lines.extend(["", "## 关键要点", *[f"- {item}" for item in summary["key_points"]]])
        if summary.get("timeline"):
            lines.extend(["", "## 时间线", *[f"- {item.get('time', '')} {item.get('content', '')}" if isinstance(item, dict) else f"- {item}" for item in summary["timeline"]]])
        if summary.get("visual_content"):
            lines.extend(["", "## 画面内容", *[f"- {item}" for item in summary["visual_content"]]])
        if summary.get("spoken_content"):
            lines.extend(["", "## 口播内容", *[f"- {item}" for item in summary["spoken_content"]]])
        structure = summary.get("narrative_structure") or {}
        if structure:
            lines.extend(["", "## 叙事结构"])
            structure_labels = {"hook": "开场钩子", "setup": "背景铺垫", "development": "发展", "turning_point": "转折/冲突", "climax": "高潮", "ending": "结尾"}
            lines.extend([f"- {label}：{structure.get(key) or '未明确'}" for key, label in structure_labels.items()])
        if summary.get("emotional_arc"):
            lines.extend(["", "## 情绪变化", *[f"- {item}" for item in summary["emotional_arc"]]])
        if summary.get("editing_and_expression"):
            lines.extend(["", "## 表达与剪辑", *[f"- {item}" for item in summary["editing_and_expression"]]])
        audience = summary.get("audience_and_intent") or {}
        if audience:
            lines.extend(["", "## 受众与意图", f"- 可能受众：{audience.get('likely_audience') or '未明确'}", f"- 传播意图：{audience.get('communication_intent') or '未明确'}", f"- 整体语气：{audience.get('tone') or '未明确'}"])
        if summary.get("reusable_elements"):
            lines.extend(["", "## 可复用要素", *[f"- {item}" for item in summary["reusable_elements"]]])
        comment_ideas = normalize_comment_ideas(summary.get("comment_ideas"))
        if comment_ideas:
            lines.extend(["", "## 神评论灵感", "", "以下为 AI 生成的评论候选，可按语气调整后使用。", ""])
            lines.extend(f"- {item['style']}：{item['text']}" for item in comment_ideas)
        content = "\n".join(lines)
        media_type = "text/markdown; charset=utf-8" if format == "md" else "text/plain; charset=utf-8"
        filename = f"{job['task_id']}-video-summary.{format}"
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": _attachment_header(filename)})


@app.post("/api/tasks", response_model=ApiResponse)
async def create_task(request: CreateTaskRequest):
    """
    创建下载任务

    根据任务类型创建对应的下载任务：
    - single: 单视频下载，需要提供 url
    - mix: 合集下载，需要提供 mix_id 和可选的 mix_name
    - mix_user: 用户合集下载（暂未实现）
    """
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")
    _require_valid_cookie()

    try:
        task_type = TaskType(request.type)

        if task_type == TaskType.SINGLE_VIDEO:
            if not request.url:
                raise HTTPException(status_code=400, detail="单视频下载需要提供 url")

            # 获取视频信息
            print(f"[DY-WEB] 创建单视频下载任务: {request.url}")

            # 先获取视频基本信息用于创建任务
            import asyncio
            from f2.apps.douyin.utils import AwemeIdFetcher

            aweme_id = await AwemeIdFetcher.get_aweme_id(request.url)
            if not aweme_id:
                raise HTTPException(status_code=400, detail="无法从URL中提取视频ID")

            # 检查是否已存在该视频的任务
            # 从数据库查询已存在的任务
            existing_result = await task_manager.get_tasks(
                status=None, task_type=TaskType.SINGLE, limit=100000
            )
            existing_tasks = existing_result.get("tasks", [])

            existing_task = None
            for task in existing_tasks:
                task_aweme_id = (
                    task.video_info.get("aweme_id") if task.video_info else None
                )
                if task_aweme_id == aweme_id:
                    existing_task = task
                    break

            if existing_task:
                # 如果任务已完成，提示用户已下载
                if existing_task.status == TaskStatus.COMPLETED.value:
                    return ApiResponse(
                        success=True,
                        data={
                            "task_id": existing_task.id,
                            "status": existing_task.status,
                            "video_name": existing_task.video_name,
                            "is_duplicate": True,
                        },
                        message=f"该视频已下载过：{existing_task.video_name}",
                    )
                # 如果任务还在进行中，直接返回现有任务
                elif existing_task.status in [
                    TaskStatus.PENDING.value,
                    TaskStatus.QUEUED.value,
                    TaskStatus.DOWNLOADING.value,
                ]:
                    return ApiResponse(
                        success=True,
                        data={
                            "task_id": existing_task.id,
                            "status": existing_task.status,
                            "video_name": existing_task.video_name,
                            "is_duplicate": True,
                        },
                        message=f"该视频已在下载队列中：{existing_task.video_name}",
                    )

            # 检查本地文件是否已存在
            if skill and hasattr(skill, "douyin_conf"):
                download_path = Path(skill.douyin_conf.get("path", "Download"))
                if download_path.exists():
                    matches = await asyncio.to_thread(
                        _find_media_candidates, download_path, "", "", aweme_id
                    )
                    if matches:
                        matched_file = matches[0]
                        return ApiResponse(
                            success=True,
                            data={
                                "file_path": str(matched_file),
                                "video_name": matched_file.name,
                                "matching_files": len(matches),
                                "is_duplicate": True,
                                "file_exists": True,
                            },
                            message=(
                                f"本地已有 {len(matches)} 个该视频的精确 ID 匹配文件"
                                if len(matches) > 1
                                else f"该视频文件已存在：{matched_file.name}"
                            ),
                        )

            # 获取下载路径
            download_path = (
                skill.douyin_conf.get("path", "Download") if skill else "Download"
            )

            # 创建任务（先不下载，由worker执行）
            task = await task_manager.create_single_task(
                aweme_id=aweme_id,
                video_info={"aweme_id": aweme_id, "desc": f"视频 {aweme_id}"},
                download_path=download_path,
            )

        elif task_type == TaskType.MIX_VIDEO:
            if not request.mix_id:
                raise HTTPException(status_code=400, detail="合集下载需要提供 mix_id")

            # 获取下载路径
            download_path = (
                skill.douyin_conf.get("path", "Download") if skill else "Download"
            )

            # 从数据库查询已存在的合集任务
            existing_result = await task_manager.get_tasks(
                status=None, task_type=TaskType.MIX, limit=100000
            )
            existing_tasks = existing_result.get("tasks", [])

            existing_mix_task = None
            for task in existing_tasks:
                task_mix_id = task.video_info.get("mix_id") if task.video_info else None
                if task_mix_id == request.mix_id and task.status in [
                    TaskStatus.PENDING.value,
                    TaskStatus.QUEUED.value,
                    TaskStatus.DOWNLOADING.value,
                ]:
                    existing_mix_task = task
                    break

            if existing_mix_task:
                # 如果合集任务还在进行中，提示用户
                return ApiResponse(
                    success=True,
                    data={
                        "task_id": existing_mix_task.id,
                        "status": existing_mix_task.status,
                        "video_name": existing_mix_task.video_name,
                        "is_duplicate": True,
                    },
                    message=f"该合集已在下载队列中：{existing_mix_task.video_name}",
                )

            task = await task_manager.create_mix_task(
                mix_id=request.mix_id,
                mix_name=request.mix_name or f"合集 {request.mix_id}",
                videos=None,
                download_path=download_path,
                max_counts=request.max_counts,
                interval=request.interval,
            )

        else:
            raise HTTPException(
                status_code=400, detail=f"不支持的任务类型: {request.type}"
            )

        return ApiResponse(
            success=True,
            data={"task_id": task.id, "status": task.status},
            message="任务创建成功",
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _schedule_task_from_record(task: Any) -> None:
    """Synchronous compatibility wrapper used by older callers/tests."""
    options = task.download_options or {}
    download_path = task.base_path or task.download_path or (skill.douyin_conf.get("path", "Download") if skill else "Download")
    if task.type == TaskType.SINGLE.value:
        aweme_id = task.video_info.get("aweme_id") if task.video_info else None
        if not aweme_id:
            raise HTTPException(status_code=409, detail="任务缺少视频 ID，无法继续")
        _schedule_download(task.id, _download_video_task(task.id, aweme_id, download_path))
        return
    if task.type == TaskType.MIX.value:
        mix_id = task.mix_id or (task.video_info or {}).get("mix_id")
        if not mix_id:
            raise HTTPException(status_code=409, detail="任务缺少合集 ID，无法继续")
        _schedule_download(task.id, _download_mix_task(task_id=task.id, mix_id=mix_id, mix_name=task.mix_name or f"合集 {mix_id}", download_path=download_path, max_counts=options.get("max_counts"), interval=options.get("interval", "all")))
        return
    raise HTTPException(status_code=409, detail="该任务类型暂不支持继续")


async def _schedule_task_from_record_async(task: Any) -> None:
    """Resume a persisted task without re-creating its database record."""
    options = task.download_options or {}
    max_counts = options.get("max_counts")
    interval = options.get("interval", "all")
    download_path = task.base_path or task.download_path or (
        skill.douyin_conf.get("path", "Download") if skill else "Download"
    )
    if task.type == TaskType.SINGLE.value:
        aweme_id = task.video_info.get("aweme_id") if task.video_info else None
        if not aweme_id:
            raise HTTPException(status_code=409, detail="任务缺少视频 ID，无法继续")
        coroutine = _download_video_task(task.id, aweme_id, download_path)
    elif task.type == TaskType.MIX.value:
        mix_id = task.mix_id or (task.video_info or {}).get("mix_id")
        if not mix_id:
            raise HTTPException(status_code=409, detail="任务缺少合集 ID，无法继续")
        coroutine = _download_mix_task(
            task_id=task.id,
            mix_id=mix_id,
            mix_name=task.mix_name or f"合集 {mix_id}",
            download_path=download_path,
            max_counts=max_counts,
            interval=interval,
        )
    elif task.type == TaskType.POST.value:
        # New scan selections are persisted as task_videos. Retry only the
        # failed/pending rows instead of re-scanning and creating duplicates.
        selected_videos = await task_manager.db.get_task_videos(task.id)
        if selected_videos:
            items = [{
                "aweme_id": video.aweme_id,
                "title": video.desc,
                "published_at": video.create_time,
            } for video in selected_videos if video.status != "downloaded"]
            if not items:
                raise HTTPException(status_code=409, detail="该批次没有可重试的作品")
            _schedule_download(task.id, _download_selected_post_task(task.id, items, download_path))
            return
        sec_user_id = task.mix_id or (task.video_info or {}).get("sec_user_id")
        if not sec_user_id:
            raise HTTPException(status_code=409, detail="任务缺少用户 ID，无法继续")
        coroutine = _download_user_posts_task(
            task_id=task.id,
            user_url=f"https://www.douyin.com/user/{sec_user_id}",
            download_path=download_path,
            max_counts=max_counts,
            interval=interval,
        )
    else:
        raise HTTPException(status_code=409, detail="该任务类型暂不支持继续")
    _schedule_download(task.id, coroutine)


@app.post("/api/tasks/{task_id}/retry", response_model=ApiResponse)
async def retry_task(task_id: str):
    """Retry failed items while preserving downloaded files and successful records."""
    _require_valid_cookie()
    try:
        task = await task_manager.retry_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在或无法重试")
        await _schedule_task_from_record_async(task)

        return ApiResponse(
            success=True,
            data={"task_id": task.id, "status": task.status},
            message="失败项已加入重试队列；已下载文件会被保留并跳过",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tasks/{task_id}/restart", response_model=ApiResponse)
async def restart_task(task_id: str):
    """Continue unfinished work without deleting or re-downloading completed files."""
    _require_valid_cookie()
    try:
        task = await task_manager.restart_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在或无法重新执行")
        await _schedule_task_from_record_async(task)

        return ApiResponse(
            success=True,
            data={
                "task_id": task.id,
                "status": task.status,
                "prev_downloaded": task.result.get("prev_downloaded", 0),
                "prev_skipped": task.result.get("prev_skipped", 0),
            },
            message="任务已继续执行；已下载文件会被保留并跳过",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tasks/{task_id}/continue", response_model=ApiResponse)
async def continue_task(task_id: str):
    """Explicit product-facing alias for continuing unfinished work."""
    return await restart_task(task_id)


@app.delete("/api/tasks/{task_id}", response_model=ApiResponse)
async def delete_task(task_id: str, delete_files: bool = False):
    """删除任务记录；可选择同时删除任务关联的本地视频。"""
    try:
        if "::" in task_id:
            parts = task_id.split("::")
            if len(parts) != 2 or not re.fullmatch(r"\d{5,32}", parts[1]):
                raise HTTPException(status_code=422, detail="视频条目标识无效")
            parent_id, aweme_id = parts
            parent = await task_manager.get_task(parent_id)
            if not parent or parent.type not in {"post", "mix", "mix_user"}:
                raise HTTPException(status_code=404, detail="作品所属任务不存在")
            if parent.status in {"pending", "queued", "downloading"}:
                raise HTTPException(status_code=409, detail="请先取消运行中的任务，再删除记录")
            videos = await task_manager.db.get_task_videos(parent_id)
            video = next((v for v in videos if v.aweme_id == aweme_id), None)
            if not video:
                raise HTTPException(status_code=404, detail="作品记录不存在")
            if delete_files:
                raise HTTPException(status_code=422, detail="批量作品请使用仅删除记录")
            children = await task_manager.db.fetch_all(
                "SELECT t.task_id FROM tasks t JOIN task_videos tv ON tv.task_id=t.task_id "
                "WHERE t.type='batch_item' AND t.mix_id=? AND tv.aweme_id=?", (parent_id, aweme_id))
            for (child_id,) in children:
                await task_manager.db.delete_task(child_id)
                if product_runtime:
                    await product_runtime.clear_checkpoints(child_id)
            await task_manager.db.execute("DELETE FROM task_videos WHERE task_id=? AND aweme_id=?", (parent_id, aweme_id))
            await task_manager.db.commit()
            remaining = [v for v in videos if v.aweme_id != aweme_id]
            if remaining:
                await task_manager.db.update_task_counts(parent_id, total=len(remaining),
                    downloaded=sum(v.status == "downloaded" for v in remaining),
                    skipped=sum(v.status == "skipped" for v in remaining),
                    failed=sum(v.status == "failed" for v in remaining))
            else:
                await task_manager.delete_task(parent_id)
            return ApiResponse(success=True, message="作品记录已删除，本地视频已保留")
        task = await task_manager.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status in {TaskStatus.PENDING.value, TaskStatus.QUEUED.value, TaskStatus.DOWNLOADING.value}:
            raise HTTPException(status_code=409, detail="请先取消运行中的任务，再删除记录")

        deleted_video_files = 0
        if delete_files:
            root = Path(skill.douyin_conf.get("path", "Download")).expanduser().resolve() if skill else Path("Download").resolve()
            videos = await task_manager.db.get_task_videos(task_id)
            video_refs = [video for video in videos if video.status == "downloaded"]
            paths: List[Path] = []
            for video in video_refs:
                try:
                    path = await asyncio.to_thread(
                        _find_task_video_file,
                        root,
                        [video.file_path],
                        [video.aweme_id],
                    )
                except FileNotFoundError:
                    continue
                except RuntimeError as exc:
                    if str(exc) == "VIDEO_FILE_AMBIGUOUS":
                        raise HTTPException(status_code=409, detail="找到多个同名视频，无法安全删除；请先整理下载目录") from exc
                    raise
                if path not in paths:
                    paths.append(path)

            if task.type == TaskType.SINGLE.value and not paths:
                aweme_id = (getattr(task, "video_info", None) or {}).get("aweme_id") or getattr(task, "mix_id", None)
                try:
                    path = await asyncio.to_thread(
                        _find_task_video_file,
                        root,
                        [getattr(task, "file_name", None), getattr(task, "download_path", None), getattr(task, "base_path", None)],
                        [aweme_id] if aweme_id else [],
                    )
                except FileNotFoundError:
                    path = None
                except RuntimeError as exc:
                    if str(exc) == "VIDEO_FILE_AMBIGUOUS":
                        raise HTTPException(status_code=409, detail="找到多个同名视频，无法安全删除；请先整理下载目录") from exc
                    raise
                if path:
                    paths.append(path)

            for path in paths:
                try:
                    await asyncio.to_thread(path.unlink)
                    deleted_video_files += 1
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise HTTPException(
                        status_code=500,
                        detail=f"本地视频删除失败，任务记录未删除；此前已删除 {deleted_video_files} 个视频文件",
                    ) from exc

        try:
            success = await task_manager.delete_task(task_id)
        except Exception as exc:
            if delete_files and deleted_video_files:
                raise HTTPException(
                    status_code=500,
                    detail=f"任务记录删除失败；已删除 {deleted_video_files} 个本地视频文件，请刷新记录状态",
                ) from exc
            raise
        if not success:
            if delete_files and deleted_video_files:
                raise HTTPException(
                    status_code=500,
                    detail=f"任务记录未删除；已删除 {deleted_video_files} 个本地视频文件，请刷新记录状态",
                )
            raise HTTPException(status_code=404, detail="任务不存在")

        if product_runtime:
            await product_runtime.clear_checkpoints(task_id)

        if delete_files:
            message = (
                f"任务记录已删除，并删除 {deleted_video_files} 个本地视频文件"
                if deleted_video_files
                else "任务记录已删除，未找到本地视频文件"
            )
            return ApiResponse(
                success=True,
                data={"deleted_video_files": deleted_video_files},
                message=message,
            )

        return ApiResponse(success=True, message="任务记录已删除，下载文件已保留")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tasks/{task_id}/cancel", response_model=ApiResponse)
async def cancel_task(task_id: str):
    """取消正在执行的任务"""
    try:
        running_task = running_download_tasks.get(task_id)
        if running_task and not running_task.done():
            running_task.cancel()
            try:
                await running_task
            except asyncio.CancelledError:
                pass
            return ApiResponse(
                success=True,
                message="任务已取消；已完成文件保留，可稍后继续未完成项",
            )
        success = await task_manager.cancel_task(task_id)
        if not success:
            raise HTTPException(status_code=400, detail="任务不存在或已完成，无法取消")

        return ApiResponse(
            success=True,
            message="任务已取消；已完成文件保留，可稍后继续未完成项",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _download_user_posts_task(
    task_id: str,
    user_url: str,
    download_path: str,
    max_counts: Optional[int] = None,
    interval: str = "all",
):
    """后台下载用户作品任务"""
    try:
        if skill is None:
            print(f"[DY-WEB] Skill未初始化，无法下载用户作品")
            await _mark_task_failed(
                task_id,
                stage="initialize",
                code="SKILL_NOT_READY",
                reason="下载服务尚未初始化",
                action="稍后刷新页面并继续未完成项",
            )
            return

        print(f"[DY-WEB] 开始后台下载用户作品任务: {task_id[:8]}..., url: {user_url}")

        # 更新任务状态为downloading
        await task_manager.update_task_status(task_id, "downloading")

        # 定义进度回调函数（作品级）
        def progress_callback(current: int, total: int, message: str):
            try:
                pct = int(current / max(total, 1) * 100)
                asyncio.create_task(_update_task_progress(task_id, pct, message))
            except RuntimeError:
                pass  # 事件循环已关闭，忽略进度更新

        # 定义文件级进度回调（来自 F2 进度桥，显示字节级下载进度）
        def file_progress_callback(percentage: int, current_file: str):
            try:
                asyncio.create_task(
                    _update_task_progress(task_id, percentage, current_file)
                )
            except RuntimeError:
                pass  # 事件循环已关闭，忽略进度更新

        # 调用skill下载用户作品
        result = await skill.download_user_posts(
            user_url=user_url,
            progress_callback=progress_callback,
            file_progress_callback=file_progress_callback,
            max_counts=max_counts,
            interval=interval,
        )

        # 更新任务结果
        if result.get("success"):
            # 保存视频信息到数据库
            videos = result.get("videos", [])
            if videos:
                try:
                    for video in videos:
                        await task_manager.db.add_video(
                            TaskVideoInfo(
                                id=0,
                                task_id=task_id,
                                aweme_id=video["aweme_id"],
                                desc=video["desc"],
                                nickname=video["nickname"],
                                sec_user_id=video.get("sec_user_id"),
                                create_time=video.get(
                                    "create_time", datetime.now().isoformat()
                                ),
                                status=video.get("status", "downloaded"),
                                file_path=None,
                                file_size=None,
                                error_message=None,
                                download_time=None,
                            )
                        )
                    print(f"[DY-WEB] 已保存 {len(videos)} 个视频信息到数据库")
                except Exception as e:
                    print(f"[DY-WEB] 保存视频信息失败: {e}")

            downloaded = int(result.get("downloaded", 0) or 0)
            skipped = int(result.get("skipped", 0) or 0)
            failed = int(result.get("failed", 0) or 0)
            await task_manager.db.update_task_counts(
                task_id,
                total=downloaded + skipped + failed,
                downloaded=downloaded,
                skipped=skipped,
                failed=failed,
            )
            if result.get("download_path"):
                await task_manager.db.update_task_result_path(
                    task_id, str(result["download_path"])
                )
            await task_manager.update_task_status(task_id, "completed")
            print(
                f"[DY-WEB] 用户作品下载成功: {task_id[:8]}..., 下载{result.get('downloaded', 0)}个, 跳过{result.get('skipped', 0)}个"
            )
        else:
            await _mark_task_failed(
                task_id,
                stage="download",
                code="USER_POSTS_DOWNLOAD_FAILED",
                reason=result.get("message", "用户作品下载失败"),
                action="检查主页、日期和登录状态后继续未完成项",
            )
            print(f"[DY-WEB] 用户作品下载失败: {task_id[:8]}...")

    except Exception as e:
        import traceback

        print(f"[DY-WEB] 用户作品下载任务异常: {task_id[:8]}..., 错误: {e}")
        print(traceback.format_exc())
        await _mark_task_failed(
            task_id,
            stage="download",
            code="USER_POSTS_DOWNLOAD_EXCEPTION",
            reason=str(e) or type(e).__name__,
            action="检查网络与登录状态后继续未完成项",
        )


async def _download_selected_post_task(task_id: str, items: List[Dict[str, Any]], download_path: str) -> None:
    """Download a user's explicitly selected works as one visible batch task."""
    if skill is None:
        await _mark_task_failed(task_id, stage="initialize", code="SKILL_NOT_READY", reason="下载服务尚未初始化", action="稍后刷新页面并重试")
        return
    total = len(items)
    downloaded = skipped = failed = 0
    await task_manager.update_task_status(task_id, "downloading")
    await task_manager.db.update_task_counts(task_id, total=total, downloaded=0, skipped=0, failed=0)
    for index, item in enumerate(items, 1):
        current = await task_manager.get_task(task_id)
        if current and current.status == TaskStatus.CANCELLED.value:
            return
        aweme_id = str(item.get("aweme_id") or "")
        title = str(item.get("title") or item.get("desc") or aweme_id)
        await _update_task_progress(task_id, int((index - 1) / max(total, 1) * 100), title)
        try:
            result = await _download_with_auto_retry(
                lambda: skill.download_single_video(
                    video_url=f"https://www.douyin.com/video/{aweme_id}",
                    download_path=download_path,
                ),
                task_id=task_id,
                progress_percentage=int((index - 1) / max(total, 1) * 100),
                label=f"第 {index}/{total} 条作品",
            )
            if result.get("success"):
                downloaded += 1
                await task_manager.update_video_status(task_id, aweme_id, "downloaded", file_path=result.get("file_name"))
                if result.get("video_name"):
                    await task_manager.db.execute("UPDATE task_videos SET desc=? WHERE task_id=? AND aweme_id=?", (result["video_name"], task_id, aweme_id))
                    await task_manager.db.commit()
            else:
                failed += 1
                await task_manager.update_video_status(task_id, aweme_id, "failed", error_message=_task_error("download", "BATCH_ITEM_FAILED", result.get("message", "下载失败"), "仅重试该批次失败项"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed += 1
            await task_manager.update_video_status(task_id, aweme_id, "failed", error_message=_task_error("download", "BATCH_ITEM_EXCEPTION", str(exc) or type(exc).__name__, "仅重试该批次失败项"))
        await task_manager.db.update_task_counts(task_id, downloaded=downloaded, skipped=skipped, failed=failed)
    await _update_task_progress(task_id, 100, "批次处理完成")
    await task_manager.update_task_status(task_id, "completed" if downloaded or not failed else "failed")


class DownloadUserPostsRequest(BaseModel):
    """下载用户作品请求模型"""

    user_url: str = Field(..., description="用户主页链接")
    max_counts: Optional[int] = Field(
        50, ge=1, le=500, description="最大下载数量，None表示明确确认全量下载"
    )
    interval: str = Field(
        "all", description="日期区间，格式如 '2024-01-01|2024-12-31' 或 'all'"
    )

    @field_validator("user_url")
    @classmethod
    def validate_user_url(cls, value: str) -> str:
        return _validate_douyin_url(value, require_user_page=True)

    @field_validator("interval")
    @classmethod
    def validate_user_interval(cls, value: str) -> str:
        return _validate_interval(value)

    class Config:
        json_schema_extra = {
            "example": {
                "user_url": "https://www.douyin.com/user/xxxxx",
                "max_counts": 50,
                "interval": "all",
            }
        }


class UserInfoRequest(BaseModel):
    """User-profile preflight request; performs real backend metadata lookup."""

    user_url: str

    @field_validator("user_url")
    @classmethod
    def validate_user_url(cls, value: str) -> str:
        return _validate_douyin_url(value, require_user_page=True)


async def _fetch_user_metadata(user_url: str) -> Dict[str, Any]:
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")
    from f2.apps.douyin.utils import SecUserIdFetcher

    sec_user_id = await SecUserIdFetcher.get_sec_user_id(user_url)
    if not sec_user_id:
        raise HTTPException(status_code=400, detail="无法从链接中提取用户 ID")

    kwargs = skill._build_kwargs({"cookie": await skill.get_cookie()})
    handler = skill._get_handler(kwargs)
    profile = await handler.fetch_user_profile(sec_user_id)
    profile_data = profile._to_dict() if hasattr(profile, "_to_dict") else {}
    nickname = getattr(profile, "nickname", None) or profile_data.get("nickname")
    if not nickname:
        raise HTTPException(status_code=502, detail="抖音未返回有效的用户资料")
    aweme_count = getattr(profile, "aweme_count", None)
    if aweme_count is None:
        aweme_count = profile_data.get("aweme_count")
    return {
        "sec_user_id": sec_user_id,
        "nickname": nickname,
        "aweme_count": aweme_count,
        "signature": getattr(profile, "signature", None)
        or profile_data.get("signature"),
        "verified": True,
    }


def _scan_upstream_status(exc: Exception) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        status = int(status)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _scan_error_details(exc: Exception, stage: str) -> Dict[str, Any]:
    stage_labels = {
        "profile": "验证作者主页",
        "posts": "读取作者作品列表",
        "pagination": "读取作品分页信息",
    }
    stage_label = stage_labels.get(stage, "读取作者作品列表")
    status_code = _scan_upstream_status(exc)
    exception_name = type(exc).__name__.lower()
    is_timeout = "timeout" in exception_name or "timedout" in exception_name
    is_connection_error = "connection" in exception_name or "proxy" in exception_name

    if status_code == 403:
        code = "SCAN_UPSTREAM_FORBIDDEN"
        message = f"{stage_label}接口返回 HTTP 403，上游拒绝了本次请求。"
        if stage == "posts":
            action = "主页资料预检已通过；403 本身无法区分登录态问题和访问限制。先稍后重试，若持续失败再刷新登录并检查网络。"
        else:
            action = "稍后重试；若持续失败，再刷新登录并检查网络。"
        retryable = False
    elif status_code == 401:
        code = "SCAN_UPSTREAM_UNAUTHORIZED"
        message = f"{stage_label}接口返回 HTTP 401，登录授权未通过。"
        action = "刷新抖音登录后重新解析。"
        retryable = False
    elif status_code == 429:
        code = "SCAN_UPSTREAM_RATE_LIMITED"
        message = f"{stage_label}接口返回 HTTP 429，请求暂时受限。"
        action = "等待一段时间后再重试，避免连续提交扫描。"
        retryable = True
    elif is_timeout:
        code = "SCAN_UPSTREAM_TIMEOUT"
        message = f"{stage_label}请求超时。"
        action = "检查网络连接，稍后重新解析。"
        retryable = True
    elif status_code is not None and status_code >= 500:
        code = "SCAN_UPSTREAM_UNAVAILABLE"
        message = f"{stage_label}接口暂时不可用（HTTP {status_code}）。"
        action = "稍后重新解析。"
        retryable = True
    elif is_connection_error:
        code = "SCAN_NETWORK_ERROR"
        message = f"{stage_label}时网络连接失败。"
        action = "检查网络或代理设置后重新解析。"
        retryable = True
    elif stage == "pagination":
        code = "SCAN_PAGINATION_INVALID"
        message = "作品列表返回了无效的分页游标，扫描已停止以避免重复请求。"
        action = "重新解析作品目录；如果仍失败，请稍后再试。"
        retryable = False
    else:
        code = "SCAN_UPSTREAM_FAILED"
        message = f"{stage_label}失败。"
        action = "检查网络连接后重新解析；如持续失败，请查看本地服务日志中的异常类型。"
        retryable = False

    return {
        "code": code,
        "stage": stage,
        "stage_label": stage_label,
        "status_code": status_code,
        "message": message,
        "action": action,
        "retryable": retryable,
    }


async def _sleep_user_post_scan(seconds: float) -> None:
    await asyncio.sleep(seconds)


@app.post("/api/user-info", response_model=ApiResponse)
async def get_user_info(request: UserInfoRequest):
    """Resolve a real Douyin user profile before allowing a batch task."""
    _require_valid_cookie()
    try:
        metadata = await _fetch_user_metadata(request.user_url)
        return ApiResponse(success=True, data=metadata, message="用户资料验证成功")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[DY-WEB] 用户主页预检失败 异常类型={type(e).__name__}")
        raise HTTPException(
            status_code=502,
            detail=_scan_error_details(e, "profile"),
        )


class CreateUserPostScanRequest(BaseModel):
    user_url: str


class UserPostScanDownloadRequest(BaseModel):
    selection_mode: str = "selected"
    selected_ids: List[str] = Field(default_factory=list)
    excluded_ids: List[str] = Field(default_factory=list)
    filters: Dict[str, Any] = Field(default_factory=dict)
    save_path: Optional[str] = None
    confirm: bool = False


def _scan_save_path(requested: Optional[str]) -> str:
    root = Path(skill.douyin_conf.get("path", "Download")).expanduser().resolve() if skill else Path("Download").resolve()
    candidate = Path(requested).expanduser() if requested else root
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "SAVE_PATH_OUTSIDE_ROOT", "message": "保存位置必须位于下载目录内。"}) from exc
    return str(resolved)


async def _run_user_post_scan(scan_id: str) -> None:
    """只读取主页作品元数据；扫描阶段绝不调用下载器。"""
    session = scan_manager.sessions.get(scan_id)
    if not session or skill is None:
        return
    session.status = "scanning"
    session.elapsed_ms = 0
    stage = "posts"
    try:
        cookie = await skill.get_cookie()
        configured_timeout = float(skill.douyin_conf.get("timeout", 10) or 10)
        scan_timeout = min(max(configured_timeout, 1), 60)
        page_counts = min(max(int(skill.douyin_conf.get("page_counts", 20) or 20), 1), 20)
        from f2.apps.douyin.crawler import DouyinCrawler
        from f2.apps.douyin.filter import UserPostFilter
        from f2.apps.douyin.model import UserPost

        try:
            resume_cursor = int(session.cursor or 0)
        except (TypeError, ValueError):
            resume_cursor = 0

        while True:
            transient_retries = 0
            while True:
                try:
                    kwargs = skill._build_kwargs({"cookie": cookie, "timeout": scan_timeout})
                    async with DouyinCrawler(kwargs) as crawler:
                        response = await crawler.fetch_user_post(
                            UserPost(
                                sec_user_id=session.sec_user_id,
                                max_cursor=resume_cursor,
                                count=page_counts,
                            )
                        )
                    page = UserPostFilter(response)
                    break
                except Exception as exc:
                    status_code = _scan_upstream_status(exc)
                    exception_name = type(exc).__name__.lower()
                    retryable = (
                        status_code in {408, 429}
                        or (status_code is not None and status_code >= 500)
                        or "timeout" in exception_name
                        or "connection" in exception_name
                    )
                    if not retryable or transient_retries >= 2:
                        raise
                    transient_retries += 1
                    session.warning = f"第 {session.pages_scanned + 1} 页暂时读取失败，正在退避重试（第 {transient_retries}/2 次）。"
                    await _sleep_user_post_scan(min(2 ** transient_retries, 4))

            raw_items = page._to_list() or []
            page_number = session.pages_scanned + 1
            for raw in raw_items:
                item = normalize_scan_item(raw, scan_id, page_number, fallback_author=session.nickname)
                if item and not any(existing.aweme_id == item.aweme_id for existing in session.items):
                    session.items.append(item)
            session.pages_scanned = page_number
            session.found_count = len(session.items)
            session.elapsed_ms = int((time.time() - session.created_at) * 1000)
            session.warning = None

            if session.total_hint is not None and session.found_count >= int(session.total_hint):
                break
            if not page.has_more:
                break

            next_cursor = page.max_cursor
            try:
                next_cursor = int(next_cursor)
            except (TypeError, ValueError) as exc:
                stage = "pagination"
                raise RuntimeError("上游返回了无效的分页游标") from exc
            if next_cursor == resume_cursor:
                stage = "pagination"
                raise RuntimeError("上游分页游标没有前进")
            resume_cursor = next_cursor
            session.cursor = str(resume_cursor)
            await _sleep_user_post_scan(0.5)

        if session.total_hint is not None and session.found_count < int(session.total_hint):
            session.status = "partial"
            session.warning = (
                f"已解析 {session.found_count} / {session.total_hint} 条；"
                "上游提前结束或未返回更多页面，请稍后重试。"
            )
        else:
            session.status = "completed"
        session.elapsed_ms = int((time.time() - session.created_at) * 1000)
    except asyncio.CancelledError:
        return
    except Exception as exc:
        session.status = "partial" if session.items else "failed"
        session.error = _scan_error_details(exc, stage)
        session.warning = f"已保留 {len(session.items)} 条已解析结果。" if session.items else None
        session.elapsed_ms = int((time.time() - session.created_at) * 1000)
        status_code = session.error.get("status_code")
        print(
            "[DY-WEB] 用户作品扫描失败 "
            f"阶段={stage} 异常类型={type(exc).__name__} "
            f"状态码={status_code if status_code is not None else '未知'}"
        )


@app.post("/api/user-post-scans", response_model=ApiResponse)
async def create_user_post_scan(request: CreateUserPostScanRequest):
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")
    _require_valid_cookie()
    try:
        normalized = _validate_douyin_url(request.user_url, require_user_page=True)
        metadata = await _fetch_user_metadata(normalized)
        session = await scan_manager.create_scan(
            normalized,
            metadata["sec_user_id"],
            metadata["nickname"],
            metadata.get("aweme_count"),
        )
        data = session.to_dict()
        data.update({"scan_token": session.token, "profile": metadata})
        scan_manager.schedule(session.scan_id, _run_user_post_scan(session.scan_id))
        return ApiResponse(success=True, data=data, message="已创建作品解析会话，尚未创建下载任务")
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        print(f"[DY-WEB] 用户作品扫描预检失败 异常类型={type(exc).__name__}")
        raise HTTPException(
            status_code=502,
            detail=_scan_error_details(exc, "profile"),
        ) from exc


@app.get("/api/user-post-scans/{scan_id}", response_model=ApiResponse)
async def get_user_post_scan(scan_id: str, x_scan_token: str = Header(..., alias="X-Scan-Token")):
    try:
        session = await scan_manager.get_scan(scan_id, x_scan_token)
        data = session.to_dict()
        data["profile"] = {"sec_user_id": session.sec_user_id, "nickname": session.nickname, "work_count": session.total_hint}
        return ApiResponse(success=True, data=data, message="获取解析状态成功")
    except ScanAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.get("/api/user-post-scans/{scan_id}/items", response_model=ApiResponse)
async def list_user_post_scan_items(
    scan_id: str,
    cursor: Optional[str] = None,
    limit: int = 24,
    keyword: str = "",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    kind: Optional[str] = None,
    history: Optional[str] = None,
    x_scan_token: str = Header(..., alias="X-Scan-Token"),
):
    try:
        result = await scan_manager.list_items(scan_id, x_scan_token, cursor, limit, keyword, date_from, date_to, kind, history)
        return ApiResponse(success=True, data=result, message="获取作品列表成功")
    except ScanAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.post("/api/user-post-scans/{scan_id}/cancel", response_model=ApiResponse)
async def cancel_user_post_scan(scan_id: str, x_scan_token: str = Header(..., alias="X-Scan-Token")):
    try:
        session = await scan_manager.cancel(scan_id, x_scan_token)
        return ApiResponse(success=True, data=session.to_dict(), message="作品解析已取消，已解析结果保留")
    except ScanAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.post("/api/user-post-scans/{scan_id}/downloads", response_model=ApiResponse)
async def submit_user_post_scan_download(scan_id: str, request: UserPostScanDownloadRequest, x_scan_token: str = Header(..., alias="X-Scan-Token")):
    if not request.confirm:
        raise HTTPException(status_code=422, detail={"code": "SELECTION_CONFIRMATION_REQUIRED", "message": "提交下载前需要明确确认所选作品。"})
    _require_valid_cookie()
    try:
        session = await scan_manager.get_scan(scan_id, x_scan_token)
        if session.status not in {"completed", "partial"}:
            raise HTTPException(status_code=409, detail={"code": "SCAN_NOT_READY", "message": "作品解析尚未完成。"})
        filters = request.filters or {}
        listed = await scan_manager.list_items(scan_id, x_scan_token, None, 10_000, filters.get("keyword", ""), filters.get("date_from"), filters.get("date_to"), filters.get("kind"), filters.get("history"))
        available = {item["aweme_id"]: item for item in listed["items"]}
        if request.selection_mode == "all":
            selected = [item for aweme_id, item in available.items() if aweme_id not in set(request.excluded_ids)]
        else:
            selected = [available[aweme_id] for aweme_id in dict.fromkeys(request.selected_ids) if aweme_id in available]
        if not selected:
            raise HTTPException(status_code=422, detail={"code": "SELECTION_EMPTY", "message": "请至少选择一个作品。"})
        if len(selected) > 200:
            raise HTTPException(status_code=422, detail={"code": "SELECTION_TOO_LARGE", "message": "单次最多选择 200 个作品。"})
        download_path = _scan_save_path(request.save_path)
        downloaded_rows = await task_manager.db.fetch_all("SELECT DISTINCT aweme_id FROM task_videos WHERE status='downloaded'")
        existing_ids = {str(row[0]) for row in downloaded_rows if row and row[0]}
        pending_selected = [item for item in selected if str(item.get("aweme_id")) not in existing_ids]
        if not pending_selected:
            return ApiResponse(success=True, data={"task_id": "", "task_ids": [], "selection_count": len(selected), "created_count": 0, "skipped_count": len(selected), "save_path": download_path}, message=f"所选 {len(selected)} 条作品均已下载，无需重复创建任务")
        # A selection is represented by one parent batch task. This keeps the
        # task desk readable and lets retry/cancel operate on the whole batch.
        task = await task_manager.create_post_task(
            sec_user_id=session.sec_user_id,
            user_name=f"{session.nickname} · 已选 {len(pending_selected)} 条",
            user_url=session.user_url or "",
            download_path=download_path,
        )
        for item in pending_selected:
            await task_manager.db.add_video(TaskVideoInfo(
                id=0,
                task_id=task.id,
                aweme_id=str(item["aweme_id"]),
                desc=item.get("title", ""),
                nickname=session.nickname,
                sec_user_id=session.sec_user_id,
                create_time=item.get("published_at", "") or datetime.now().isoformat(),
                status="pending",
                file_path=None,
                file_size=None,
                error_message=None,
                download_time=None,
            ))
        await task_manager.db.update_task_counts(task.id, total=len(pending_selected), downloaded=0, skipped=0, failed=0)
        _schedule_download(task.id, _download_selected_post_task(task.id, pending_selected, download_path))
        skipped_count = len(selected) - len(pending_selected)
        suffix = f"，跳过已下载 {skipped_count} 条" if skipped_count else ""
        return ApiResponse(success=True, data={"task_id": task.id, "task_ids": [task.id], "selection_count": len(selected), "created_count": 1, "skipped_count": skipped_count, "save_path": download_path}, message=f"已创建 1 个批次任务，包含 {len(pending_selected)} 条作品{suffix}")
    except ScanAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


class DistillSelection(BaseModel):
    mode: str = "selected"
    aweme_ids: List[str] = Field(default_factory=list)
    excluded_aweme_ids: List[str] = Field(default_factory=list)
    filters: Dict[str, Any] = Field(default_factory=dict)


class DistillRunCreateRequest(BaseModel):
    confirm: bool = False
    selection: DistillSelection = Field(default_factory=DistillSelection)
    save_path: Optional[str] = None
    force_reanalyze: bool = False
    stage_policy: Literal["preview", "full"] = "full"
    request_limit: Optional[int] = Field(default=None, ge=0, le=1000000)


class DistillResumeRequest(BaseModel):
    aweme_ids: List[str] = Field(default_factory=list)
    stage_policy: Optional[Literal["full"]] = None


def _distill_item_metadata(session: Any, item: Any) -> Dict[str, Any]:
    raw = item.to_dict() if hasattr(item, "to_dict") else dict(item)
    return {
        "aweme_id": raw.get("aweme_id"),
        "title": raw.get("desc") or raw.get("title") or "",
        "body": raw.get("desc") or raw.get("desc_raw") or "",
        "url": f"https://www.douyin.com/video/{raw.get('aweme_id')}",
        "published_at": raw.get("publish_time") or raw.get("published_at") or "",
        "kind": "image" if raw.get("media_type") == "image" or raw.get("kind") == "image" else "video",
        "duration_seconds": (float(raw["duration_ms"]) / 1000) if raw.get("duration_ms") else raw.get("duration_seconds"),
        "cover_url": raw.get("cover_url"),
        "nickname": session.nickname,
        "sec_user_id": session.sec_user_id,
    }


async def _distill_selected_scan_items(scan_id: str, token: str, selection: DistillSelection) -> tuple[Any, List[Dict[str, Any]]]:
    session = await scan_manager.get_scan(scan_id, token)
    if session.status not in {"completed", "partial"}:
        raise HTTPException(status_code=409, detail={"code": "SCAN_NOT_READY", "message": "作品解析尚未完成。"})
    all_items = list(session.items)
    if selection.mode in {"all", "all_filtered"}:
        filters = selection.filters or {}
        selected_items = all_items
        keyword = str(filters.get("keyword") or "").lower()
        if keyword:
            selected_items = [item for item in selected_items if keyword in item.desc.lower()]
        if filters.get("kind") and filters["kind"] != "all":
            selected_items = [item for item in selected_items if ("image" if item.media_type == "image" else "video") == filters["kind"]]
        if filters.get("date_from"):
            selected_items = [item for item in selected_items if (item.publish_time or "")[:10] >= filters["date_from"]]
        if filters.get("date_to"):
            selected_items = [item for item in selected_items if (item.publish_time or "")[:10] <= filters["date_to"]]
        excluded = set(selection.excluded_aweme_ids)
        selected_items = [item for item in selected_items if item.aweme_id not in excluded]
        selected_items.extend(item for item in all_items if item.aweme_id in set(selection.aweme_ids) and item not in selected_items)
    else:
        wanted = set(selection.aweme_ids)
        selected_items = [item for item in all_items if item.aweme_id in wanted]
    unique: Dict[str, Dict[str, Any]] = {}
    for item in selected_items:
        unique.setdefault(item.aweme_id, _distill_item_metadata(session, item))
    selected = list(unique.values())
    if not selected:
        raise HTTPException(status_code=422, detail={"code": "SELECTION_EMPTY", "message": "请至少选择一个作品。"})
    if len(selected) > 200:
        raise HTTPException(status_code=422, detail={"code": "SELECTION_TOO_LARGE", "message": "单次最多选择 200 个作品。"})
    return session, selected


@app.post("/api/distill-runs", response_model=ApiResponse)
async def create_distill_run(request: DistillRunCreateRequest, x_scan_token: str = Header(..., alias="X-Scan-Token"), scan_id: str = Query(...)):
    if not request.confirm:
        raise HTTPException(status_code=422, detail={"code": "DISTILL_CONFIRMATION_REQUIRED", "message": "开始蒸馏前需要确认选择范围。"})
    if distill_service is None or skill is None:
        raise HTTPException(status_code=503, detail="蒸馏服务尚未初始化")
    _require_valid_cookie()
    try:
        session, selected = await _distill_selected_scan_items(scan_id, x_scan_token, request.selection)
        root = Path(skill.douyin_conf.get("path", "Download")).expanduser().resolve()
        candidate = Path(request.save_path).expanduser() if request.save_path else root
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "SAVE_PATH_OUTSIDE_ROOT", "message": "保存位置必须位于下载目录内。"}) from exc
        resolved_save_path = str(candidate)
        run_id = str(uuid.uuid4())
        await task_manager.db.create_distill_run(run_id, sec_user_id=session.sec_user_id, nickname=session.nickname, user_url=session.user_url, options={"scan_id": scan_id, "save_path": resolved_save_path, "force_reanalyze": request.force_reanalyze, "stage_policy": request.stage_policy, "selection": request.selection.model_dump()}, synthesis_model="gpt-5.6-luna")
        items_for_db: List[Dict[str, Any]] = []
        for ordinal, meta in enumerate(selected, 1):
            item = {"ordinal": ordinal, "aweme_id": meta["aweme_id"], "metadata": meta}
            # Existing-file lookup must use the run's resolved directory too;
            # otherwise a custom save path is only honored for new downloads.
            item["_save_root"] = resolved_save_path
            if meta["kind"] != "image" and not request.force_reanalyze:
                task_id, path = await _distill_find_existing(item)
                reusable = await _distill_find_reusable_snapshot(meta["aweme_id"])
                if reusable and reusable.get("local_path"):
                    path = Path(reusable["local_path"])
                    task_id = reusable.get("task_id")
                    item.update(
                        task_id=task_id,
                        local_path=str(path),
                        source_size=reusable.get("source_size"),
                        source_mtime_ns=reusable.get("source_mtime_ns"),
                        download_status="reused",
                        transcript_job_id=reusable.get("transcript_job_id"),
                        summary_job_id=reusable.get("summary_job_id"),
                        original_transcript=reusable.get("original_transcript", ""),
                        polished_text=reusable.get("polished_text", ""),
                        summary=reusable.get("summary") if not _summary_invalid_reason(reusable.get("summary")) else None,
                        transcript_status="reused" if reusable.get("original_transcript") else "pending",
                        summary_status="reused" if reusable.get("summary") and not _summary_invalid_reason(reusable.get("summary")) else "pending",
                    )
                if task_id and path:
                    stat = path.stat()
                    item.update(task_id=task_id, local_path=str(path), source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns, download_status="reused")
                    task = await task_manager.get_task(task_id)
                    transcript = await task_manager.db.get_transcript_job(task_id=task_id) if task else None
                    summary = await task_manager.db.get_video_summary_job(task_id=task_id) if task else None
                    if transcript and transcript.get("status") == "completed" and _source_is_current(item, path, transcript.get("completed_at")):
                        item.update(transcript_job_id=transcript["job_id"], original_transcript=transcript.get("original_transcript", ""), polished_text=transcript.get("polished_text", ""), transcript_status="reused")
                    if summary and summary.get("status") == "completed" and _source_is_current(item, path, summary.get("completed_at")) and not _summary_invalid_reason(summary.get("summary")):
                        item.update(summary_job_id=summary["job_id"], summary=summary.get("summary"), summary_status="reused")
            items_for_db.append(item)
        await task_manager.db.add_distill_items(run_id, items_for_db)
        if usage_ledger:
            await usage_ledger.set_limit("distill", run_id, request.request_limit)
        distill_service.schedule(run_id)
        data = await task_manager.db.get_distill_run(run_id)
        video_count = sum(1 for item in selected if item["kind"] == "video")
        # 归纳分组按实际文本长度决定；启动前不虚构精确总调用数。
        minimum_calls = sum(2 for item in items_for_db if item["metadata"]["kind"] == "video" and item.get("transcript_status") != "reused")
        minimum_calls += sum(1 for item in items_for_db if item["metadata"]["kind"] == "video" and item.get("summary_status") != "reused")
        return ApiResponse(success=True, data={"run": data, "run_id": run_id, "selection_count": len(selected), "video_count": video_count, "image_text_count": sum(1 for item in selected if item["kind"] == "image"), "estimated_model_calls": None, "minimum_model_calls": minimum_calls,
            "estimate_note": "按未复用口播每条转写与整理各一次、总结至少一次估算下限；长视频分段、归纳、重试与备用通道另计，实际金额未知", "save_path": resolved_save_path}, message="博主内容蒸馏已加入队列")
    except ScanAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@app.get("/api/distill-runs", response_model=ApiResponse)
async def list_distill_runs(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    return ApiResponse(success=True, data={"items": await task_manager.db.list_distill_runs(limit, offset), "limit": limit, "offset": offset}, message="获取蒸馏历史成功")


@app.get("/api/distill-runs/{run_id}", response_model=ApiResponse)
async def get_distill_run(run_id: str, limit: int = Query(200, ge=1, le=10_000), offset: int = Query(0, ge=0), include_text: bool = Query(False)):
    run = await task_manager.db.get_distill_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail={"code": "DISTILL_RUN_NOT_FOUND", "message": "蒸馏运行不存在"})
    return ApiResponse(success=True, data={"run": run, "items": await task_manager.db.get_distill_items(run_id, limit=limit, offset=offset, include_text=include_text), "counts": await task_manager.db.count_distill_items(run_id)}, message="获取蒸馏进度成功")


@app.post("/api/distill-runs/{run_id}/resume", response_model=ApiResponse)
async def resume_distill_run(run_id: str, request: DistillResumeRequest = DistillResumeRequest()):
    if distill_service is None:
        raise HTTPException(status_code=503, detail="蒸馏服务尚未初始化")
    try:
        await distill_service.resume(run_id, request.aweme_ids, request.stage_policy)
        return ApiResponse(success=True, data=await task_manager.db.get_distill_run(run_id), message="蒸馏已继续")
    except DistillError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": exc.reason, "action": exc.action}) from exc


@app.post("/api/distill-runs/{run_id}/cancel", response_model=ApiResponse)
async def cancel_distill_run(run_id: str):
    if distill_service is None:
        raise HTTPException(status_code=503, detail="蒸馏服务尚未初始化")
    if not await task_manager.db.get_distill_run(run_id):
        raise HTTPException(status_code=404, detail={"code": "DISTILL_RUN_NOT_FOUND", "message": "蒸馏运行不存在"})
    await task_manager.db.update_distill_run(run_id, status="cancelling")
    await distill_service.cancel(run_id)
    return ApiResponse(success=True, data=await task_manager.db.get_distill_run(run_id), message="蒸馏已取消")


@app.get("/api/distill-runs/{run_id}/markdown")
async def download_distill_markdown(run_id: str, format: str = Query("md", pattern="^(md|zip)$")):
    run = await task_manager.db.get_distill_run(run_id)
    if not run or not run.get("output_path"):
        raise HTTPException(status_code=404, detail="蒸馏总纲尚未生成")
    output = Path(run["output_path"]).resolve()
    root = Path(skill.douyin_conf.get("path", "Download")).expanduser().resolve() if skill else output.parent
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="产物路径不在下载目录内") from exc
    if not output.exists():
        raise HTTPException(status_code=404, detail="蒸馏总纲文件不存在")
    if format == "zip":
        from .distill_service import DistillMarkdown
        package = DistillMarkdown.package(output.parent, output)
        return FileResponse(package, media_type="application/zip", filename=package.name)
    return FileResponse(output, media_type="text/markdown; charset=utf-8", filename=output.name)


def _distill_inline_html(value: str, link_rewrites: Optional[Dict[str, str]] = None) -> str:
    """Escape generated Markdown and allow only safe, local artifact links."""
    if link_rewrites:
        for source, target in link_rewrites.items():
            value = value.replace(f"]({source})", f"]({target})")
    text = html_escape(value, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\((/api/distill-runs/[A-Za-z0-9-]+(?:/items/[A-Za-z0-9_]+)?/view|https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def _distill_markdown_to_html(markdown_text: str, title: str, link_rewrites: Optional[Dict[str, str]] = None) -> str:
    """Small dependency-free renderer for our controlled Markdown artifacts."""
    rendered: List[str] = []
    in_list = False
    in_details = False
    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        if line == "<details>":
            if in_list:
                rendered.append("</ul>"); in_list = False
            rendered.append("<details>"); in_details = True; continue
        if line == "</details>":
            if in_list:
                rendered.append("</ul>"); in_list = False
            rendered.append("</details>"); in_details = False; continue
        if line.startswith("<summary>") and line.endswith("</summary>"):
            rendered.append(f"<summary>{_distill_inline_html(line[9:-10], link_rewrites)}</summary>"); continue
        if not line.strip():
            if in_list:
                rendered.append("</ul>"); in_list = False
            continue
        if line.startswith("### "):
            if in_list: rendered.append("</ul>"); in_list = False
            rendered.append(f"<h3>{_distill_inline_html(line[4:], link_rewrites)}</h3>"); continue
        if line.startswith("## "):
            if in_list: rendered.append("</ul>"); in_list = False
            rendered.append(f"<h2>{_distill_inline_html(line[3:], link_rewrites)}</h2>"); continue
        if line.startswith("# "):
            if in_list: rendered.append("</ul>"); in_list = False
            rendered.append(f"<h1>{_distill_inline_html(line[2:], link_rewrites)}</h1>"); continue
        if line.startswith("- "):
            if not in_list:
                rendered.append("<ul>"); in_list = True
            rendered.append(f"<li>{_distill_inline_html(line[2:], link_rewrites)}</li>"); continue
        if in_list:
            rendered.append("</ul>"); in_list = False
        if line.startswith("> "):
            rendered.append(f"<blockquote>{_distill_inline_html(line[2:], link_rewrites)}</blockquote>"); continue
        rendered.append(f"<p>{_distill_inline_html(line, link_rewrites)}</p>")
    if in_list: rendered.append("</ul>")
    body = "\n".join(rendered)
    safe_title = html_escape(title, quote=True)
    return f"""<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{safe_title}</title>
<style>body{{margin:0;background:#f7f4ee;color:#172536;font:15px/1.75 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}}main{{max-width:900px;margin:0 auto;padding:28px 24px 64px;background:#fff;min-height:100vh;box-sizing:border-box}}h1{{font-size:30px;line-height:1.25;margin:0 0 24px}}h2{{margin-top:30px;padding-bottom:8px;border-bottom:1px solid #dce5e7;font-size:21px}}h3{{margin-top:22px;font-size:17px}}p{{margin:10px 0}}ul{{padding-left:24px}}blockquote{{margin:14px 0;padding:9px 14px;border-left:3px solid #19c4bd;background:#eef8f6;color:#526475}}a{{color:#087f86;text-decoration:none;border-bottom:1px solid #9edcd8}}a:hover{{color:#d65f4e}}code{{padding:2px 5px;border-radius:4px;background:#eef1f3;font-size:.9em}}details{{margin:14px 0;padding:10px 14px;border:1px solid #dce5e7;border-radius:8px;background:#fbfcfb}}summary{{cursor:pointer;font-weight:700}}</style></head><body><main>{body}</main></body></html>"""


async def _distill_artifact_file(run_id: str, aweme_id: Optional[str] = None) -> tuple[Dict[str, Any], Path]:
    run = await task_manager.db.get_distill_run(run_id)
    if not run or not run.get("output_path"):
        raise HTTPException(status_code=404, detail="蒸馏总纲尚未生成")
    output = Path(run["output_path"]).resolve()
    root = Path(skill.douyin_conf.get("path", "Download")).expanduser().resolve() if skill else output.parent
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="产物路径不在下载目录内") from exc
    if aweme_id is None:
        return run, output
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", aweme_id):
        raise HTTPException(status_code=400, detail="非法作品 ID")
    item = await task_manager.db.get_distill_item(run_id, aweme_id)
    if not item:
        raise HTTPException(status_code=404, detail="蒸馏条目不存在")
    item_file = output.parent / "口播文案" / DistillMarkdown._item_filename(item)
    try:
        item_file.resolve().relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="条目产物路径不在下载目录内") from exc
    return run, item_file


@app.get("/api/distill-runs/{run_id}/view", response_class=HTMLResponse)
async def view_distill_artifact(run_id: str):
    run, output = await _distill_artifact_file(run_id)
    if not output.exists():
        raise HTTPException(status_code=404, detail="蒸馏总纲文件不存在")
    items = await task_manager.db.get_distill_items(run_id, limit=10_000, include_text=False)
    rewrites = {f"口播文案/{DistillMarkdown._item_filename(item)}": f"/api/distill-runs/{run_id}/items/{item['aweme_id']}/view" for item in items}
    markdown_text = output.read_text(encoding="utf-8")
    # Legacy runs created before per-item links existed still get a clickable
    # index in the browser view without rewriting the stored Markdown file.
    if items and not re.search(r"/api/distill-runs/[A-Za-z0-9-]+/items/", markdown_text):
        markdown_text += "\n\n## 逐条视频文件\n" + "\n".join(
            f"- [{DistillMarkdown._item_filename(item)}](/api/distill-runs/{run_id}/items/{item['aweme_id']}/view)"
            for item in items
        )
    return HTMLResponse(_distill_markdown_to_html(markdown_text, f"{run.get('nickname') or '博主'} · 蒸馏总纲", rewrites))


@app.get("/api/distill-runs/{run_id}/items/{aweme_id}/view", response_class=HTMLResponse)
async def view_distill_item(run_id: str, aweme_id: str):
    run, output = await _distill_artifact_file(run_id, aweme_id)
    if output.exists():
        markdown_text = output.read_text(encoding="utf-8")
    else:
        # Older runs may have a valid DB snapshot but no per-item file. Render
        # that snapshot read-only so historical links do not dead-end.
        item = await task_manager.db.get_distill_item(run_id, aweme_id)
        if not item:
            raise HTTPException(status_code=404, detail="逐条蒸馏文件尚未生成")
        markdown_text = f"# {run.get('nickname') or '博主'} · {DistillMarkdown._source_label(item)}\n\n{DistillMarkdown(output.parent)._appendix(item, split=True)}\n"
    return HTMLResponse(_distill_markdown_to_html(markdown_text, f"{run.get('nickname') or '博主'} · {aweme_id}"))


@app.post("/api/download-user-posts", response_model=ApiResponse)
async def download_user_posts(request: DownloadUserPostsRequest):
    """
    下载用户主页作品（异步任务模式）

    Args:
        request: 包含用户主页URL和下载选项的请求

    Returns:
        创建的任务信息
    """
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")
    _require_valid_cookie()

    try:
        metadata = await _fetch_user_metadata(request.user_url)
        sec_user_id = metadata["sec_user_id"]
        user_nickname = metadata["nickname"]

        # 检查是否已存在该用户的作品下载任务（在下载中或队列中）
        print(f"[DY-WEB] 检查用户作品任务重复: sec_user_id={sec_user_id}")

        # 从数据库查询已存在的用户作品任务
        existing_result = await task_manager.get_tasks(
            status=None, task_type=TaskType.POST, limit=100000
        )
        existing_tasks = existing_result.get("tasks", [])

        existing_post_task = None
        for task in existing_tasks:
            task_sec_user_id = (
                task.video_info.get("sec_user_id") if task.video_info else None
            )
            if task_sec_user_id == sec_user_id and task.status in [
                TaskStatus.PENDING.value,
                TaskStatus.QUEUED.value,
                TaskStatus.DOWNLOADING.value,
            ]:
                existing_post_task = task
                print(f"[DY-WEB] [OK] 找到进行中的用户作品任务: {task.video_name}")
                break

        if existing_post_task:
            # 如果任务还在进行中，提示用户
            return ApiResponse(
                success=True,
                data={
                    "task_id": existing_post_task.id,
                    "status": existing_post_task.status,
                    "video_name": existing_post_task.video_name,
                    "is_duplicate": True,
                },
                message=f"该用户的作品下载已在队列中：{existing_post_task.video_name}",
            )

        # 获取下载路径
        download_path = (
            skill.douyin_conf.get("path", "Download") if skill else "Download"
        )

        # 创建用户作品下载任务（异步保存到数据库）
        print(
            f"[DY-WEB] 创建用户作品任务: sec_user_id={sec_user_id}, name={user_nickname}"
        )
        task = await task_manager.create_post_task(
            sec_user_id=sec_user_id,
            user_name=user_nickname,
            user_url=request.user_url,
            max_counts=request.max_counts,
            interval=request.interval,
            download_path=download_path,
        )

        # 启动后台下载任务
        _schedule_download(
            task.id,
            _download_user_posts_task(
                task_id=task.id,
                user_url=request.user_url,
                download_path=download_path,
                max_counts=request.max_counts,
                interval=request.interval,
            )
        )

        return ApiResponse(
            success=True,
            data={
                "task_id": task.id,
                "status": task.status,
                "video_name": task.video_name,
                "message": "用户作品下载任务已创建",
            },
            message=f"用户作品下载任务已创建：{task.video_name}",
        )

    except HTTPException:
        raise
    except Exception as e:
        import traceback

        print(f"[DY-WEB] 下载用户作品错误: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# 前端静态文件服务
frontend_path = Path(__file__).parent.parent.parent / "frontend" / "dist"
if frontend_path.exists():
    # 挂载 /assets 路径提供静态资源
    app.mount(
        "/assets", StaticFiles(directory=str(frontend_path / "assets")), name="assets"
    )
    # 挂载 /app 路径提供前端应用（html=true 支持前端路由）
    app.mount(
        "/app", StaticFiles(directory=str(frontend_path), html=True), name="frontend"
    )


@app.get("/api/download/file/{filename}")
async def download_file(
    filename: str,
    subpath: Optional[str] = None,
    aweme_id: Optional[str] = None,
    preview: bool = False,
):
    """
    下载已下载的视频文件

    Args:
        filename: 文件名
        subpath: 子目录路径（可选）
        aweme_id: 视频ID（可选，用于智能查找文件）

    Returns:
        文件下载响应
    """
    if skill is None:
        raise HTTPException(status_code=503, detail="Skill未初始化")

    try:
        # 获取下载根目录
        download_root = Path(skill.douyin_conf.get("path", "Download"))

        if not download_root.exists():
            raise HTTPException(status_code=404, detail="下载目录不存在")

        # 构建完整路径
        if subpath:
            file_path = download_root / subpath / filename
        else:
            file_path = download_root / filename

        # 安全检查：确保文件路径在下载目录内
        try:
            file_path.resolve().relative_to(download_root.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="非法的文件路径")

        # If the direct path is absent, use deterministic, anchored matching.
        # Never use fuzzy ``*fragment*`` globbing or pick an arbitrary first file.
        if not file_path.exists():
            search_root = (download_root / subpath) if subpath else download_root
            try:
                search_root.resolve().relative_to(download_root.resolve())
            except ValueError:
                raise HTTPException(status_code=403, detail="非法的搜索路径")

            requested_name = Path(filename).name
            requested_stem = Path(requested_name).stem
            safe_aweme_id = (aweme_id or "").strip()
            if safe_aweme_id and not re.fullmatch(r"\d{5,32}", safe_aweme_id):
                raise HTTPException(status_code=400, detail="非法的视频 ID")

            candidates = await asyncio.to_thread(
                _find_media_candidates, search_root, requested_name, requested_stem, safe_aweme_id
            )

            if len(candidates) > 1:
                raise HTTPException(
                    status_code=409,
                    detail="找到多个匹配文件，请从任务记录使用精确文件名下载",
                )
            if candidates:
                file_path = candidates[0]

        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"文件不存在: {filename}")

        if not file_path.is_file():
            raise HTTPException(status_code=400, detail="不是有效的文件")

        # 按扩展名返回正确的媒体类型
        media_types = {
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".mkv": "video/x-matroska",
            ".flv": "video/x-flv",
            ".webp": "image/webp",
            ".jpeg": "image/jpeg",
            ".jpg": "image/jpeg",
            ".png": "image/png",
        }
        return FileResponse(
            path=str(file_path),
            filename=file_path.name,
            media_type=media_types.get(file_path.suffix.lower(), "application/octet-stream"),
            content_disposition_type="inline" if preview else "attachment",
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="抖音视频下载器 Web 服务")
    parser.add_argument("--port", type=int, default=8080, help="服务端口")
    parser.add_argument(
        "--host",
        type=str,
        choices=["127.0.0.1", "0.0.0.0"],
        default="127.0.0.1",
        help="服务地址；默认仅本机，局域网模式可使用 0.0.0.0",
    )
    args = parser.parse_args()
    configure_origin_port(args.port)

    if args.host != "127.0.0.1" and not LAN_MODE:
        parser.error("只有设置 DOUYIN_LAN_MODE=1 时才能绑定非回环地址")

    # 在执行启动恢复前占用监听端口；重复进程不得改写正在运行的作业。
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((args.host, args.port))
        listener.listen(128)
        listener.setblocking(False)
    except OSError:
        listener.close()
        parser.exit(1, f"端口 {args.port} 已被占用或无法监听，未执行任务恢复。\n")
    print(f"[DY-WEB] 启动服务: http://{args.host}:{args.port}")
    print(f"[DY-WEB] API文档: http://{args.host}:{args.port}/docs")
    try:
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port))
        server.run(sockets=[listener])
    finally:
        listener.close()
