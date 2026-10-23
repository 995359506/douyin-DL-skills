"""主页批量下载的本地持久化队列，不在入队时请求抖音。"""

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiosqlite

try:
    from .logger import get_logger
    from .usage_ledger import model_request_context, get_default_ledger
except ImportError:
    from logger import get_logger
    from usage_ledger import model_request_context, get_default_ledger


logger = get_logger()


PrepareItem = Callable[[str, str, str], Awaitable[str]]
ExecuteItem = Callable[[str, str, bool], Awaitable[Dict[str, Any]]]
ReuseItem = Callable[[str, str, str, bool], Awaitable[Optional[str]]]


class PersistentDownloadQueue:
    """单进程串行下载，冷却时间与条目状态一起保存。"""

    def __init__(
        self,
        db_path: str,
        prepare_item: PrepareItem,
        execute_item: ExecuteItem,
        *,
        reuse_item: Optional[ReuseItem] = None,
        now: Callable[[], float] = time.time,
        interval_seconds: int = 30,
    ) -> None:
        if interval_seconds < 30:
            raise ValueError("批量下载间隔不能少于 30 秒")
        self.db_path = str(db_path)
        self.prepare_item = prepare_item
        self.execute_item = execute_item
        self.reuse_item = reuse_item
        self.now = now
        self.interval_seconds = interval_seconds
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._process_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._worker: Optional[asyncio.Task] = None
        self._reuse_cursor = (0, -1)
        self._reuse_scan_done = False

    async def initialize(self) -> None:
        """追加队列表，并保守恢复上一次中断的条目。"""
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA busy_timeout = 10000")
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS download_queue_batches (
                batch_id TEXT PRIMARY KEY,
                client_request_id TEXT NOT NULL UNIQUE,
                author TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                auto_summary INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS download_queue_items (
                batch_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                aweme_id TEXT NOT NULL,
                task_id TEXT,
                status TEXT NOT NULL,
                PRIMARY KEY (batch_id, aweme_id)
            );
            CREATE TABLE IF NOT EXISTS download_queue_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                not_before REAL NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO download_queue_state (singleton, not_before) VALUES (1, 0);
        """)
        interrupted = await self._fetchone(
            "SELECT COUNT(*) AS count FROM download_queue_items WHERE status = 'running'"
        )
        if interrupted["count"]:
            await self._set_cooldown()
            await self._db.execute("""
                UPDATE download_queue_items
                SET status = CASE WHEN batch_id IN (
                    SELECT batch_id FROM download_queue_batches WHERE status = 'cancelled'
                ) THEN 'cancelled' ELSE 'queued' END
                WHERE status = 'running'
            """)
        await self._db.execute(
            "UPDATE download_queue_batches SET status = 'queued' WHERE status = 'running'"
        )
        await self._db.commit()
        self._reset_reuse_scan()

    async def start(self) -> None:
        await self.initialize()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def shutdown(self) -> None:
        """中断当前协程，保存冷却；下次启动复用同一任务编号。"""
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        # ponytail: 单 worker；同进程 process_next 共用锁，不允许并行出队。
        async with self._process_lock:
            async with self._lock:
                if self._db is not None:
                    await self._db.commit()
                    await self._db.close()
                    self._db = None

    async def create(
        self,
        video_ids: List[str],
        author: str = "",
        profile_id: str = "",
        client_request_id: str = "",
        auto_summary: bool = True,
        request_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """仅写本地数据库；相同请求编号始终返回同一批次。"""
        if not video_ids or len(video_ids) > 20000:
            raise ValueError("请选择 1 至 20000 条视频")
        if any(not isinstance(value, str) or not re.fullmatch(r"[0-9]{5,32}", value) for value in video_ids):
            raise ValueError("视频编号格式不正确")
        if request_limit is not None and (isinstance(request_limit, bool) or not isinstance(request_limit, int) or request_limit < 0):
            raise ValueError("调用次数上限必须是非负整数")
        if len(author) > 80 or len(profile_id) > 256 or len(client_request_id) > 128:
            raise ValueError("作者或请求编号过长")
        unique_ids = list(dict.fromkeys(video_ids))
        request_id = client_request_id or str(uuid.uuid4())
        async with self._lock:
            self._require_db()
            existing = await self._fetchone(
                "SELECT * FROM download_queue_batches WHERE client_request_id = ?", (request_id,)
            )
            if existing is not None:
                previous_ids = [row["aweme_id"] for row in await self._fetchall(
                    "SELECT aweme_id FROM download_queue_items WHERE batch_id = ? ORDER BY position",
                    (existing["batch_id"],),
                )]
                if (unique_ids != previous_ids or author != existing["author"]
                        or profile_id != existing["profile_id"]
                        or bool(auto_summary) != bool(existing["auto_summary"])):
                    raise ValueError("同一提交编号不能用于不同的批量下载内容")
                return await self._view(existing["batch_id"])
            batch_id = str(uuid.uuid4())
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                await self._db.execute("""
                    INSERT INTO download_queue_batches
                    (batch_id, client_request_id, author, profile_id, auto_summary, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'queued', ?)
                """, (batch_id, request_id, author, profile_id, int(auto_summary),
                      datetime.fromtimestamp(self.now(), timezone.utc).isoformat()))
                await self._db.executemany("""
                    INSERT INTO download_queue_items (batch_id, position, aweme_id, status)
                    VALUES (?, ?, ?, 'queued')
                """, [(batch_id, index, value) for index, value in enumerate(unique_ids)])
                await self._db.commit()
            except BaseException:
                await self._db.rollback()
                raise
            if ledger := get_default_ledger():
                try:
                    await ledger.set_limit("download", batch_id, request_limit)
                except Exception:
                    await self._db.execute("UPDATE download_queue_batches SET status='paused' WHERE batch_id=?", (batch_id,))
                    await self._db.commit()
                    raise
            result = await self._view(batch_id)
            self._reset_reuse_scan()
        self._wake.set()
        return result

    async def list_batches(self) -> List[Dict[str, Any]]:
        async with self._lock:
            self._require_db()
            rows = await self._fetchall("SELECT batch_id FROM download_queue_batches ORDER BY rowid DESC")
            return [await self._view(row["batch_id"]) for row in rows]

    async def get(self, batch_id: str) -> Dict[str, Any]:
        async with self._lock:
            self._require_db()
            return await self._view(batch_id)

    async def control(self, batch_id: str, action: str) -> Dict[str, Any]:
        if action not in {"pause", "resume", "stop"}:
            raise ValueError("不支持的队列操作")
        async with self._lock:
            batch = await self._view(batch_id)
            if batch["status"] in {"completed", "cancelled"}:
                return batch
            if action == "pause":
                await self._db.execute(
                    "UPDATE download_queue_batches SET status = 'paused' WHERE batch_id = ?", (batch_id,)
                )
            elif action == "stop":
                await self._db.execute(
                    "UPDATE download_queue_batches SET status = 'cancelled' WHERE batch_id = ?", (batch_id,)
                )
                await self._db.execute("""
                    UPDATE download_queue_items SET status = 'cancelled'
                    WHERE batch_id = ? AND status IN ('queued', 'failed')
                """, (batch_id,))
            else:
                await self._db.execute("""
                    UPDATE download_queue_items SET status = 'queued'
                    WHERE batch_id = ? AND status = 'failed'
                """, (batch_id,))
                await self._db.execute("""
                    UPDATE download_queue_batches SET status = 'queued', error_json = NULL WHERE batch_id = ?
                """, (batch_id,))
                self._reset_reuse_scan()
            await self._db.commit()
            result = await self._view(batch_id)
        self._wake.set()
        return result

    async def process_next(self) -> bool:
        """先做有界本地查重；每步最多下载一条，冷却不阻止本地复用。"""
        async with self._process_lock:
            reused = await self._reuse_available_items()
            async with self._lock:
                self._require_db()
                state = await self._fetchone("SELECT not_before FROM download_queue_state WHERE singleton = 1")
                if self.now() < state["not_before"]:
                    return reused
                item = await self._fetchone("""
                    SELECT i.*, b.author, b.profile_id, b.auto_summary
                    FROM download_queue_items i JOIN download_queue_batches b USING (batch_id)
                    WHERE i.status = 'queued' AND b.status IN ('queued', 'running')
                    ORDER BY b.rowid, i.position LIMIT 1
                """)
                if item is None:
                    return reused
                batch_id, aweme_id = item["batch_id"], item["aweme_id"]
                await self._db.execute(
                    "UPDATE download_queue_items SET status = 'running' WHERE batch_id = ? AND aweme_id = ?",
                    (batch_id, aweme_id),
                )
                await self._db.execute(
                    "UPDATE download_queue_batches SET status = 'running' WHERE batch_id = ?", (batch_id,)
                )
                # 先持久化运行态；异常退出由 initialize 保守补冷却，正常跳过不新增等待。
                await self._db.commit()
            try:
                task_id = item["task_id"]
                if not task_id:
                    task_id = await self._prepare_task(batch_id, aweme_id, item["author"], item["profile_id"])
                # 此时任务编号已经落盘，execute 才可以开始请求抖音。
                with model_request_context(batch_kind="download", batch_id=batch_id):
                    result = await self.execute_item(task_id, aweme_id, bool(item["auto_summary"]))
                if isinstance(result, dict) and result.get("status") == "missing":
                    # 任务记录可能被用户删除；此状态保证尚未发出网络请求，只重建一次。
                    task_id = await self._prepare_task(batch_id, aweme_id, item["author"], item["profile_id"])
                    with model_request_context(batch_kind="download", batch_id=batch_id):
                        result = await self.execute_item(task_id, aweme_id, bool(item["auto_summary"]))
                succeeded = isinstance(result, dict) and result.get("status") == "completed"
                await self._finish_item(
                    batch_id, aweme_id, succeeded,
                    result.get("error") if isinstance(result, dict) else None,
                    skipped=succeeded and result.get("skipped") is True,
                )
            except asyncio.CancelledError:
                async with self._lock:
                    await self._set_cooldown()
                    await self._db.execute("""
                        UPDATE download_queue_items SET status = CASE WHEN batch_id IN (
                            SELECT batch_id FROM download_queue_batches WHERE status = 'cancelled'
                        ) THEN 'cancelled' ELSE 'queued' END
                        WHERE batch_id = ? AND aweme_id = ?
                    """, (batch_id, aweme_id))
                    await self._db.execute("""
                        UPDATE download_queue_batches SET status = 'queued'
                        WHERE batch_id = ? AND status = 'running'
                    """, (batch_id,))
                    await self._db.commit()
                raise
            except Exception:
                # 不保存上游异常：异常可能包含 Cookie、地址参数或视频内容。
                await self._finish_item(batch_id, aweme_id, False)
            return True

    def _reset_reuse_scan(self) -> None:
        self._reuse_cursor = (0, -1)
        self._reuse_scan_done = False

    async def _reuse_available_items(self) -> bool:
        """只调用本地复用检查，不准备或下载任务；暂停操作可在检查期间生效。"""
        if self.reuse_item is None or self._reuse_scan_done:
            return False
        async with self._lock:
            self._require_db()
            # ponytail: 每轮用内存游标分批扫一次；执行回调仍重新查文件，缺失不作为永久结论。
            items = await self._fetchall("""
                SELECT i.*, b.rowid AS batch_order, b.author, b.profile_id, b.auto_summary
                FROM download_queue_items i JOIN download_queue_batches b USING (batch_id)
                WHERE i.status = 'queued' AND b.status IN ('queued', 'running')
                  AND (b.rowid > ? OR (b.rowid = ? AND i.position > ?))
                ORDER BY b.rowid, i.position LIMIT 64
            """, (self._reuse_cursor[0], *self._reuse_cursor))
            if items:
                self._reuse_cursor = (items[-1]["batch_order"], items[-1]["position"])
            self._reuse_scan_done = len(items) < 64
        reused = False
        for item in items:
            batch_id, aweme_id = item["batch_id"], item["aweme_id"]
            async with self._lock:
                if not await self._queued_item_is_active(batch_id, aweme_id):
                    continue
            with model_request_context(batch_kind="download", batch_id=batch_id):
                task_id = await self.reuse_item(
                    aweme_id, item["author"], item["profile_id"], bool(item["auto_summary"])
                )
            if task_id is None:
                continue
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("本地复用任务编号为空")
            async with self._lock:
                if not await self._queued_item_is_active(batch_id, aweme_id):
                    continue
                await self._db.execute("""
                    UPDATE download_queue_items SET task_id = ?, status = 'skipped'
                    WHERE batch_id = ? AND aweme_id = ? AND status = 'queued'
                """, (task_id, batch_id, aweme_id))
                await self._db.execute("""
                    UPDATE download_queue_batches SET status = 'completed'
                    WHERE batch_id = ? AND status IN ('queued', 'running') AND NOT EXISTS (
                        SELECT 1 FROM download_queue_items
                        WHERE batch_id = ? AND status IN ('queued', 'running', 'failed')
                    )
                """, (batch_id, batch_id))
                await self._db.commit()
                reused = True
        return reused

    async def _queued_item_is_active(self, batch_id: str, aweme_id: str) -> bool:
        return await self._fetchone("""
            SELECT 1 FROM download_queue_items i JOIN download_queue_batches b USING (batch_id)
            WHERE i.batch_id = ? AND i.aweme_id = ? AND i.status = 'queued'
              AND b.status IN ('queued', 'running')
        """, (batch_id, aweme_id)) is not None

    async def _prepare_task(self, batch_id: str, aweme_id: str, author: str, profile_id: str) -> str:
        task_id = await self.prepare_item(aweme_id, author, profile_id)
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("下载任务编号为空")
        async with self._lock:
            await self._db.execute(
                "UPDATE download_queue_items SET task_id = ? WHERE batch_id = ? AND aweme_id = ?",
                (task_id, batch_id, aweme_id),
            )
            await self._db.commit()
        return task_id

    async def _finish_item(
        self, batch_id: str, aweme_id: str, succeeded: bool, error: Any = None,
        *, skipped: bool = False,
    ) -> None:
        async with self._lock:
            if not skipped:
                await self._set_cooldown()
            await self._db.execute(
                "UPDATE download_queue_items SET status = ? WHERE batch_id = ? AND aweme_id = ?",
                ("skipped" if skipped else "completed" if succeeded else "failed", batch_id, aweme_id),
            )
            batch = await self._view(batch_id)
            if batch["status"] != "cancelled" and not succeeded:
                # 只接受应用内部回调的预定义诊断字段，不保存整个结果或上游响应。
                safe_error = {
                    "code": "DOWNLOAD_QUEUE_ITEM_FAILED",
                    "reason": "当前视频下载失败，已暂停该批次，避免继续请求抖音",
                    "action": f"在任务工作台查看失败原因，处理后继续队列；仍需等待 {self.interval_seconds} 秒冷却",
                }
                if (isinstance(error, dict) and isinstance(error.get("code"), str)
                        and re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", error["code"])
                        and all(isinstance(error.get(key), str) for key in ("reason", "action"))):
                    safe_error = {key: error[key][:500] for key in safe_error}
                await self._db.execute("""
                    UPDATE download_queue_batches SET status = 'paused', error_json = ? WHERE batch_id = ?
                """, (json.dumps(safe_error, ensure_ascii=False), batch_id))
            elif batch["status"] not in {"cancelled", "paused"}:
                await self._db.execute(
                    "UPDATE download_queue_batches SET status = ? WHERE batch_id = ?",
                    ("queued" if batch["pending"] else "completed", batch_id),
                )
            elif succeeded and batch["status"] == "paused" and not batch["pending"] and not batch["failed"]:
                await self._db.execute(
                    "UPDATE download_queue_batches SET status = 'completed' WHERE batch_id = ?", (batch_id,)
                )
            await self._db.commit()

    async def _run(self) -> None:
        while True:
            try:
                processed = await self.process_next()
            except Exception as exc:
                # 只记录类型；数据库异常文本也可能带查询参数或敏感信息。
                logger.error("批量下载队列异常，已停止继续出队；异常类型：%s", type(exc).__name__)
                while True:
                    try:
                        await self._pause_after_worker_error()
                        break
                    except Exception as pause_exc:
                        logger.error("批量下载队列暂停写入失败；异常类型：%s", type(pause_exc).__name__)
                        await asyncio.sleep(1)
                processed = False
            if not processed:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass

    async def _pause_after_worker_error(self) -> None:
        """数据库恢复前不再出队，恢复后保留暂停状态供人工继续。"""
        error = json.dumps({
            "code": "DOWNLOAD_QUEUE_WORKER_FAILED",
            "reason": "后台下载队列发生本地存储异常，已暂停后续下载",
            "action": f"检查本地服务和磁盘状态后继续队列；仍需等待 {self.interval_seconds} 秒冷却",
        }, ensure_ascii=False)
        async with self._lock:
            self._require_db()
            await self._db.rollback()
            await self._set_cooldown()
            await self._db.execute("""
                UPDATE download_queue_items
                SET status = CASE WHEN batch_id IN (
                    SELECT batch_id FROM download_queue_batches WHERE status = 'cancelled'
                ) THEN 'cancelled' ELSE 'queued' END
                WHERE status = 'running'
            """)
            await self._db.execute("""
                UPDATE download_queue_batches SET status = 'paused', error_json = ?
                WHERE status IN ('queued', 'running')
            """, (error,))
            await self._db.commit()

    async def _view(self, batch_id: str) -> Dict[str, Any]:
        self._require_db()
        batch = await self._fetchone("SELECT * FROM download_queue_batches WHERE batch_id = ?", (batch_id,))
        if batch is None:
            raise KeyError("批量下载队列不存在")
        counts = {row["status"]: row["count"] for row in await self._fetchall("""
            SELECT status, COUNT(*) AS count FROM download_queue_items WHERE batch_id = ? GROUP BY status
        """, (batch_id,))}
        current = await self._fetchone("""
            SELECT aweme_id, task_id FROM download_queue_items WHERE batch_id = ? AND status = 'running' LIMIT 1
        """, (batch_id,))
        state = await self._fetchone("SELECT not_before FROM download_queue_state WHERE singleton = 1")
        return {
            "batch_id": batch_id, "author": batch["author"], "profile_id": batch["profile_id"],
            "auto_summary": bool(batch["auto_summary"]),
            "status": batch["status"], "total": sum(counts.values()),
            "completed": counts.get("completed", 0) + counts.get("skipped", 0),
            "skipped": counts.get("skipped", 0), "failed": counts.get("failed", 0),
            "pending": counts.get("queued", 0) + counts.get("running", 0),
            "cancelled": counts.get("cancelled", 0),
            "current_aweme_id": current["aweme_id"] if current else None,
            "current_task_id": current["task_id"] if current else None,
            "next_allowed_at": state["not_before"],
            "next_in_seconds": max(0, int(state["not_before"] - self.now() + 0.999)),
            "interval_seconds": self.interval_seconds,
            "error": json.loads(batch["error_json"]) if batch["error_json"] else None,
        }

    async def _set_cooldown(self) -> None:
        await self._db.execute("""
            UPDATE download_queue_state SET not_before = MAX(not_before, ?) WHERE singleton = 1
        """, (self.now() + self.interval_seconds,))

    def _require_db(self) -> None:
        if self._db is None:
            raise RuntimeError("批量下载队列尚未初始化")

    async def _fetchone(self, sql: str, params: tuple = ()) -> Any:
        async with self._db.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> List[Any]:
        async with self._db.execute(sql, params) as cursor:
            return await cursor.fetchall()
