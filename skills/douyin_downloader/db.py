"""
数据库管理器 - 参考F2项目设计

使用异步SQLite操作，支持任务和视频信息的完整存储
"""

import aiosqlite
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field


@dataclass
class TaskInfo:
    """任务信息"""

    task_id: str
    type: str  # 'single' | 'mix' | 'mix_user'
    mix_id: Optional[str]
    mix_name: Optional[str]
    status: str  # 'pending' | 'downloading' | 'completed' | 'failed' | 'cancelled'
    base_path: Optional[str]
    total_count: int = 0
    downloaded_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    download_options: Optional[str] = None  # JSON格式字符串
    progress_percentage: Optional[int] = None  # 下载中间进度百分比
    current_video_name: Optional[str] = None  # 下载中的当前文件名
    result_path: Optional[str] = None  # 完成后由下载器返回的精确文件/目录
    history_attempts: List[Dict[str, Any]] = field(default_factory=list)
    history_count: int = 0
    history_failed_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "type": self.type,
            "mix_id": self.mix_id,
            "mix_name": self.mix_name,
            "status": self.status,
            "base_path": self.base_path,
            "total_count": self.total_count,
            "downloaded_count": self.downloaded_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "download_options": self.download_options,
            "progress_percentage": self.progress_percentage,
            "current_video_name": self.current_video_name,
            "result_path": self.result_path,
            "history_attempts": self.history_attempts,
            "history_count": self.history_count,
            "history_failed_count": self.history_failed_count,
        }


@dataclass
class TaskVideoInfo:
    """任务中的视频信息 - 参考F2的video_info表设计"""

    id: int
    task_id: str
    aweme_id: str
    desc: str
    nickname: str
    sec_user_id: Optional[str]
    create_time: str
    status: str  # 'pending' | 'downloaded' | 'skipped' | 'failed'
    file_path: Optional[str]
    file_size: Optional[int]
    error_message: Optional[str]
    download_time: Optional[str]
    # 扩展字段，参考F2设计
    video_play_addr: Optional[str] = None
    cover: Optional[str] = None
    duration: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "aweme_id": self.aweme_id,
            "desc": self.desc,
            "nickname": self.nickname,
            "sec_user_id": self.sec_user_id,
            "create_time": self.create_time,
            "status": self.status,
            "file_path": self.file_path,
            "file_size": self.file_size,
            "error_message": self.error_message,
            "download_time": self.download_time,
            "video_play_addr": self.video_play_addr,
            "cover": self.cover,
            "duration": self.duration,
        }


