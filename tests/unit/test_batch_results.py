import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from skills.douyin_downloader.batch_results import BatchResults
from skills.douyin_downloader.content_service import ContentService
from skills.douyin_downloader.db import TaskDB
from skills.douyin_downloader.product_runtime import ProductRuntime
from skills.douyin_downloader.usage_ledger import UsageLedger, current_request_context, model_request_context


@pytest_asyncio.fixture
async def results_workspace(tmp_path):
    path = str(tmp_path / "results.db")
    db = TaskDB(path)
    await db.connect()
    # 仅初始化队列表，不启动调度器或发出下载请求。
    await db.execute("CREATE TABLE download_queue_batches(batch_id TEXT PRIMARY KEY,author TEXT,status TEXT,created_at TEXT,auto_summary INTEGER)")
    await db.execute("CREATE TABLE download_queue_items(batch_id TEXT,position INTEGER,aweme_id TEXT,task_id TEXT,status TEXT)")
    await db.execute("INSERT INTO tasks(task_id,type,status,created_at) VALUES ('t1','single','completed','2026-01-01'),('t2','single','completed','2026-01-01')")
    await db.execute("INSERT INTO task_videos(task_id,aweme_id,desc,nickname,status) VALUES ('t1','1','视频一','甲','downloaded'),('t2','2','视频二','乙','downloaded')")
    await db.execute("INSERT INTO video_summary_jobs(job_id,task_id,status,summary_json,error_json,created_at,updated_at) VALUES ('s1','t1','failed',NULL,?,'n','n'),('s2','t2','completed',? ,NULL,'n','n')", (json.dumps({"reason": "上游暂不可用", "action": "稍后重试"}), json.dumps({"overview": "总结"})))
    await db.execute("INSERT INTO transcript_jobs(job_id,task_id,status,original_transcript,created_at,updated_at) VALUES ('tr1','t1','failed','已完成原始转写','n','n')")
    await db.execute("INSERT INTO download_queue_batches VALUES ('d1','作者','completed','2026-01-01',1)")
    await db.execute("INSERT INTO download_queue_items VALUES ('d1',0,'1','t1','completed'),('d1',1,'2','t2','skipped')")
    await db.commit()
    ledger = UsageLedger(path)
    await ledger.initialize()
    content = ContentService(db)
    await content.initialize()
    results = BatchResults(path, ledger)
    await results.initialize()
    yield results, db, ledger, content
    await results.close()
    await db.close()


@pytest.mark.asyncio
async def test_mixed_download_analysis_results_and_unknown_history(results_workspace):
    results, _, _, _ = results_workspace
    report = await results.report("download", "d1")
    assert report["counts"]["download"] == {"completed": 1, "reused": 1}
    first, second = report["items"]
    assert first["summary"] == "failed" and first["transcript"] == "partial"
    assert first["actions"][0]["reason"] == "上游暂不可用"
    assert second["summary"] == "completed"
    assert report["usage"]["request_count"] is None
    assert report["usage"]["cost_status"] == "unknown"
    filtered = await results.report("download", "d1", status="failed", author="甲")
    assert filtered["total"] == 1 and filtered["items"][0]["aweme_id"] == "1"


@pytest.mark.asyncio
async def test_reused_distill_snapshot_overrides_old_failed_job_without_new_call(results_workspace):
    results, _, ledger, _ = results_workspace
    await results.track(kind="download", batch_id="d1", task_id="t1", aweme_id="1", stage="summary", job_id="distill:run:1:summary", mode="reused")
    report = await results.report("download", "d1")
    assert report["items"][0]["summary"] == "reused"
    assert (await ledger.stats("download", "d1"))["request_count"] == 0


