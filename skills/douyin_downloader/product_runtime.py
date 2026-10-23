"""内容工作台与已有作业的少量连接，保持 Web 路由模块独立。"""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

try:
    from .usage_ledger import content_hash, current_request_context, model_request_context
except ImportError:
    from usage_ledger import content_hash, current_request_context, model_request_context


class ProductRuntime:
    def __init__(self, web: Any, results: Any, ledger: Any):
        self.web, self.results, self.ledger = web, results, ledger

    async def track(self, task: Any, stage: str, job: dict, mode: str) -> str:
        current = current_request_context()
        kind, batch_id = current.batch_kind, current.batch_id or task.id
        prior = await self.results.execution(job.get("job_id", ""), stage)
        execution_id = uuid.uuid4().hex if mode == "new" else (prior or {}).get("execution_id", "")
        await self.results.track(kind=kind, batch_id=batch_id, task_id=task.id,
            aweme_id=str(task.video_info.get("aweme_id") or ""), stage=stage,
            job_id=job.get("job_id", ""), execution_id=execution_id, mode=mode)
        await self.ledger.bind_execution(execution_id, task.id)
        return execution_id

    async def register_author(self, stage: str, batch: dict, tasks: list, pending: list[str], request_limit: Optional[int]) -> None:
        await self.results.register_batch(stage, batch, [task.id for task in tasks])
        await self.ledger.set_limit(stage, batch["batch_id"], request_limit)
        with model_request_context(batch_kind=stage, batch_id=batch["batch_id"]):
            for task in tasks:
                if task.id in pending:
                    continue
                response = await (self.web.get_video_summary(task.id) if stage == "summary" else self.web.get_transcript(task.id))
                if response.data:
                    await self.track(task, stage, response.data, "reused" if response.data["status"] == "completed" else "shared")

    async def clear_checkpoints(self, task_id: str) -> None:
        await self.ledger.delete_task_checkpoints(task_id)

    async def resolve_media(self, aweme_id: str) -> Optional[Path]:
        """沿用原播放器的目录与文件校验，不将数据库中的任意路径直接公开。"""
        rows = await self.web.task_manager.db.fetch_all(
            "SELECT task_id FROM task_videos WHERE aweme_id=? ORDER BY id DESC", (aweme_id,))
        for (task_id,) in rows:
            task = await self.web.task_manager.get_task(task_id)
            if not task:
                continue
            try:
                return await self.web._resolve_task_video_path(task, "播放", aweme_id=aweme_id, require_completed=False)
            except HTTPException:
                continue
        rows = await self.web.task_manager.db.fetch_all(
            "SELECT d.local_path,r.options_json FROM distill_items d JOIN distill_runs r USING(run_id) "
            "WHERE d.aweme_id=? AND d.local_path IS NOT NULL ORDER BY d.updated_at DESC", (aweme_id,))
        for value, options in rows:
            roots = [Path(self.web.skill.douyin_conf.get("path", "Download")).expanduser().resolve()]
            try:
                saved_root = json.loads(options or "{}").get("save_root")
                if saved_root:
                    roots.append(Path(saved_root).expanduser().resolve())
            except (ValueError, TypeError):
                pass
            for root in roots:
                path = self.web._safe_video_candidate(Path(value).expanduser(), root)
                if path:
                    return path
        return None

    async def generate_topic(self, payload: dict) -> dict:
        """仅向已配置的文本模型发送用户预览确认的专题资料。"""
        version_id, topic_id = payload["version_id"], payload["topic_id"]
        request_limit = payload.get("request_limit")
        if not payload.get("preserve_limit", False):
            # 新版本确认页留空表示明确取消旧上限；恢复旧版本则保留用户刚调整的额度。
            await self.ledger.set_limit("topic", topic_id, request_limit)
            await self.ledger.resume("topic", topic_id)
        prompt_hash = content_hash(payload["prompt"])
        checkpoint = await self.ledger.get_checkpoint(version_id, "topic", prompt_hash)
        if checkpoint:
            return checkpoint
        synthesis = self.web.DistillSynthesis(model="gpt-5.6-luna")
        with model_request_context(batch_kind="topic", batch_id=topic_id, execution_id=version_id, stage="topic"):
            text = await synthesis._chat(payload["prompt"])
        result = {"output_markdown": text, "model": synthesis.model, "provider": "text"}
        await self.ledger.save_checkpoint(version_id, "topic", prompt_hash, result)
        return result

    async def resume(self, kind: str, batch_id: str) -> dict:
        usage = await self.ledger.stats(kind, batch_id)
        if usage.get("request_limit") is not None and usage["request_count"] >= usage["request_limit"]:
            raise HTTPException(409, detail="请求次数上限仍已用完，请先提高上限或取消上限后继续")
        await self.ledger.resume(kind, batch_id)
        web = self.web
        if kind == "distill":
            if web.distill_service is None:
                raise HTTPException(503, detail="蒸馏服务尚未就绪")
            await web.distill_service.resume(batch_id)
            return {"status": "running"}
        if kind == "topic":
            version = await web.content_service.resume_generation(batch_id)
            return {"status": version["status"], "version_id": version["id"]}
        if kind in {"summary", "transcript"}:
            manifest = await self.results.get_manifest(kind, batch_id)
            if not manifest:
                raise HTTPException(404, detail="批次不存在")
            cache, active, runner = (web.summary_batches, web.running_summary_batches, web._run_author_summary_batch) if kind == "summary" else (web.transcript_batches, web.running_transcript_batches, web._run_author_transcript_batch)
            if batch_id in active and not active[batch_id].done():
                return {"status": "running"}
            pending = []
            for task_id in manifest["task_ids"]:
                job = await (web.task_manager.db.get_video_summary_job(task_id=task_id) if kind == "summary" else web.task_manager.db.get_transcript_job(task_id=task_id))
                if job and job["status"] == "completed":
                    continue
                if job and job["status"] == "paused":
                    await self._resume_job(job, kind)
                pending.append(task_id)
            cache[batch_id] = {"batch_id": batch_id, "author": manifest["author"], "status": "running",
                "total": len(pending), "processed": 0, "completed": 0, "reused": 0, "failed": 0,
                "current_task_id": None, "skipped_summarized": 0, "skipped_transcribed": 0, "skipped_running": 0}
            await self.results.set_status(kind, batch_id, "running")
            active[batch_id] = asyncio.create_task(runner(batch_id, pending))
            return {"status": "running", "pending": len(pending)}
        resumed = 0
        for stage, table in (("summary", "video_summary_jobs"), ("transcript", "transcript_jobs")):
            rows = await self.results._rows(f"SELECT j.* FROM {table} j JOIN analysis_executions e "
                "ON e.job_id=j.job_id AND e.stage=? WHERE e.batch_kind=? AND e.batch_id=? AND j.status='paused'", (stage, kind, batch_id))
            for job in rows:
                resumed += int(await self._resume_job(job, stage))
        return {"status": "running" if resumed else "completed", "resumed": resumed}

    async def _resume_job(self, job: dict, stage: str) -> bool:
        web = self.web
        active = web.running_summary_tasks if stage == "summary" else web.running_transcription_tasks
        if job["job_id"] in active and not active[job["job_id"]].done():
            return False
        task = await web.task_manager.get_task(job["task_id"])
        if not task:
            return False
        path = await web._resolve_task_video_path(task, "恢复分析")
        owner = await self.results.execution(job["job_id"], stage)
        if not owner:
            return False
        await self.results.finish_execution(job["job_id"], stage, "pending")
        update = web.task_manager.db.update_video_summary_job if stage == "summary" else web.task_manager.db.update_transcript_job
        await update(job["job_id"], status="queued", stage="queued", error_json=None)
        runner = web._run_video_summary_job if stage == "summary" else web._run_transcription_job
        with model_request_context(batch_kind=owner["batch_kind"], batch_id=owner["batch_id"],
                                   execution_id=owner["execution_id"], stage=stage):
            active[job["job_id"]] = asyncio.create_task(runner(job["job_id"], job["task_id"], path))
        return True

    async def retry(self, kind: str, batch_id: str, task_id: str) -> dict:
        if kind == "download":
            belongs = await self.results._rows("SELECT 1 FROM download_queue_items WHERE batch_id=? AND task_id=?", (batch_id, task_id))
        else:
            manifest = await self.results.get_manifest(kind, batch_id)
            belongs = bool(manifest and task_id in manifest["task_ids"])
        if not belongs:
            raise HTTPException(404, detail="该视频不在所选批次中")
        started = []
        with model_request_context(batch_kind=kind, batch_id=batch_id):
            for stage in ("transcript", "summary"):
                getter = self.web.task_manager.db.get_video_summary_job if stage == "summary" else self.web.task_manager.db.get_transcript_job
                job = await getter(task_id=task_id)
                if not job or job["status"] != "failed":
                    continue
                # 重试沿用成功阶段检查点，主动强制重新分析仍会产生新执行。
                if await self._resume_job(job, stage):
                    started.append(stage)
        return {"status": "running" if started else "unchanged", "stages": started}