class TaskDB:
    """
    任务数据库管理器 - 参考F2 BaseDB设计

    使用异步SQLite存储，支持完整的任务和视频信息管理
    """

    def __init__(self, db_path: str = "./douyin_downloader.db"):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None

    @staticmethod
    def _canonical_tasks_cte() -> str:
        """Build the shared pre-pagination single-video attempt grouping query."""
        return """
            WITH task_identity AS (
                SELECT tasks.*,
                    COALESCE((
                        SELECT tv.aweme_id FROM task_videos tv
                        WHERE tv.task_id = tasks.task_id
                        ORDER BY tv.id ASC LIMIT 1
                    ), '') AS canonical_aweme_id
                FROM tasks
            ),
            task_keys AS (
                SELECT task_identity.*,
                    CASE
                        WHEN type='single' AND canonical_aweme_id <> ''
                        THEN 'single:' || canonical_aweme_id
                        ELSE 'task:' || task_id
                    END AS canonical_key
                FROM task_identity
            ),
            ranked_tasks AS (
                SELECT task_keys.*,
                    COUNT(*) OVER (PARTITION BY canonical_key) AS attempt_count,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END)
                        OVER (PARTITION BY canonical_key) AS failed_attempt_count,
                    ROW_NUMBER() OVER (
                        PARTITION BY canonical_key
                        ORDER BY
                            CASE status
                                WHEN 'completed' THEN 0
                                WHEN 'downloading' THEN 1
                                WHEN 'pending' THEN 2
                                WHEN 'failed' THEN 3
                                WHEN 'cancelled' THEN 3
                                ELSE 4
                            END ASC,
                            COALESCE(completed_at, created_at) DESC,
                            created_at DESC,
                            task_id DESC
                    ) AS canonical_rank
                FROM task_keys
            ),
            canonical_tasks AS (
                SELECT * FROM ranked_tasks WHERE canonical_rank=1
            )
        """

    @staticmethod
    def _task_filter_sql(
        *, status: Optional[str], task_type: Optional[str], keyword: Optional[str], author: Optional[str] = None, alias: str = "c"
    ) -> tuple[List[str], List[Any]]:
        conditions: List[str] = []
        params: List[Any] = []
        if status:
            conditions.append(f"{alias}.status=?")
            params.append(status)
        if task_type:
            conditions.append(f"{alias}.type=?")
            params.append(task_type)
        if author is not None:
            author_sql = ("CASE WHEN c.type='post' THEN NULLIF(TRIM(c.mix_name), '') "
                          "ELSE (SELECT NULLIF(TRIM(tv.nickname), '') FROM task_videos tv "
                          "WHERE tv.task_id=c.task_id ORDER BY tv.id LIMIT 1) END")
            conditions.append(f"COALESCE({author_sql}, '')=?")
            params.append(author)
        if keyword:
            conditions.append(
                f"({alias}.task_id LIKE ? OR {alias}.mix_id LIKE ? OR {alias}.mix_name LIKE ? "
                f"OR {alias}.error_message LIKE ? OR EXISTS ("
                f"SELECT 1 FROM task_videos tv WHERE tv.task_id={alias}.task_id "
                "AND (tv.desc LIKE ? OR tv.nickname LIKE ? OR tv.aweme_id LIKE ?)))"
            )
            pattern = f"%{keyword.strip()}%"
            params.extend([pattern] * 7)
        # Keep malformed legacy single tasks and internal per-video references
        # out of the visible task list.  ``batch_item`` rows are materialized
        # only when a gallery card opens an AI feature; the parent batch remains
        # the sole visible task and is flattened by the frontend.
        conditions.append(f"({alias}.type <> 'single' OR {alias}.canonical_aweme_id <> '')")
        conditions.append(f"{alias}.type <> 'batch_item'")
        return conditions, params

    @staticmethod
    def _task_info_from_row(row: tuple) -> TaskInfo:
        return TaskInfo(
            task_id=row[0],
            type=row[1],
            mix_id=row[2],
            mix_name=row[3],
            status=row[4],
            base_path=row[5],
            total_count=row[6] or 0,
            downloaded_count=row[7] or 0,
            skipped_count=row[8] or 0,
            failed_count=row[9] or 0,
            error_message=row[10],
            created_at=row[11],
            completed_at=row[12],
            download_options=row[13] if len(row) > 13 else None,
            progress_percentage=row[14] if len(row) > 14 else None,
            current_video_name=row[15] if len(row) > 15 else None,
            result_path=row[16] if len(row) > 16 else None,
            history_count=max(0, int(row[19] or 1) - 1) if len(row) > 19 else 0,
            history_failed_count=max(
                0,
                int(row[20] or 0) - (1 if row[4] == "failed" else 0),
            ) if len(row) > 20 else 0,
        )

    @staticmethod
    def _history_error(raw_error: Optional[str]) -> Optional[Dict[str, str]]:
        """Expose only structured, user-facing error fields from historical attempts."""
        if not raw_error:
            return None
        try:
            value = json.loads(raw_error)
        except (TypeError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        result = {
            key: str(value[key])
            for key in ("stage", "code", "reason", "action")
            if value.get(key) not in (None, "")
        }
        return result or None

    async def connect(self) -> None:
        """连接到数据库"""
        self.conn = await aiosqlite.connect(self.db_path)
        # SQLite disables FK enforcement by default. Enable it for every
        # connection so the job tables cannot outlive their parent task.
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self._create_tables()

    async def close(self) -> None:
        """关闭数据库连接"""
        conn = self.conn
        self.conn = None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass

    async def _create_tables(self) -> None:
        """创建数据库表"""
        # 任务表 - 简化版，参考F2设计
        await self.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                mix_id TEXT,
                mix_name TEXT,
                status TEXT DEFAULT 'pending',
                base_path TEXT,
                total_count INTEGER DEFAULT 0,
                downloaded_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                error_message TEXT,
                created_at TEXT,
                completed_at TEXT,
                download_options TEXT  -- JSON格式存储下载选项(max_counts, interval等)
            )
        """)

        # 视频表 - 参考F2的video_info表设计，添加task_id关联
        await self.execute("""
            CREATE TABLE IF NOT EXISTS task_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                aweme_id TEXT NOT NULL,
                desc TEXT,
                nickname TEXT,
                sec_user_id TEXT,
                create_time TEXT,
                status TEXT DEFAULT 'pending',
                file_path TEXT,
                file_size INTEGER,
                error_message TEXT,
                download_time TEXT,
                video_play_addr TEXT,
                cover TEXT,
                duration TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id),
                UNIQUE(task_id, aweme_id)
            )
        """)

        # 创建索引
        await self.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_videos_task_id 
            ON task_videos(task_id)
        """)
        await self.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_videos_aweme_id 
            ON task_videos(aweme_id)
        """)
        await self.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status 
            ON tasks(status)
        """)

        await self.execute("""
            CREATE TABLE IF NOT EXISTS transcript_jobs (
                job_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'queued',
                stage TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER NOT NULL DEFAULT 0,
                original_transcript TEXT,
                polished_text TEXT,
                segments_json TEXT,
                error_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id)
            )
        """)
        await self.execute("""
            CREATE INDEX IF NOT EXISTS idx_transcript_jobs_task_id
            ON transcript_jobs(task_id)
        """)

        await self.execute("""
            CREATE TABLE IF NOT EXISTS video_summary_jobs (
                job_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'queued',
                stage TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER NOT NULL DEFAULT 0,
                model TEXT,
                input_bytes INTEGER,
                summary_json TEXT,
                error_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                prepared_bytes INTEGER,
                processing_mode TEXT,
                segment_count INTEGER,
                provider_attempts_json TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id)
            )
        """)
        await self.execute("""
            CREATE INDEX IF NOT EXISTS idx_video_summary_jobs_task_id
            ON video_summary_jobs(task_id)
        """)

        await self.execute("""
            CREATE TABLE IF NOT EXISTS distill_runs (
                run_id TEXT PRIMARY KEY,
                sec_user_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                user_url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                options_json TEXT NOT NULL DEFAULT '{}',
                synthesis_model TEXT,
                total_count INTEGER NOT NULL DEFAULT 0,
                completed_count INTEGER NOT NULL DEFAULT 0,
                degraded_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                output_path TEXT,
                synthesis_json TEXT,
                synthesis_audit TEXT,
                synthesis_response_meta TEXT,
                error_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
        """)
        await self.execute("""
            CREATE INDEX IF NOT EXISTS idx_distill_runs_status
            ON distill_runs(status)
        """)
        await self.execute("""
            CREATE TABLE IF NOT EXISTS distill_items (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                aweme_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                task_id TEXT,
                local_path TEXT,
                source_size INTEGER,
                source_mtime_ns INTEGER,
                item_status TEXT NOT NULL DEFAULT 'queued',
                download_status TEXT NOT NULL DEFAULT 'pending',
                transcript_status TEXT NOT NULL DEFAULT 'pending',
                summary_status TEXT NOT NULL DEFAULT 'pending',
                transcript_job_id TEXT,
                summary_job_id TEXT,
                original_transcript TEXT,
                polished_text TEXT,
                summary_json TEXT,
                degrade_reason TEXT,
                error_json TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES distill_runs(run_id) ON DELETE CASCADE,
                UNIQUE(run_id, aweme_id)
            )
        """)
        await self.execute("""
            CREATE INDEX IF NOT EXISTS idx_distill_items_run_ordinal
            ON distill_items(run_id, ordinal)
        """)
        await self.execute("""
            CREATE INDEX IF NOT EXISTS idx_distill_items_aweme_id
            ON distill_items(aweme_id)
        """)

        await self.commit()

        # 执行数据库迁移
        await self._migrate_database()

    async def _migrate_database(self) -> None:
        """数据库迁移 - 添加新字段"""
        # 检查是否需要添加 download_options 字段
        try:
            await self.execute("SELECT download_options FROM tasks LIMIT 1")
        except Exception:
            # 字段不存在，添加它
            print("[DB] Migrating: adding download_options column to tasks table")
            await self.execute("ALTER TABLE tasks ADD COLUMN download_options TEXT")
            await self.commit()
            print("[DB] Migration completed")

        # 检查是否需要添加 progress_percentage 字段（下载中间进度）
        try:
            await self.execute("SELECT progress_percentage FROM tasks LIMIT 1")
        except Exception:
            print("[DB] Migrating: adding progress_percentage column to tasks table")
            await self.execute(
                "ALTER TABLE tasks ADD COLUMN progress_percentage INTEGER"
            )
            await self.commit()
            print("[DB] Migration completed")

        # 检查是否需要添加 current_video_name 字段（下载中的当前文件）
        try:
            await self.execute("SELECT current_video_name FROM tasks LIMIT 1")
        except Exception:
            print("[DB] Migrating: adding current_video_name column to tasks table")
            await self.execute(
                "ALTER TABLE tasks ADD COLUMN current_video_name TEXT"
            )
            await self.commit()
            print("[DB] Migration completed")

        # 保存下载器返回的精确目录，同时保留 base_path 供失败重试使用
        try:
            await self.execute("SELECT result_path FROM tasks LIMIT 1")
        except Exception:
            print("[DB] Migrating: adding result_path column to tasks table")
            await self.execute("ALTER TABLE tasks ADD COLUMN result_path TEXT")
            await self.commit()
            print("[DB] Migration completed")

        for column, definition in (
            ("prepared_bytes", "INTEGER"),
            ("processing_mode", "TEXT"),
            ("segment_count", "INTEGER"),
            ("provider_attempts_json", "TEXT"),
        ):
            try:
                await self.execute(f"SELECT {column} FROM video_summary_jobs LIMIT 1")
            except Exception:
                print(f"[DB] Migrating: adding {column} column to video_summary_jobs table")
                await self.execute(f"ALTER TABLE video_summary_jobs ADD COLUMN {column} {definition}")
                await self.commit()
                print("[DB] Migration completed")

        try:
            await self.execute("SELECT synthesis_json FROM distill_runs LIMIT 1")
        except Exception:
            print("[DB] Migrating: adding synthesis_json column to distill_runs table")
            await self.execute("ALTER TABLE distill_runs ADD COLUMN synthesis_json TEXT")
            await self.commit()
        try:
            await self.execute("SELECT synthesis_audit FROM distill_runs LIMIT 1")
        except Exception:
            await self.execute("ALTER TABLE distill_runs ADD COLUMN synthesis_audit TEXT")
            await self.commit()
        try:
            await self.execute("SELECT synthesis_response_meta FROM distill_runs LIMIT 1")
        except Exception:
            await self.execute("ALTER TABLE distill_runs ADD COLUMN synthesis_response_meta TEXT")
            await self.commit()

        # Older versions created these tables without enforcing foreign keys;
        # remove any rows left behind by deleting their parent task.
        await self.execute("DELETE FROM transcript_jobs WHERE task_id NOT IN (SELECT task_id FROM tasks)")
        await self.execute("DELETE FROM video_summary_jobs WHERE task_id NOT IN (SELECT task_id FROM tasks)")
        await self.commit()

    # ==================== 博主内容蒸馏 ====================

    async def create_distill_run(self, run_id: str, *, sec_user_id: str, nickname: str, user_url: str,
                                 options: Optional[Dict[str, Any]] = None, synthesis_model: str = "") -> None:
        now = datetime.now().isoformat()
        await self.execute(
            """
            INSERT INTO distill_runs
            (run_id, sec_user_id, nickname, user_url, status, options_json, synthesis_model, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)
            """,
            (run_id, sec_user_id, nickname, user_url, json.dumps(options or {}, ensure_ascii=False), synthesis_model, now, now),
        )
        await self.commit()

    async def add_distill_items(self, run_id: str, items: List[Dict[str, Any]]) -> None:
        now = datetime.now().isoformat()
        for item in items:
            await self.execute(
                """
                INSERT INTO distill_items
                (run_id, ordinal, aweme_id, metadata_json, task_id, item_status, download_status,
                 transcript_status, summary_status, local_path, source_size, source_mtime_ns,
                 transcript_job_id, summary_job_id, original_transcript, polished_text, summary_json,
                 degrade_reason, error_json, retry_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    int(item.get("ordinal") or 0),
                    str(item.get("aweme_id") or ""),
                    json.dumps(item.get("metadata") or {}, ensure_ascii=False),
                    item.get("task_id"),
                    item.get("item_status") or "queued",
                    item.get("download_status") or "pending",
                    item.get("transcript_status") or "pending",
                    item.get("summary_status") or "pending",
                    item.get("local_path"), item.get("source_size"), item.get("source_mtime_ns"),
                    item.get("transcript_job_id"), item.get("summary_job_id"), item.get("original_transcript"),
                    item.get("polished_text"), json.dumps(item.get("summary"), ensure_ascii=False) if item.get("summary") is not None else item.get("summary_json"),
                    item.get("degrade_reason"), item.get("error_json"), int(item.get("retry_count") or 0),
                    now,
                    now,
                ),
            )
        await self.execute("UPDATE distill_runs SET total_count=?, updated_at=? WHERE run_id=?", (len(items), now, run_id))
        await self.commit()

    @staticmethod
    def _distill_run_from_row(row: tuple) -> Dict[str, Any]:
        return {
            "run_id": row[0], "sec_user_id": row[1], "nickname": row[2], "user_url": row[3],
            "status": row[4], "options": json.loads(row[5] or "{}"), "synthesis_model": row[6] or "",
            "total_count": row[7] or 0, "completed_count": row[8] or 0, "degraded_count": row[9] or 0,
            "skipped_count": row[10] or 0, "failed_count": row[11] or 0, "output_path": row[12],
            "synthesis": json.loads(row[13]) if row[13] else None,
            "error": json.loads(row[14]) if row[14] else None, "created_at": row[15], "updated_at": row[16],
            "started_at": row[17], "completed_at": row[18],
            "synthesis_audit": json.loads(row[19]) if len(row) > 19 and row[19] else None,
            "synthesis_response_meta": json.loads(row[20]) if len(row) > 20 and row[20] else [],
        }

    @staticmethod
    def _distill_item_from_row(row: tuple) -> Dict[str, Any]:
        return {
            "item_id": row[0], "run_id": row[1], "ordinal": row[2], "aweme_id": row[3],
            "metadata": json.loads(row[4] or "{}"), "task_id": row[5], "local_path": row[6],
            "source_size": row[7], "source_mtime_ns": row[8], "item_status": row[9],
            "download_status": row[10], "transcript_status": row[11], "summary_status": row[12],
            "transcript_job_id": row[13], "summary_job_id": row[14], "original_transcript": row[15] or "",
            "polished_text": row[16] or "", "summary": json.loads(row[17]) if row[17] else None,
            "degrade_reason": row[18], "error": json.loads(row[19]) if row[19] else None,
            "retry_count": row[20] or 0, "created_at": row[21], "updated_at": row[22],
        }

    async def get_distill_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = await self.fetch_one("SELECT run_id, sec_user_id, nickname, user_url, status, options_json, synthesis_model, total_count, completed_count, degraded_count, skipped_count, failed_count, output_path, synthesis_json, error_json, created_at, updated_at, started_at, completed_at, synthesis_audit, synthesis_response_meta FROM distill_runs WHERE run_id=?", (run_id,))
        if not row:
            return None
        run = self._distill_run_from_row(row)
        counts = await self.count_distill_items(run_id)
        run.update(completed_count=counts["completed"], degraded_count=counts["degraded"], skipped_count=counts["skipped"], failed_count=counts["failed"], cancelled_count=counts["cancelled"], pending_count=counts["pending"])
        return run

    async def list_distill_runs(self, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        rows = await self.fetch_all(
            "SELECT run_id, sec_user_id, nickname, user_url, status, options_json, synthesis_model, total_count, completed_count, degraded_count, skipped_count, failed_count, output_path, synthesis_json, error_json, created_at, updated_at, started_at, completed_at, synthesis_audit, synthesis_response_meta FROM distill_runs ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?",
            (max(1, min(int(limit), 100)), max(0, int(offset))),
        )
        runs = [self._distill_run_from_row(row) for row in rows]
        for run in runs:
            counts = await self.count_distill_items(run["run_id"])
            run.update(completed_count=counts["completed"], degraded_count=counts["degraded"], skipped_count=counts["skipped"], failed_count=counts["failed"], cancelled_count=counts["cancelled"], pending_count=counts["pending"])
        return runs

    async def update_distill_run(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "status", "completed_count", "degraded_count", "skipped_count", "failed_count",
            "output_path", "synthesis_json", "synthesis_audit", "synthesis_response_meta", "error_json", "started_at", "completed_at", "options_json",
        }
        updates = []
        params: List[Any] = []
        for key, value in fields.items():
            if key in allowed:
                updates.append(f"{key}=?")
                params.append(value)
        if not updates:
            return
        updates.append("updated_at=?")
        params.extend([datetime.now().isoformat(), run_id])
        await self.execute(f"UPDATE distill_runs SET {', '.join(updates)} WHERE run_id=?", tuple(params))
        await self.commit()

    async def get_distill_items(self, run_id: str, *, item_ids: Optional[List[str]] = None,
                                limit: int = 200, offset: int = 0, include_text: bool = True) -> List[Dict[str, Any]]:
        params: List[Any] = [run_id]
        where = "run_id=?"
        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            where += f" AND aweme_id IN ({placeholders})"
            params.extend(item_ids)
        params.extend([max(1, min(int(limit), 10_000)), max(0, int(offset))])
        columns = "*" if include_text else "item_id, run_id, ordinal, aweme_id, metadata_json, task_id, local_path, source_size, source_mtime_ns, item_status, download_status, transcript_status, summary_status, transcript_job_id, summary_job_id, NULL, NULL, NULL, degrade_reason, error_json, retry_count, created_at, updated_at"
        rows = await self.fetch_all(
            f"SELECT {columns} FROM distill_items WHERE {where} ORDER BY ordinal ASC LIMIT ? OFFSET ?",
            tuple(params),
        )
        return [self._distill_item_from_row(row) for row in rows]

    async def get_distill_item(self, run_id: str, aweme_id: str) -> Optional[Dict[str, Any]]:
        row = await self.fetch_one("SELECT * FROM distill_items WHERE run_id=? AND aweme_id=?", (run_id, aweme_id))
        return self._distill_item_from_row(row) if row else None

    async def get_latest_distill_result_for_task(self, task_id: str, result_type: str) -> Optional[Dict[str, Any]]:
        """Return the newest persisted distill item that contains one AI result.

        Distill runs intentionally keep their own result snapshots instead of
        writing into the UNIQUE task-level AI job tables.  This lookup lets the
        task detail UI reuse those snapshots without starting another paid
        model request.  ``result_type`` is an internal selector, not SQL.
        """
        if result_type == "transcript":
            predicate = "(NULLIF(original_transcript, '') IS NOT NULL OR NULLIF(polished_text, '') IS NOT NULL)"
        elif result_type == "summary":
            predicate = "NULLIF(summary_json, '') IS NOT NULL"
        else:
            raise ValueError("unsupported distill result type")
        row = await self.fetch_one(
            f"SELECT * FROM distill_items WHERE task_id=? AND {predicate} ORDER BY updated_at DESC, item_id DESC LIMIT 1",
            (task_id,),
        )
        return self._distill_item_from_row(row) if row else None

    async def update_distill_item(self, run_id: str, aweme_id: str, **fields: Any) -> None:
        allowed = {
            "task_id", "local_path", "source_size", "source_mtime_ns", "item_status", "download_status",
            "transcript_status", "summary_status", "transcript_job_id", "summary_job_id",
            "original_transcript", "polished_text", "summary_json", "degrade_reason", "error_json", "retry_count",
        }
        updates = []
        params: List[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            updates.append(f"{key}=?")
            params.append(value)
        if not updates:
            return
        updates.append("updated_at=?")
        params.extend([datetime.now().isoformat(), run_id, aweme_id])
        await self.execute(
            f"UPDATE distill_items SET {', '.join(updates)} WHERE run_id=? AND aweme_id=?",
            tuple(params),
        )
        await self.commit()

    async def count_distill_items(self, run_id: str) -> Dict[str, int]:
        row = await self.fetch_one(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN item_status IN ('ready', 'written') THEN 1 ELSE 0 END) AS completed,
              SUM(CASE WHEN item_status='degraded' THEN 1 ELSE 0 END) AS degraded,
              SUM(CASE WHEN item_status IN ('skipped', 'low_signal', 'summary_invalid', 'transcribed') THEN 1 ELSE 0 END) AS skipped,
              SUM(CASE WHEN item_status='failed' THEN 1 ELSE 0 END) AS failed,
              SUM(CASE WHEN item_status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
              SUM(CASE WHEN item_status NOT IN ('ready', 'written', 'degraded', 'transcribed', 'skipped', 'low_signal', 'summary_invalid', 'failed', 'cancelled') THEN 1 ELSE 0 END) AS pending
            FROM distill_items WHERE run_id=?
            """,
            (run_id,),
        )
        return {
            "total": int(row[0] or 0) if row else 0,
            "completed": int(row[1] or 0) if row else 0,
            "degraded": int(row[2] or 0) if row else 0,
            "skipped": int(row[3] or 0) if row else 0,
            "failed": int(row[4] or 0) if row else 0,
            "cancelled": int(row[5] or 0) if row else 0,
            "pending": int(row[6] or 0) if row else 0,
        }

    async def execute(self, sql: str, parameters: tuple = ()) -> aiosqlite.Cursor:
        """执行SQL语句"""
        if not self.conn:
            await self.connect()
        return await self.conn.execute(sql, parameters)

    async def commit(self) -> None:
        """提交事务"""
        if self.conn:
            await self.conn.commit()

    async def fetch_one(self, sql: str, parameters: tuple = ()) -> Optional[tuple]:
        """获取单条记录"""
        cursor = await self.execute(sql, parameters)
        return await cursor.fetchone()

    async def fetch_all(self, sql: str, parameters: tuple = ()) -> List[tuple]:
        """获取多条记录"""
        cursor = await self.execute(sql, parameters)
        return await cursor.fetchall()

    # ==================== 任务操作 ====================

    async def create_task(
        self,
        task_type: str,
        mix_id: Optional[str] = None,
        mix_name: Optional[str] = None,
        base_path: Optional[str] = None,
        download_options: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
    ) -> str:
        """
        创建新任务

        Returns:
            task_id: 任务ID
        """
        import uuid
        import json

        # Most callers let the database allocate an ID.  The compatibility
        # ``TaskManager.create_task`` path already returns an ID synchronously,
        # so it must be able to persist that exact ID later; otherwise the
        # parent row and its video row become disconnected (or the video insert
        # fails under foreign-key enforcement).
        task_id = task_id or str(uuid.uuid4())
        created_at = datetime.now().isoformat()
        download_options_json = json.dumps(download_options) if download_options else None

        await self.execute(
            """
            INSERT INTO tasks (task_id, type, mix_id, mix_name, status, base_path, created_at, download_options)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (task_id, task_type, mix_id, mix_name, base_path, created_at, download_options_json),
        )
        await self.commit()
        return task_id

    async def get_task(self, task_id: str) -> Optional[TaskInfo]:
        """获取任务信息"""
        row = await self.fetch_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if row:
            return self._task_info_from_row(row)
        return None

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        """更新任务状态"""
        if status in ["completed", "failed"]:
            completed_at = datetime.now().isoformat()
            await self.execute(
                """
                UPDATE tasks SET status=?, error_message=?, completed_at=?
                WHERE task_id=?
                """,
                (status, error_message, completed_at, task_id),
            )
        else:
            await self.execute(
                "UPDATE tasks SET status=?, error_message=? WHERE task_id=?",
                (status, error_message, task_id),
            )
        await self.commit()

    async def update_task_counts(
        self,
        task_id: str,
        total: Optional[int] = None,
        downloaded: Optional[int] = None,
        skipped: Optional[int] = None,
        failed: Optional[int] = None,
    ) -> None:
        """更新任务计数"""
        updates = []
        params = []
        if total is not None:
            updates.append("total_count=?")
            params.append(total)
        if downloaded is not None:
            updates.append("downloaded_count=?")
            params.append(downloaded)
        if skipped is not None:
            updates.append("skipped_count=?")
            params.append(skipped)
        if failed is not None:
            updates.append("failed_count=?")
            params.append(failed)

        if updates:
            sql = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id=?"
            params.append(task_id)
            await self.execute(sql, tuple(params))
            await self.commit()

    async def update_task_result_path(self, task_id: str, result_path: str) -> None:
        """Persist the exact completed output path without changing retry base_path."""
        await self.execute(
            "UPDATE tasks SET result_path=? WHERE task_id=?", (result_path, task_id)
        )
        await self.commit()

    async def update_task_progress(
        self,
        task_id: str,
        percentage: Optional[int] = None,
        current_video_name: Optional[str] = None,
    ) -> None:
        """更新任务下载进度（中间状态）"""
        updates = []
        params = []
        if percentage is not None:
            updates.append("progress_percentage=?")
            params.append(percentage)
        if current_video_name is not None:
            updates.append("current_video_name=?")
            params.append(current_video_name)

        if updates:
            sql = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id=?"
            params.append(task_id)
            await self.execute(sql, tuple(params))
            await self.commit()

    async def get_tasks(
        self,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        keyword: Optional[str] = None,
        author: Optional[str] = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> List[TaskInfo]:
        """Fetch canonical rows and their historical attempts before applying pagination."""
        conditions, params = self._task_filter_sql(
            status=status, task_type=task_type, keyword=keyword, author=author
        )
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sort_columns = {
            "created_at": "c.created_at",
            "completed_at": "c.completed_at",
            "status": "c.status",
            "type": "c.type",
            "title": "COALESCE((SELECT tv.desc FROM task_videos tv "
                     "WHERE tv.task_id=c.task_id ORDER BY tv.id ASC LIMIT 1), c.mix_name, c.task_id)",
        }
        order_column = sort_columns.get(sort_by, "created_at")
        direction = "ASC" if str(sort_order).lower() == "asc" else "DESC"
        rows = await self.fetch_all(
            f"{self._canonical_tasks_cte()} "
            f"SELECT c.* FROM canonical_tasks c{where} "
            f"ORDER BY {order_column} {direction}, c.task_id DESC LIMIT ? OFFSET ?",
            tuple([*params, limit, offset]),
        )

        tasks = [self._task_info_from_row(row) for row in rows]
        if not tasks:
            return tasks

        task_ids = [task.task_id for task in tasks]
        placeholders = ",".join("?" for _ in task_ids)
        history_rows = await self.fetch_all(
            f"{self._canonical_tasks_cte()} "
            "SELECT canonical.task_id, history.task_id, history.status, "
            "history.created_at, history.completed_at, history.error_message "
            "FROM canonical_tasks canonical "
            "JOIN task_keys history ON history.canonical_key=canonical.canonical_key "
            f"WHERE canonical.task_id IN ({placeholders}) "
            "AND history.task_id <> canonical.task_id "
            "ORDER BY history.created_at DESC, history.task_id DESC",
            tuple(task_ids),
        )
        attempts_by_task: Dict[str, List[Dict[str, Any]]] = {}
        for canonical_id, attempt_id, attempt_status, created_at, completed_at, raw_error in history_rows:
            attempts_by_task.setdefault(canonical_id, []).append(
                {
                    "id": attempt_id,
                    "status": attempt_status,
                    "created_at": created_at,
                    "completed_at": completed_at,
                    # Task attempts predate a persisted retry counter. Keep a
                    # stable field for the list API; future schema versions
                    # can populate the actual value without changing clients.
                    "retry_count": 0,
                    "error": self._history_error(raw_error),
                }
            )
        for task in tasks:
            task.history_attempts = attempts_by_task.get(task.task_id, [])[:5]
            # Keep the count consistent even for legacy records whose attempt details
            # could not be projected; the list endpoint remains bounded and safe.
            task.history_count = max(task.history_count, len(task.history_attempts))
        return tasks

    async def count_tasks(
        self,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
        keyword: Optional[str] = None,
        author: Optional[str] = None,
    ) -> int:
        """Count filtered canonical rows, matching list pagination exactly."""
        conditions, params = self._task_filter_sql(
            status=status, task_type=task_type, keyword=keyword, author=author
        )
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        row = await self.fetch_one(
            f"{self._canonical_tasks_cte()} SELECT COUNT(*) FROM canonical_tasks c{where}",
            tuple(params),
        )
        return int(row[0]) if row else 0

    async def get_task_authors(self, status: Optional[str] = None, task_type: Optional[str] = None, keyword: Optional[str] = None) -> List[Dict[str, Any]]:
        """统计全部匹配任务的作者，不受当前页和作者筛选影响。"""
        conditions, params = self._task_filter_sql(status=status, task_type=task_type, keyword=keyword)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = await self.fetch_all(
            f"{self._canonical_tasks_cte()} "
            "SELECT COALESCE(CASE WHEN c.type='post' THEN NULLIF(TRIM(c.mix_name), '') "
            "ELSE (SELECT NULLIF(TRIM(tv.nickname), '') FROM task_videos tv "
            "WHERE tv.task_id=c.task_id ORDER BY tv.id LIMIT 1) END, '') AS author, "
            "SUM(CASE WHEN c.type IN ('post', 'mix', 'mix_user') "
            "THEN (SELECT COUNT(*) FROM task_videos tv_count WHERE tv_count.task_id=c.task_id) "
            "ELSE 1 END) AS work_count "
            f"FROM canonical_tasks c{where} GROUP BY author ORDER BY work_count DESC, author ASC",
            tuple(params),
        )
        return [{"name": row[0], "count": row[1]} for row in rows]

    async def get_task_summary_statuses(self, task_ids: List[str]) -> Dict[str, str]:
        """批量读取列表徽标状态，蒸馏产物与普通总结都视为已总结。"""
        statuses: Dict[str, str] = {}
        for start in range(0, len(task_ids), 500):
            batch = task_ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = await self.fetch_all(
                f"SELECT task_id, status, summary_json FROM video_summary_jobs WHERE task_id IN ({placeholders})",
                tuple(batch),
            )
            for task_id, status, summary_json in rows:
                if status == "completed" and summary_json:
                    statuses[task_id] = "completed"
                elif status in {"queued", "running"}:
                    statuses[task_id] = "running"
                elif status == "failed":
                    statuses[task_id] = "failed"
            distilled = await self.fetch_all(
                f"SELECT DISTINCT task_id FROM distill_items WHERE task_id IN ({placeholders}) "
                "AND NULLIF(summary_json, '') IS NOT NULL",
                tuple(batch),
            )
            for (task_id,) in distilled:
                statuses[task_id] = "completed"
        return statuses

    async def get_batch_video_feature_statuses(self, parent_task_id: str) -> Dict[str, Dict[str, str]]:
        """读取批量父任务下每个作品的总结状态。

        ``batch_item`` 是隐藏的单视频引用任务，只有用户打开某个作品的
        AI 功能后才会出现。用相关子查询把它映射回父任务的 aweme_id，
        画廊刷新后即可正确显示“已总结/总结中/总结失败”徽标。
        """
        rows = await self.fetch_all(
            """
            SELECT tv.aweme_id,
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM tasks bt
                        JOIN task_videos btv ON btv.task_id=bt.task_id
                        JOIN video_summary_jobs vj ON vj.task_id=bt.task_id
                        WHERE bt.type='batch_item' AND bt.mix_id=?
                          AND btv.aweme_id=tv.aweme_id
                          AND vj.status='completed' AND NULLIF(vj.summary_json, '') IS NOT NULL
                    ) THEN 'completed'
                    WHEN EXISTS (
                        SELECT 1 FROM tasks bt
                        JOIN task_videos btv ON btv.task_id=bt.task_id
                        JOIN video_summary_jobs vj ON vj.task_id=bt.task_id
                        WHERE bt.type='batch_item' AND bt.mix_id=?
                          AND btv.aweme_id=tv.aweme_id
                          AND vj.status IN ('queued', 'running')
                    ) THEN 'running'
                    WHEN EXISTS (
                        SELECT 1 FROM tasks bt
                        JOIN task_videos btv ON btv.task_id=bt.task_id
                        JOIN video_summary_jobs vj ON vj.task_id=bt.task_id
                        WHERE bt.type='batch_item' AND bt.mix_id=?
                          AND btv.aweme_id=tv.aweme_id
                          AND vj.status='failed'
                    ) THEN 'failed'
                    ELSE 'none'
                END AS summary_status
            FROM task_videos tv
            WHERE tv.task_id=?
            """,
            (parent_task_id, parent_task_id, parent_task_id, parent_task_id),
        )
        return {str(row[0]): {"summary_status": str(row[1] or "none")} for row in rows}

    async def get_task_transcript_statuses(self, task_ids: List[str]) -> Dict[str, str]:
        """批量读取口播状态，蒸馏产物与普通口播作业都视为已有结果。"""
        statuses: Dict[str, str] = {}
        for start in range(0, len(task_ids), 500):
            batch = task_ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = await self.fetch_all(
                f"SELECT task_id, status, original_transcript, polished_text FROM transcript_jobs "
                f"WHERE task_id IN ({placeholders})",
                tuple(batch),
            )
            for task_id, status, original, polished in rows:
                if status == "completed" and (original or polished):
                    statuses[task_id] = "completed"
                elif status in {"queued", "running"}:
                    statuses[task_id] = "running"
                elif status == "failed":
                    statuses[task_id] = "failed"
            distilled = await self.fetch_all(
                f"SELECT DISTINCT task_id FROM distill_items WHERE task_id IN ({placeholders}) "
                "AND (NULLIF(original_transcript, '') IS NOT NULL OR NULLIF(polished_text, '') IS NOT NULL)",
                tuple(batch),
            )
            for (task_id,) in distilled:
                statuses[task_id] = "completed"
        return statuses

    async def delete_task(self, task_id: str) -> bool:
        """删除任务"""
        # Delete derived jobs explicitly for databases created before FK
        # enforcement was enabled, then remove videos and the parent task.
        await self.execute("DELETE FROM transcript_jobs WHERE task_id=?", (task_id,))
        await self.execute("DELETE FROM video_summary_jobs WHERE task_id=?", (task_id,))
        await self.execute("DELETE FROM task_videos WHERE task_id=?", (task_id,))
        await self.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        await self.commit()
        return True

    async def recover_interrupted_jobs(self) -> Dict[str, int]:
        """Mark in-flight AI jobs as retryable failures after a restart."""
        now = datetime.now().isoformat()
        transcript_error = json.dumps({
            "stage": "interrupted", "code": "JOB_INTERRUPTED",
            "reason": "服务重启或异常退出，中断了口播提取作业。",
            "action": "点击重试重新提取。",
        }, ensure_ascii=False)
        summary_error = json.dumps({
            "stage": "interrupted", "code": "JOB_INTERRUPTED",
            "reason": "服务重启或异常退出，中断了视频总结作业。",
            "action": "点击重试重新生成总结。",
        }, ensure_ascii=False)
        transcript_cursor = await self.execute(
            "UPDATE transcript_jobs SET status='failed', stage='interrupted', error_json=?, updated_at=? WHERE status IN ('queued','running')",
            (transcript_error, now),
        )
        summary_cursor = await self.execute(
            "UPDATE video_summary_jobs SET status='failed', stage='interrupted', error_json=?, updated_at=? WHERE status IN ('queued','running')",
            (summary_error, now),
        )
        await self.commit()
        return {
            "transcript": transcript_cursor.rowcount,
            "summary": summary_cursor.rowcount,
        }

    # ==================== 视频操作 ====================

    async def add_video(self, video_info: TaskVideoInfo) -> int:
        """
        添加视频信息 - 参考F2的add_video_info设计

        Returns:
            video_id: 视频记录ID
        """
        await self.execute(
            """
            INSERT OR REPLACE INTO task_videos 
            (task_id, aweme_id, desc, nickname, sec_user_id,
             create_time, status, file_path, file_size, error_message, download_time,
             video_play_addr, cover, duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_info.task_id,
                video_info.aweme_id,
                video_info.desc,
                video_info.nickname,
                video_info.sec_user_id,
                video_info.create_time,
                video_info.status,
                video_info.file_path,
                video_info.file_size,
                video_info.error_message,
                video_info.download_time,
                video_info.video_play_addr,
                video_info.cover,
                video_info.duration,
            ),
        )
        await self.commit()

        # 获取最后插入的ID
        cursor = await self.execute("SELECT last_insert_rowid()")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def add_videos_batch(self, videos: List[TaskVideoInfo]) -> None:
        """批量添加视频信息"""
        for video in videos:
            await self.add_video(video)

    async def get_task_videos(
        self, task_id: str, status: Optional[str] = None
    ) -> List[TaskVideoInfo]:
        """获取任务的所有视频，可选按状态过滤"""
        if status:
            rows = await self.fetch_all(
                "SELECT * FROM task_videos WHERE task_id=? AND status=? ORDER BY create_time",
                (task_id, status),
            )
        else:
            rows = await self.fetch_all(
                "SELECT * FROM task_videos WHERE task_id=? ORDER BY create_time",
                (task_id,),
            )
        videos = []
        for row in rows:
            # 兼容旧表结构（列数可能不足）
            row_len = len(row)
            videos.append(
                TaskVideoInfo(
                    id=row[0] if row_len > 0 else 0,
                    task_id=row[1] if row_len > 1 else task_id,
                    aweme_id=row[2] if row_len > 2 else "",
                    desc=row[3] if row_len > 3 else "",
                    nickname=row[4] if row_len > 4 else "",
                    sec_user_id=row[5] if row_len > 5 else None,
                    create_time=row[6] if row_len > 6 else datetime.now().isoformat(),
                    status=row[7] if row_len > 7 else "pending",
                    file_path=row[8] if row_len > 8 else None,
                    file_size=row[9] if row_len > 9 else None,
                    error_message=row[10] if row_len > 10 else None,
                    download_time=row[11] if row_len > 11 else None,
                    video_play_addr=row[12] if row_len > 12 else None,
                    cover=row[13] if row_len > 13 else None,
                    duration=row[14] if row_len > 14 else None,
                )
            )
        return videos

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
        download_time = datetime.now().isoformat() if status == "downloaded" else None

        await self.execute(
            """
            UPDATE task_videos 
            SET status=?, file_path=?, file_size=?, error_message=?, download_time=?
            WHERE task_id=? AND aweme_id=?
            """,
            (
                status,
                file_path,
                file_size,
                error_message,
                download_time,
                task_id,
                aweme_id,
            ),
        )
        await self.commit()

    # ==================== 文案提取作业 ====================

    async def create_transcript_job(self, job_id: str, task_id: str) -> None:
        now = datetime.now().isoformat()
        await self.execute(
            """
            INSERT INTO transcript_jobs
            (job_id, task_id, status, stage, progress, created_at, updated_at)
            VALUES (?, ?, 'queued', 'queued', 0, ?, ?)
            """,
            (job_id, task_id, now, now),
        )
        await self.commit()

    async def get_transcript_job(self, job_id: str = "", task_id: str = "") -> Optional[Dict[str, Any]]:
        if job_id:
            row = await self.fetch_one("SELECT * FROM transcript_jobs WHERE job_id=?", (job_id,))
        elif task_id:
            row = await self.fetch_one("SELECT * FROM transcript_jobs WHERE task_id=?", (task_id,))
        else:
            return None
        if not row:
            return None
        return {
            "job_id": row[0],
            "task_id": row[1],
            "status": row[2],
            "stage": row[3],
            "progress": row[4] or 0,
            "original_transcript": row[5] or "",
            "polished_text": row[6] or "",
            "segments": json.loads(row[7]) if row[7] else [],
            "error": json.loads(row[8]) if row[8] else None,
            "created_at": row[9],
            "updated_at": row[10],
            "completed_at": row[11],
            "prepared_bytes": row[12] if len(row) > 12 else None,
            "processing_mode": row[13] if len(row) > 13 else None,
            "segment_count": row[14] if len(row) > 14 else None,
        }

    async def update_transcript_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "status", "stage", "progress", "original_transcript", "polished_text",
            "segments_json", "error_json", "completed_at",
        }
        updates = []
        params: List[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            updates.append(f"{key}=?")
            params.append(value)
        if not updates:
            return
        updates.append("updated_at=?")
        params.append(datetime.now().isoformat())
        params.append(job_id)
        await self.execute(f"UPDATE transcript_jobs SET {', '.join(updates)} WHERE job_id=?", tuple(params))
        await self.commit()

    # ==================== 视频总结作业 ====================

    async def create_video_summary_job(self, job_id: str, task_id: str, model: str, input_bytes: int) -> None:
        now = datetime.now().isoformat()
        await self.execute(
            """
            INSERT INTO video_summary_jobs
            (job_id, task_id, status, stage, progress, model, input_bytes, created_at, updated_at)
            VALUES (?, ?, 'queued', 'queued', 0, ?, ?, ?, ?)
            """,
            (job_id, task_id, model, input_bytes, now, now),
        )
        await self.commit()

    async def get_video_summary_job(self, job_id: str = "", task_id: str = "") -> Optional[Dict[str, Any]]:
        if job_id:
            row = await self.fetch_one("SELECT * FROM video_summary_jobs WHERE job_id=?", (job_id,))
        elif task_id:
            row = await self.fetch_one("SELECT * FROM video_summary_jobs WHERE task_id=?", (task_id,))
        else:
            return None
        if not row:
            return None
        return {
            "job_id": row[0],
            "task_id": row[1],
            "status": row[2],
            "stage": row[3],
            "progress": row[4] or 0,
            "model": row[5] or "",
            "input_bytes": row[6] or 0,
            "summary": json.loads(row[7]) if row[7] else None,
            "error": json.loads(row[8]) if row[8] else None,
            "created_at": row[9],
            "updated_at": row[10],
            "completed_at": row[11],
            "prepared_bytes": row[12] if len(row) > 12 else None,
            "processing_mode": row[13] if len(row) > 13 else None,
            "segment_count": row[14] if len(row) > 14 else None,
            "provider_attempts": json.loads(row[15]) if len(row) > 15 and row[15] else [],
        }

    async def update_video_summary_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "status", "stage", "progress", "model", "input_bytes", "summary_json", "error_json", "completed_at",
            "prepared_bytes", "processing_mode", "segment_count", "provider_attempts_json",
        }
        updates = []
        params: List[Any] = []
        for key, value in fields.items():
            if key in allowed:
                updates.append(f"{key}=?")
                params.append(value)
        if not updates:
            return
        updates.append("updated_at=?")
        params.append(datetime.now().isoformat())
        params.append(job_id)
        await self.execute(f"UPDATE video_summary_jobs SET {', '.join(updates)} WHERE job_id=?", tuple(params))
        await self.commit()

    # ==================== 统计和清理 ====================

    async def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        total = await self.fetch_one("SELECT COUNT(*) FROM tasks")
        pending = await self.fetch_one(
            "SELECT COUNT(*) FROM tasks WHERE status='pending'"
        )
        downloading = await self.fetch_one(
            "SELECT COUNT(*) FROM tasks WHERE status='downloading'"
        )
        completed = await self.fetch_one(
            "SELECT COUNT(*) FROM tasks WHERE status='completed'"
        )
        failed = await self.fetch_one(
            "SELECT COUNT(*) FROM tasks WHERE status='failed'"
        )

        return {
            "total": total[0] if total else 0,
            "pending": pending[0] if pending else 0,
            "downloading": downloading[0] if downloading else 0,
            "completed": completed[0] if completed else 0,
            "failed": failed[0] if failed else 0,
        }

    async def cleanup_old_tasks(self, days: int = 30) -> int:
        """清理旧任务"""
        cutoff = datetime.now().timestamp() - (days * 24 * 60 * 60)
        # 获取要删除的任务ID
        rows = await self.fetch_all(
            """
            SELECT task_id FROM tasks 
            WHERE (status='completed' OR status='failed') 
            AND created_at < datetime(?, 'unixepoch')
            """,
            (cutoff,),
        )

        count = len(rows)
        for row in rows:
            await self.delete_task(row[0])

        return count

    async def export_task_metadata(self, task_id: str) -> Dict[str, Any]:
        """导出任务元数据"""
        task = await self.get_task(task_id)
        if not task:
            return {}

        videos = await self.get_task_videos(task_id)

        return {
            "task": task.to_dict(),
            "videos": [v.to_dict() for v in videos],
            "export_time": datetime.now().isoformat(),
        }

    # 上下文管理器支持
    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