@pytest.mark.asyncio
async def test_shared_execution_has_one_owner_and_frozen_historical_status(results_workspace):
    results, db, ledger, _ = results_workspace
    await results.register_batch("summary", {"batch_id": "b1", "author": "甲", "status": "running"}, ["t1"])
    await results.register_batch("summary", {"batch_id": "b2", "author": "甲", "status": "running"}, ["t1"])
    await results.track(kind="summary", batch_id="b1", task_id="t1", aweme_id="1", stage="summary", job_id="s1", execution_id="execution-old", mode="new")
    await results.track(kind="summary", batch_id="b2", task_id="t1", aweme_id="1", stage="summary", job_id="s1", execution_id="execution-old", mode="shared")
    owner = await results.execution("s1", "summary")
    assert owner["batch_id"] == "b1"
    with model_request_context(batch_kind="summary", batch_id="b1", execution_id="execution-old"):
        request_id = await ledger.reserve(execution_id="execution-old", stage="summary", provider="test", model="offline")
        await ledger.finish(request_id, elapsed_seconds=1, status="succeeded")
    await results.finish_execution("s1", "summary", "completed")
    assert (await results.report("summary", "b1"))["items"][0]["summary"] == "completed"
    assert (await results.report("summary", "b2"))["items"][0]["summary"] == "shared"
    assert (await ledger.stats("summary", "b1"))["request_count"] == 1
    assert (await ledger.stats("summary", "b2"))["request_count"] == 0
    # 同 job_id 强制重做为新execution；旧批次不随当前作业变成pending/failed。
    await results.track(kind="single", batch_id="manual", task_id="t1", aweme_id="1", stage="summary", job_id="s1", execution_id="execution-new", mode="new")
    await db.execute("UPDATE video_summary_jobs SET status='running' WHERE job_id='s1'")
    await db.commit()
    assert (await results.report("summary", "b1"))["items"][0]["summary"] == "completed"
    assert (await results.report("single", "manual"))["items"][0]["summary"] == "pending"


@pytest.mark.asyncio
async def test_distill_without_task_preserves_metadata_and_stage_results(results_workspace):
    results, db, _, _ = results_workspace
    await db.create_distill_run("run1", sec_user_id="sec", nickname="独立作者", user_url="u")
    await db.add_distill_items("run1", [{"ordinal": 1, "aweme_id": "999", "metadata": {"title": "仅蒸馏资料的标题", "nickname": "独立作者"}}])
    await db.execute("UPDATE distill_items SET download_status='skipped',transcript_status='failed',original_transcript='保留的转写',summary_status='completed',summary_json=? WHERE run_id='run1'", (json.dumps({"overview": "总结"}),))
    await db.commit()
    report = await results.report("distill", "run1")
    item = report["items"][0]
    assert item["title"] == "仅蒸馏资料的标题" and item["author"] == "独立作者"
    assert item["download"] == "reused" and item["transcript"] == "partial" and item["summary"] == "completed"


@pytest.mark.asyncio
async def test_resume_paused_job_reuses_execution_and_checkpoint(results_workspace, tmp_path):
    results, db, ledger, _ = results_workspace
    await results.track(kind="download", batch_id="d1", task_id="t1", aweme_id="1", stage="summary", job_id="s1", execution_id="keep-execution", mode="new")
    await ledger.save_checkpoint("keep-execution", "segment:0", "input-hash", {"overview": "已成功的片段"})
    path = tmp_path / "video.mp4"
    path.write_bytes(b"offline fixture")
    captured = []

    async def task(_):
        return SimpleNamespace(id="t1", video_info={"aweme_id": "1"})

    async def resolve(*_, **__):
        return path

    async def run(job_id, task_id, source):
        captured.append(current_request_context())
        assert await ledger.get_checkpoint("different-fallback", "segment:0", "input-hash") == {"overview": "已成功的片段"}

    web = SimpleNamespace(task_manager=SimpleNamespace(db=db, get_task=task), running_summary_tasks={}, running_transcription_tasks={}, _resolve_task_video_path=resolve, _run_video_summary_job=run)
    runtime = ProductRuntime(web, results, ledger)
    assert await runtime._resume_job({"job_id": "s1", "task_id": "t1", "status": "paused"}, "summary")
    await asyncio.gather(*web.running_summary_tasks.values())
    assert captured[0].execution_id == "keep-execution" and captured[0].batch_id == "d1"
    assert (await ledger.stats("download", "d1"))["request_count"] == 0


@pytest.mark.asyncio
async def test_topic_pause_and_resume_same_version_snapshot(results_workspace):
    _, _, _, content = results_workspace
    from skills.douyin_downloader.usage_ledger import BatchRequestLimitReached

    calls = []

    async def generator(payload):
        calls.append(payload)
        if len(calls) == 1:
            raise BatchRequestLimitReached("topic", payload["topic_id"], 0)
        return {"output_markdown": "只归纳原选中资料 [S1]"}

    content.generator = generator
    topic = await content.create_topic({"name": "原目标", "goal": "原目标"})
    member = await content.add_member(topic["id"], {"text": "原选中的资料"})
    first = await content.generate(topic["id"], {"member_ids": [member["id"]], "settings": {"tone": "自然"}})
    assert first["status"] == "paused"
    await content.update_topic(topic["id"], {"goal": "已改变的目标"})
    await content.remove_member(topic["id"], member["id"])
    resumed = await content.resume_generation(topic["id"])
    assert resumed["id"] == first["id"] and resumed["status"] == "completed"
    assert calls[1]["inputs"] == calls[0]["inputs"]
    assert calls[1]["goal"] == "原目标"
    assert calls[0]["preserve_limit"] is False and calls[1]["preserve_limit"] is True
    assert len((await content.get_topic(topic["id"]))["versions"]) == 1


