"""本地用户主页作品元数据扫描会话。

扫描阶段只保存作品元数据，不下载媒体；下载必须由用户在结果页明确选择后提交。
"""

from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit


class ScanAccessError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass
class ScanItem:
    scan_id: str
    aweme_id: str
    desc: str
    cover_url: Optional[str]
    publish_time: Optional[str]
    media_type: str
    author: str
    duration_ms: Optional[int]
    local_exists: bool = False
    historical_task: bool = False
    page_number: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScanSession:
    scan_id: str
    token: str
    user_url: str
    sec_user_id: str
    nickname: str
    status: str = "pending"
    found_count: int = 0
    total_hint: Optional[int] = None
    pages_scanned: int = 0
    cursor: Optional[str] = None
    elapsed_ms: int = 0
    warning: Optional[str] = None
    error: Optional[Dict[str, Any]] = None
    created_at: float = 0.0
    items: List[ScanItem] = None  # type: ignore[assignment]

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload.pop("token", None)
        payload.pop("items", None)
        payload["expires_at"] = datetime.fromtimestamp(self.created_at + 7200).astimezone().isoformat()
        return payload


def _local_iso_from_timestamp(value: float) -> str:
    """Format Douyin epoch timestamps in the user's local timezone."""
    return datetime.fromtimestamp(value).astimezone().isoformat()


def _safe_cover(value: Any) -> Optional[str]:
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if parsed.scheme in {"http", "https"} and parsed.hostname else None


def normalize_scan_item(raw: Dict[str, Any], scan_id: str, page_number: int, fallback_author: str = "", **kwargs: Any) -> Optional[ScanItem]:
    aweme_id = str(raw.get("aweme_id") or "")
    if not aweme_id.isdigit() or not 6 <= len(aweme_id) <= 30:
        return None
    images = raw.get("images")
    media_type = "image" if str(raw.get("aweme_type")) == "68" or images else "video"
    cover = raw.get("cover") or raw.get("animated_cover") or images
    if not cover:
        video = raw.get("video") or {}
        cover = (video.get("origin_cover") or {}).get("url_list") or (video.get("cover") or {}).get("url_list")
    duration = raw.get("video_duration", raw.get("duration"))
    try:
        duration_ms = int(duration) if duration not in (None, "") else None
    except (TypeError, ValueError):
        duration_ms = None
    published = raw.get("create_time")
    if isinstance(published, (int, float)):
        published = _local_iso_from_timestamp(float(published))
    return ScanItem(
        scan_id=scan_id,
        aweme_id=aweme_id,
        desc=str(raw.get("desc") or raw.get("desc_raw") or ""),
        cover_url=_safe_cover(cover),
        publish_time=str(published) if published else None,
        media_type=media_type,
        author=str(raw.get("nickname") or fallback_author or ""),
        duration_ms=duration_ms,
        local_exists=bool(kwargs.get("local_exists", False)),
        historical_task=bool(kwargs.get("historical_task", False)),
        page_number=page_number,
    )


class ScanManager:
    def __init__(self, ttl_seconds: int = 7200):
        self.ttl_seconds = ttl_seconds
        self.sessions: Dict[str, ScanSession] = {}
        self.tasks: Dict[str, asyncio.Task[Any]] = {}

    async def initialize(self) -> None:
        self.cleanup_expired()

    async def shutdown(self) -> None:
        for task in list(self.tasks.values()):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()

    def cleanup_expired(self) -> None:
        now = time.time()
        for scan_id, session in list(self.sessions.items()):
            if session.created_at and now - session.created_at > self.ttl_seconds:
                task = self.tasks.pop(scan_id, None)
                if task and not task.done():
                    task.cancel()
                self.sessions.pop(scan_id, None)

    async def create_scan(
        self,
        user_url: str,
        sec_user_id: str,
        nickname: str = "",
        total_hint: Optional[int] = None,
    ) -> ScanSession:
        self.cleanup_expired()
        session = ScanSession(
            str(uuid.uuid4()),
            secrets.token_urlsafe(32),
            user_url,
            sec_user_id,
            nickname,
            total_hint=total_hint,
            created_at=time.time(),
            items=[],
        )
        self.sessions[session.scan_id] = session
        return session

    def _get(self, scan_id: str, token: str) -> ScanSession:
        self.cleanup_expired()
        session = self.sessions.get(scan_id)
        if not session:
            raise ScanAccessError(404, "SCAN_NOT_FOUND", "扫描会话不存在或已过期。")
        if not secrets.compare_digest(session.token, token):
            raise ScanAccessError(403, "SCAN_ACCESS_DENIED", "无权访问该扫描会话。")
        return session

    async def get_scan(self, scan_id: str, token: str) -> ScanSession:
        return self._get(scan_id, token)

    async def cancel(self, scan_id: str, token: str) -> ScanSession:
        session = self._get(scan_id, token)
        task = self.tasks.get(scan_id)
        if task and not task.done():
            task.cancel()
        session.status = "cancelled"
        session.elapsed_ms = int((time.time() - session.created_at) * 1000)
        return session

    def schedule(self, scan_id: str, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks[scan_id] = task
        task.add_done_callback(lambda _task: self.tasks.pop(scan_id, None))

    async def list_items(self, scan_id: str, token: str, cursor: Optional[str], limit: int, keyword: str = "", date_from: Optional[str] = None, date_to: Optional[str] = None, kind: Optional[str] = None, history: Optional[str] = None) -> Dict[str, Any]:
        session = self._get(scan_id, token)
        limit = max(1, min(int(limit or 24), 10_000))
        # Keep ordering deterministic while the producer appends pages. Cursor
        # values are aweme IDs (not mutable list offsets), so a refresh cannot
        # silently repeat or skip an item when more metadata arrives.
        items = sorted(session.items, key=lambda item: (item.publish_time or "", item.aweme_id), reverse=True)
        if keyword:
            items = [item for item in items if keyword.lower() in item.desc.lower()]
        if kind and kind != "all":
            items = [item for item in items if item.media_type == kind]
        if history == "downloaded":
            items = [item for item in items if item.historical_task or item.local_exists]
        elif history == "not_downloaded":
            items = [item for item in items if not item.historical_task and not item.local_exists]
        if date_from:
            items = [item for item in items if (item.publish_time or "")[:10] >= date_from]
        if date_to:
            items = [item for item in items if (item.publish_time or "")[:10] <= date_to]
        start = 0
        if cursor:
            for index, item in enumerate(items):
                if item.aweme_id == cursor:
                    start = index + 1
                    break
            else:
                # Accept legacy numeric cursors issued by older frontends.
                try:
                    start = max(0, int(cursor))
                except ValueError:
                    start = 0
        page = items[start:start + limit]
        next_cursor = page[-1].aweme_id if start + limit < len(items) and page else None
        return {
            "items": [{
                "aweme_id": item.aweme_id,
                "title": item.desc,
                "published_at": item.publish_time or "",
                "kind": item.media_type,
                "duration_seconds": round(item.duration_ms / 1000, 2) if item.duration_ms else None,
                "cover_url": item.cover_url,
                "downloaded": item.historical_task or item.local_exists,
            } for item in page],
            "cursor": cursor,
            "next_cursor": next_cursor,
            "matched_count": len(items),
            "total_count": len(session.items),
        }


scan_manager = ScanManager()
