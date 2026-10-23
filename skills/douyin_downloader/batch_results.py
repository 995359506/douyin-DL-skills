"""持久化批次与分析归属；下载、口播和总结分别报告。"""

import asyncio
import json
import math
from collections import Counter
from datetime import datetime
from typing import Any, Optional

import aiosqlite


class BatchResults:
    """仅保存运行关联，不复制来源正文，也不从旧记录推算历史调用数。"""

    def __init__(self, db_path: str, ledger: Any = None):
        self.db_path = db_path
        self.ledger = ledger
        self.conn: Optional[aiosqlite.Connection] = None
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA busy_timeout=10000")
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS analysis_batches (
                kind TEXT NOT NULL, batch_id TEXT NOT NULL, author TEXT NOT NULL,
                status TEXT NOT NULL, task_ids_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(kind,batch_id)
            );
            CREATE TABLE IF NOT EXISTS analysis_executions (
                job_id TEXT NOT NULL, stage TEXT NOT NULL, execution_id TEXT NOT NULL,
                batch_kind TEXT NOT NULL, batch_id TEXT NOT NULL, task_id TEXT NOT NULL,
                PRIMARY KEY(job_id,stage)
            );
            CREATE TABLE IF NOT EXISTS batch_analysis_links (
                kind TEXT NOT NULL, batch_id TEXT NOT NULL, task_id TEXT NOT NULL,
                aweme_id TEXT NOT NULL, stage TEXT NOT NULL, job_id TEXT,
                execution_id TEXT, mode TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(kind,batch_id,task_id,stage)
            );
            CREATE INDEX IF NOT EXISTS idx_batch_analysis_execution
            ON batch_analysis_links(execution_id);
            CREATE TABLE IF NOT EXISTS analysis_execution_timings (
                execution_id TEXT PRIMARY KEY, stage TEXT NOT NULL,
                active_started_at TEXT, active_seconds REAL NOT NULL DEFAULT 0,
                queue_seconds REAL, outcome TEXT NOT NULL DEFAULT 'running',
                completed_at TEXT
            );
        """)
        columns = {row[1] for row in await (await self.conn.execute("PRAGMA table_info(batch_analysis_links)")).fetchall()}
        if "result_status" not in columns:
            await self.conn.execute("ALTER TABLE batch_analysis_links ADD COLUMN result_status TEXT")
        await self.conn.execute("UPDATE analysis_batches SET status='paused' WHERE status='running'")
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.conn.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def register_batch(self, kind: str, batch: dict, task_ids: list[str]) -> None:
        async with self.lock:
            await self.conn.execute(
                "INSERT OR REPLACE INTO analysis_batches VALUES(?,?,?,?,?,?)",
                (kind, batch["batch_id"], batch["author"], batch["status"],
                 json.dumps(task_ids), datetime.now().isoformat()),
            )
            await self.conn.commit()

    async def set_status(self, kind: str, batch_id: str, status: str) -> None:
        async with self.lock:
            await self.conn.execute("UPDATE analysis_batches SET status=? WHERE kind=? AND batch_id=?", (status, kind, batch_id))
            await self.conn.commit()

    async def get_manifest(self, kind: str, batch_id: str) -> Optional[dict]:
        rows = await self._rows("SELECT * FROM analysis_batches WHERE kind=? AND batch_id=?", (kind, batch_id))
        if not rows:
            return None
        value = rows[0]
        value["task_ids"] = json.loads(value.pop("task_ids_json"))
        return value

    async def execution(self, job_id: str, stage: str) -> Optional[dict]:
        rows = await self._rows("SELECT * FROM analysis_executions WHERE job_id=? AND stage=?", (job_id, stage))
        return rows[0] if rows else None

    async def track(self, *, kind: str, batch_id: str, task_id: str, aweme_id: str,
                    stage: str, job_id: str = "", execution_id: str = "", mode: str = "new") -> None:
        async with self.lock:
            if mode == "new" and job_id:
                await self.conn.execute("INSERT OR REPLACE INTO analysis_executions VALUES(?,?,?,?,?,?)",
                    (job_id, stage, execution_id, kind, batch_id, task_id))
            await self.conn.execute("""
                INSERT INTO batch_analysis_links(kind,batch_id,task_id,aweme_id,stage,job_id,execution_id,mode,created_at,result_status) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(kind,batch_id,task_id,stage) DO UPDATE SET
                    job_id=excluded.job_id, execution_id=excluded.execution_id,
                    mode=CASE WHEN batch_analysis_links.execution_id=excluded.execution_id
                              AND batch_analysis_links.mode='new' THEN 'new' ELSE excluded.mode END,
                    result_status=CASE WHEN batch_analysis_links.execution_id=excluded.execution_id
                                       AND excluded.mode!='reused' THEN batch_analysis_links.result_status ELSE excluded.result_status END
            """, (kind, batch_id, task_id, aweme_id, stage, job_id, execution_id, mode, datetime.now().isoformat(), "reused" if mode == "reused" else None))
            await self.conn.commit()

    async def finish_execution(self, job_id: str, stage: str, status: str) -> None:
        """冻结当次执行结局，之后同一 job_id 的强制重做不改旧批次结果。"""
        async with self.lock:
            owner = await self.execution(job_id, stage)
            if not owner:
                return
            await self.conn.execute("UPDATE batch_analysis_links SET result_status=? WHERE execution_id=(SELECT execution_id FROM analysis_executions WHERE job_id=? AND stage=?) AND stage=? AND mode!='reused'", (status, job_id, stage, stage))
            timings = await self._rows("SELECT * FROM analysis_execution_timings WHERE execution_id=?", (owner["execution_id"],))
            if timings:
                timing = timings[0]
                elapsed = timing["active_seconds"] + self._elapsed(timing["active_started_at"], datetime.now().isoformat())
                await self.conn.execute("UPDATE analysis_execution_timings SET active_started_at=NULL,active_seconds=?,outcome=?,completed_at=? WHERE execution_id=?", (elapsed, status, datetime.now().isoformat() if status == "completed" else None, owner["execution_id"]))
            await self.conn.commit()

    @staticmethod
    def _elapsed(start: Optional[str], end: str) -> float:
        try:
            return max(0.0, datetime.fromisoformat(end).timestamp() - datetime.fromisoformat(start).timestamp()) if start else 0.0
        except (TypeError, ValueError):
            return 0.0

    async def observe_execution(self, job_id: str, stage: str, phase: str) -> None:
        """在服务拿到执行槽后起钟，排队和用户暂停不算活跃处理时间。"""
        if phase in {"queued", "completed", "failed", "cancelled", "budget_paused"}:
            return
        async with self.lock:
            owner = await self.execution(job_id, stage)
            if not owner:
                return
            rows = await self._rows("SELECT * FROM analysis_execution_timings WHERE execution_id=?", (owner["execution_id"],))
            now = datetime.now().isoformat()
            if not rows:
                links = await self._rows("SELECT MIN(created_at) AS created_at FROM batch_analysis_links WHERE execution_id=?", (owner["execution_id"],))
                queued_at = links[0]["created_at"] if links else None
                await self.conn.execute("INSERT INTO analysis_execution_timings(execution_id,stage,active_started_at,queue_seconds) VALUES(?,?,?,?)", (owner["execution_id"], stage, now, self._elapsed(queued_at, now)))
            elif rows[0]["active_started_at"] is None:
                await self.conn.execute("UPDATE analysis_execution_timings SET active_started_at=?,outcome='running',completed_at=NULL WHERE execution_id=?", (now, owner["execution_id"]))
            await self.conn.commit()

    async def _eta(self, items: list[dict], batch: dict) -> dict:
        analysis = {}
        for stage, concurrency in (("summary", 5), ("transcript", 1)):
            samples = [row["active_seconds"] for row in await self._rows("SELECT active_seconds FROM analysis_execution_timings WHERE stage=? AND outcome='completed' AND active_seconds>0 ORDER BY completed_at DESC LIMIT 200", (stage,))]
            ready = sum(item[stage] == "pending" and item["download"] in {"completed", "reused"} for item in items)
            waiting_download = sum(item[stage] == "pending" and item["download"] not in {"completed", "reused"} for item in items)
            paused = batch["status"] in {"paused", "cancelled"} or any(item[stage] == "paused" for item in items)
            estimate = None
            if len(samples) >= 10 and ready and not paused:
                samples.sort()
                waves = math.ceil(ready / concurrency)
                estimate = [round(samples[int((len(samples)-1)*.1)]*waves, 1), round(samples[int((len(samples)-1)*.9)]*waves, 1)]
            analysis[stage] = {"remaining_seconds_range": estimate, "sample_count": len(samples), "ready_pending": ready, "awaiting_download": waiting_download, "concurrency_assumption": concurrency, "basis": "最近最多200个同类成功逻辑执行的活跃处理时长p10–p90；含预处理、网络和重试，排除排队与用户暂停", "note": "仅估算文件已就绪的分析；未就绪下载、其他批次竞争和未来重试会影响实际完成时间"}
        return {"download": None, "analysis": analysis, "message": "下载与分析可同时进行，不相加。每类至少10个成功处理样本才估算；下载缺少独立耗时样本时保持未知。"}

    async def list_batches(self) -> list[dict]:
        rows = await self._rows("""
            SELECT 'download' AS kind,batch_id,author,status,created_at,
                (SELECT count(*) FROM download_queue_items i WHERE i.batch_id=b.batch_id) AS total
            FROM download_queue_batches b
            UNION ALL
            SELECT kind,batch_id,author,status,created_at,json_array_length(task_ids_json) FROM analysis_batches
            UNION ALL
            SELECT 'distill',run_id,nickname,status,created_at,total_count FROM distill_runs
            ORDER BY created_at DESC
        """)
        singles = await self._rows("SELECT kind,batch_id,'' AS author,CASE WHEN SUM(result_status='paused')>0 THEN 'paused' WHEN SUM(result_status IS NULL)>0 THEN 'running' ELSE 'completed' END AS status,MIN(created_at) AS created_at,COUNT(DISTINCT task_id) AS total FROM batch_analysis_links WHERE kind='single' GROUP BY kind,batch_id")
        rows.extend(singles)
        exists = await self._rows("SELECT name FROM sqlite_master WHERE type='table' AND name='content_topics'")
        if exists:
            rows.extend(await self._rows("SELECT 'topic' AS kind,topic_id AS batch_id,name AS author,COALESCE((SELECT status FROM content_topic_versions v WHERE v.topic_id=t.topic_id ORDER BY created_at DESC LIMIT 1),'not_started') AS status,created_at,(SELECT COUNT(*) FROM content_topic_versions v WHERE v.topic_id=t.topic_id) AS total FROM content_topics t"))
        return sorted(rows, key=lambda row: row.get("created_at") or "", reverse=True)

    @staticmethod
    def _analysis_status(job: Optional[dict], link: Optional[dict], requested: bool) -> str:
        if link and link.get("mode") == "reused":
            return "reused"
        if link and link.get("result_status"):
            state = link["result_status"]
            return "shared" if state == "completed" and link.get("mode") == "shared" else state
        if job and job.get("status") == "completed":
            return "reused" if link and link["mode"] == "reused" else "shared" if link and link["mode"] == "shared" else "completed"
        if job and job.get("status") in {"queued", "running", "paused", "failed", "cancelled"}:
            if job["status"] == "failed" and job.get("original_transcript"):
                return "partial"
            return "pending" if job["status"] in {"queued", "running"} else job["status"]
        return "pending" if requested else "not_requested"

    async def report(self, kind: str, batch_id: str, *, status: str = "", author: str = "",
                     page: int = 1, page_size: int = 30) -> dict:
        batch = next((b for b in await self.list_batches() if b["kind"] == kind and b["batch_id"] == batch_id), None)
        if batch is None:
            raise KeyError(batch_id)
        links = await self._rows("SELECT * FROM batch_analysis_links WHERE kind=? AND batch_id=?", (kind, batch_id))
        link_by = {(v["task_id"], v["stage"]): v for v in links}
        if kind == "download":
            rows = await self._rows("SELECT * FROM download_queue_items WHERE batch_id=? ORDER BY position", (batch_id,))
            info = (await self._rows("SELECT auto_summary FROM download_queue_batches WHERE batch_id=?", (batch_id,)))[0]
        elif kind == "distill":
            rows = await self._rows("SELECT * FROM distill_items WHERE run_id=? ORDER BY ordinal", (batch_id,))
            info = {}
        else:
            manifest = await self.get_manifest(kind, batch_id)
            rows = [{"task_id": task_id} for task_id in manifest["task_ids"]] if manifest else list({row["task_id"]: {"task_id": row["task_id"], "aweme_id": row["aweme_id"]} for row in links}.values())
            info = {}
        # 批次通常较小；成组查询，避免每条记录触发数据库查询。
        task_ids = list(dict.fromkeys(row.get("task_id") for row in rows if row.get("task_id")))
        videos, jobs = {}, {"transcript": {}, "summary": {}}
        for start in range(0, len(task_ids), 500):
            group = task_ids[start:start + 500]
            placeholders = ",".join("?" for _ in group)
            for video in await self._rows(f"SELECT * FROM task_videos WHERE task_id IN ({placeholders})", tuple(group)):
                videos[(video["task_id"], video["aweme_id"])] = video
                videos.setdefault(video["task_id"], video)
            for stage, table in (("transcript", "transcript_jobs"), ("summary", "video_summary_jobs")):
                for job in await self._rows(f"SELECT * FROM {table} WHERE task_id IN ({placeholders})", tuple(group)):
                    jobs[stage][job["task_id"]] = job
        items = []
        for row in rows:
            task_id = row.get("task_id") or ""
            video = videos.get((task_id, row.get("aweme_id")), videos.get(task_id, {}))
            try:
                meta = json.loads(row.get("metadata_json") or "{}") if kind == "distill" else {}
                meta = meta if isinstance(meta, dict) else {}
            except (ValueError, TypeError):
                meta = {}
            aweme_id = row.get("aweme_id") or video.get("aweme_id") or ""
            item = {"task_id": task_id, "aweme_id": aweme_id,
                    "title": video.get("desc") or meta.get("title") or meta.get("desc") or f"视频 {aweme_id}",
                    "author": video.get("nickname") or meta.get("nickname") or batch["author"],
                    "cover": video.get("cover") or meta.get("cover"), "actions": []}
            state = row.get("status") or row.get("download_status") or video.get("status") or "not_requested"
            item["download"] = {"skipped": "reused", "downloaded": "completed", "queued": "pending", "running": "pending"}.get(state, state)
            for stage in ("transcript", "summary"):
                if kind == "distill":
                    state = row.get(f"{stage}_status") or "pending"
                    item[stage] = {"ready": "completed", "done": "completed", "skipped": "not_requested", "reused": "reused"}.get(state, state)
                    if stage == "transcript" and state == "failed" and row.get("original_transcript"):
                        item[stage] = "partial"
                else:
                    link = link_by.get((task_id, stage))
                    job = jobs[stage].get(task_id)
                    requested = bool(link) or kind == stage or (stage == "summary" and bool(info.get("auto_summary")))
                    item[stage] = self._analysis_status(job, link, requested)
                    if job and job.get("error_json") and item[stage] in {"failed", "paused", "partial"}:
                        try:
                            error = json.loads(job["error_json"])
                            item["actions"].append({"stage": stage, "reason": error.get("reason", "处理未完成"), "action": error.get("action", "查看任务后重试")})
                        except (ValueError, TypeError):
                            pass
            items.append(item)
        counts = {stage: dict(Counter(v[stage] for v in items)) for stage in ("download", "transcript", "summary")}
        filtered = [v for v in items if (not author or v["author"] == author)
                    and (not status or status in (v["download"], v["transcript"], v["summary"]))]
        usage = await self.ledger.stats(kind, batch_id) if self.ledger else {"requests": None, "cost_status": "unknown"}
        limits = await self._rows("SELECT 1 FROM model_batch_limits WHERE batch_kind=? AND batch_id=?", (kind, batch_id)) if self.ledger else []
        if not links and not limits and not usage.get("request_count"):
            for key in ("request_count", "logical_execution_count", "retry_request_count", "fallback_request_count", "in_flight_count", "usage_known_count", "usage_unknown_count"):
                usage[key] = None
            usage["historical_coverage"] = "此批次早于调用台账或缺少执行关联，历史调用数与费用未知；不追溯推算为零"
            usage["providers"] = []
        return {"batch": batch, "counts": counts, "items": filtered[(page-1)*page_size:page*page_size],
                "total": len(filtered), "page": page, "page_size": page_size,
                "authors": sorted({v["author"] for v in items}), "usage": usage,
                "eta": await self._eta(items, batch)}