@pytest.mark.asyncio
async def test_new_topic_version_clears_old_cap_but_resume_preserves_adjusted_cap(results_workspace):
    results, _, ledger, content = results_workspace
    from skills.douyin_downloader.usage_ledger import BatchRequestLimitReached

    class OfflineSynthesis:
        def __init__(self, model):
            self.model = model

        async def _chat(self, prompt):
            request = await ledger.reserve(execution_id="fallback", stage="topic", provider="offline", model=self.model)
            await ledger.finish(request, elapsed_seconds=1, status="succeeded")
            return "离线验证资料归纳 [S1]"

    web = SimpleNamespace(DistillSynthesis=OfflineSynthesis, content_service=content)
    runtime = ProductRuntime(web, results, ledger)
    content.generator = runtime.generate_topic
    topic = await content.create_topic({"name": "额度回归"})
    member = await content.add_member(topic["id"], {"text": "明确选择的资料"})
    await ledger.set_limit("topic", topic["id"], 0)
    with model_request_context(batch_kind="topic", batch_id=topic["id"]):
        with pytest.raises(BatchRequestLimitReached):
            await ledger.reserve(execution_id="blocked", stage="topic", provider="offline", model="offline")
    assert (await ledger.stats("topic", topic["id"]))["paused"] is True
    # 新生成确认留空：不应继承之前的零额度或暂停位。
    first = await content.generate(topic["id"], {"member_ids": [member["id"]], "request_limit": None})
    assert first["status"] == "completed"
    stats = await ledger.stats("topic", topic["id"])
    assert stats["request_limit"] is None and stats["request_count"] == 1 and stats["paused"] is False
    # 累计上限1已用完，此版本暂停；提高到3后恢复应保留3且复用原版本。
    second = await content.generate(topic["id"], {"member_ids": [member["id"]], "request_limit": 1})
    assert second["status"] == "paused"
    await ledger.set_limit("topic", topic["id"], 3)
    resumed = await runtime.resume("topic", topic["id"])
    assert resumed["version_id"] == second["id"] and resumed["status"] == "completed"
    stats = await ledger.stats("topic", topic["id"])
    assert stats["request_limit"] == 3 and stats["request_count"] == 2
    assert len((await content.get_topic(topic["id"]))["versions"]) == 2


@pytest.mark.asyncio
async def test_eta_needs_ten_complete_logical_samples_and_separates_paused_queue(results_workspace):
    results, db, _, _ = results_workspace
    await results.register_batch("summary", {"batch_id": "eta", "author": "作者", "status": "running"}, ["t1", "t2"])
    await db.execute("UPDATE video_summary_jobs SET status='queued' WHERE task_id IN ('t1','t2')")
    await db.commit()
    await results.track(kind="summary", batch_id="eta", task_id="t1", aweme_id="1", stage="summary", job_id="s1", execution_id="eta-active", mode="new")
    await results.observe_execution("s1", "summary", "queued")
    assert not await results._rows("SELECT * FROM analysis_execution_timings WHERE execution_id='eta-active'")
    await results.observe_execution("s1", "summary", "preparing_video")
    rows = await results._rows("SELECT * FROM analysis_execution_timings WHERE execution_id='eta-active'")
    assert len(rows) == 1 and rows[0]["active_started_at"]
    await results.finish_execution("s1", "summary", "paused")
    assert (await results._rows("SELECT * FROM analysis_execution_timings WHERE execution_id='eta-active'"))[0]["active_started_at"] is None
    for index in range(9):
        await results.conn.execute("INSERT INTO analysis_execution_timings(execution_id,stage,active_seconds,outcome,completed_at) VALUES (?,'summary',?,'completed','2026-01-01')", (f"sample-{index}", 20+index))
    await results.conn.commit()
    assert (await results.report("summary", "eta"))["eta"]["analysis"]["summary"]["remaining_seconds_range"] is None
    await results.conn.execute("INSERT INTO analysis_execution_timings(execution_id,stage,active_seconds,outcome,completed_at) VALUES ('sample-ten','summary',30,'completed','2026-01-01')")
    await results.conn.commit()
    # paused关联先恢复pending，随后才提供剩余处理区间。
    await results.finish_execution("s1", "summary", "pending")
    report = await results.report("summary", "eta")
    estimate = report["eta"]["analysis"]["summary"]
    assert estimate["sample_count"] == 10 and estimate["remaining_seconds_range"] == [20.0, 28.0]
    assert estimate["ready_pending"] == 2
    assert report["eta"]["download"] is None
