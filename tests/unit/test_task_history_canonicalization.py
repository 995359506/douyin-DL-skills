import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from skills.douyin_downloader import web
from skills.douyin_downloader.db import TaskDB, TaskVideoInfo
from skills.douyin_downloader.task_manager_v3 import TaskManager


async def _add_single_attempt(db: TaskDB, task_id: str, aweme_id: str, status: str, created_at: str, error=None):
    await db.create_task("single", task_id=task_id)
    await db.add_video(
        TaskVideoInfo(
            id=0,
            task_id=task_id,
            aweme_id=aweme_id,
            desc=f"视频 {aweme_id}",
            nickname="作者",
            sec_user_id=None,
            create_time=created_at,
            status="downloaded" if status == "completed" else status,
            file_path=f"{aweme_id}.mp4" if status == "completed" else None,
            file_size=10 if status == "completed" else None,
            error_message=None,
            download_time=created_at if status == "completed" else None,
        )
    )
    await db.update_task_status(task_id, status, json.dumps(error, ensure_ascii=False) if error else None)
    await db.execute("UPDATE tasks SET created_at=? WHERE task_id=?", (created_at, task_id))
    await db.commit()


@pytest.mark.asyncio
async def test_task_list_canonicalizes_before_pagination_and_preserves_failed_history(tmp_path, monkeypatch):
    db = TaskDB(str(tmp_path / "tasks.db"))
    await db.connect()
    try:
        # The later failed attempt must not replace an earlier successful record.
        await _add_single_attempt(db, "same-success", "100001", "completed", "2026-01-01T00:00:00")
        await _add_single_attempt(
            db,
            "same-failed",
            "100001",
            "failed",
            "2026-01-02T00:00:00",
            {"stage": "download", "code": "NETWORK_ERROR", "reason": "网络中断", "action": "稍后重试"},
        )
        await _add_single_attempt(db, "other-failed", "100002", "failed", "2026-01-03T00:00:00")

        manager = TaskManager.__new__(TaskManager)
        manager.db = db
        monkeypatch.setattr(web, "task_manager", manager)

        first_page = await manager.get_tasks(limit=1, offset=0)
        second_page = await manager.get_tasks(limit=1, offset=1)
        assert first_page["total"] == second_page["total"] == 2
        assert len(first_page["tasks"]) == len(second_page["tasks"]) == 1

        canonical = next(task for task in second_page["tasks"] if task.id == "same-success")
        assert canonical.status == "completed"
        assert canonical.history_count == 1
        assert canonical.history_failed_count == 1
        assert canonical.history_attempts == [
            {
                "id": "same-failed",
                "status": "failed",
                "created_at": "2026-01-02T00:00:00",
                "completed_at": canonical.history_attempts[0]["completed_at"],
                "retry_count": 0,
                "error": {
                    "stage": "download",
                    "code": "NETWORK_ERROR",
                    "reason": "网络中断",
                    "action": "稍后重试",
                },
            }
        ]

        # Status filters run after canonical selection: the old failed attempt is
        # represented in history, not as a separate failed list row.
        failed = await manager.get_tasks(status="failed", limit=10, offset=0)
        assert failed["total"] == 1
        assert [task.id for task in failed["tasks"]] == ["other-failed"]

        response = await web.get_tasks(
            status=None,
            type=None,
            limit=10,
            offset=0,
            keyword=None,
            sort_by="created_at",
            sort_order="desc",
        )
        response_rows = response.data["tasks"]
        successful_row = next(row for row in response_rows if row["id"] == "same-success")
        assert successful_row["history_count"] == 1
        assert successful_row["history_failed_count"] == 1
        assert successful_row["history_attempts"][0]["status"] == "failed"
        assert successful_row["history_attempts"][0]["error"]["reason"] == "网络中断"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_canonical_task_total_and_paging_keep_non_single_tasks_independent(tmp_path):
    db = TaskDB(str(tmp_path / "tasks.db"))
    await db.connect()
    try:
        await _add_single_attempt(db, "single-1", "100001", "completed", "2026-01-01T00:00:00")
        await _add_single_attempt(db, "single-2", "100001", "failed", "2026-01-02T00:00:00")
        await db.create_task("mix", mix_id="100001", task_id="mix-1")
        await db.create_task("mix", mix_id="100001", task_id="mix-2")
        await db.create_task("single", task_id="legacy-empty-single")

        # Invalid legacy rows are ignored consistently by the page and total.
        assert await db.count_tasks() == 3
        page_one = await db.get_tasks(limit=2, offset=0)
        page_two = await db.get_tasks(limit=2, offset=2)
        ids = [task.task_id for task in page_one + page_two]
        assert len(ids) == 3
        assert len(set(ids)) == 3
        assert {"mix-1", "mix-2"}.issubset(ids)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_author_facets_and_filter_span_all_pages(tmp_path):
    db = TaskDB(str(tmp_path / "tasks.db"))
    await db.connect()
    try:
        await _add_single_attempt(db, "a-1", "101", "completed", "2026-01-01T00:00:00")
        await _add_single_attempt(db, "a-2", "102", "completed", "2026-01-02T00:00:00")
        await _add_single_attempt(db, "a-retry", "101", "failed", "2026-01-03T00:00:00")
        await db.execute("UPDATE task_videos SET nickname='另一作者' WHERE task_id='a-2'")
        await db.execute("UPDATE task_videos SET cover='https://example.com/cover.jpg' WHERE task_id='a-1'")
        await db.commit()
        manager = TaskManager.__new__(TaskManager)
        manager.db = db
        page = await manager.get_tasks(limit=1)
        assert page["authors"] == [{"name": "作者", "count": 1}, {"name": "另一作者", "count": 1}]
        filtered = await manager.get_tasks(author="作者", limit=1)
        assert filtered["total"] == 1
        assert filtered["tasks"][0].video_info["cover"] == "https://example.com/cover.jpg"
        assert [task.task_id for task in await db.get_tasks(author="另一作者")] == ["a-2"]
        assert await db.count_tasks(author="不存在") == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_author_summary_batch_only_queues_completed_single_videos(tmp_path, monkeypatch):
    db = TaskDB(str(tmp_path / "tasks.db"))
    await db.connect()
    try:
        await _add_single_attempt(db, "ready", "101", "completed", "2026-01-01T00:00:00")
        await _add_single_attempt(db, "prior-done", "102", "completed", "2026-01-02T00:00:00")
        await _add_single_attempt(db, "in-progress", "103", "completed", "2026-01-03T00:00:00")
        await _add_single_attempt(db, "prior-failure", "104", "completed", "2026-01-04T00:00:00")
        await _add_single_attempt(db, "other", "105", "completed", "2026-01-05T00:00:00")
        await db.execute("UPDATE task_videos SET nickname='另一作者' WHERE task_id='other'")
        for task_id, status, summary in (("prior-done", "completed", '{"title":"已有"}'),
                                         ("in-progress", "running", None),
                                         ("prior-failure", "failed", None)):
            job_id = f"job-{task_id}"
            await db.create_video_summary_job(job_id, task_id, "test-model", 100)
            await db.update_video_summary_job(job_id, status=status, summary_json=summary)
        await db.commit()
        manager = TaskManager.__new__(TaskManager)
        manager.db = db
        monkeypatch.setattr(web, "task_manager", manager)
        monkeypatch.setattr(web, "skill", object())
        web.summary_batches.clear()
        calls = []

        async def fake_create(task_id, request):
            calls.append(task_id)
            return web.ApiResponse(success=True, data={"status": "completed"}, message="已复用")

        monkeypatch.setattr(web, "create_video_summary", fake_create)
        preview = await web.preview_author_summary_batch(web.AuthorSummaryRequest(author="作者"))
        assert preview.data == {"author": "作者", "total": 4, "to_summarize": 2,
                                "already_summarized": 1, "already_running": 1, "previous_failures": 1}
        response = await web.create_author_summary_batch(web.AuthorSummaryRequest(author="作者"))
        batch_id = response.data["batch_id"]
        await web.running_summary_batches[batch_id]
        assert set(calls) == {"ready", "prior-failure"}
        assert response.data["total"] == 2
        assert web.summary_batches[batch_id]["reused"] == 2
        assert web.summary_batches[batch_id]["skipped_summarized"] == 1
        assert web.summary_batches[batch_id]["skipped_running"] == 1
        assert web.summary_batches[batch_id]["processed"] == 2
        assert web.summary_batches[batch_id]["status"] == "completed"
    finally:
        web.summary_batches.clear()
        await db.close()


@pytest.mark.asyncio
async def test_author_transcript_batch_skips_existing_and_queues_missing_results(tmp_path, monkeypatch):
    db = TaskDB(str(tmp_path / "tasks.db"))
    await db.connect()
    try:
        await _add_single_attempt(db, "ready", "201", "completed", "2026-01-01T00:00:00")
        await _add_single_attempt(db, "prior-done", "202", "completed", "2026-01-02T00:00:00")
        await _add_single_attempt(db, "in-progress", "203", "completed", "2026-01-03T00:00:00")
        await _add_single_attempt(db, "prior-failure", "204", "completed", "2026-01-04T00:00:00")
        await _add_single_attempt(db, "other", "205", "completed", "2026-01-05T00:00:00")
        await db.execute("UPDATE task_videos SET nickname='另一作者' WHERE task_id='other'")
        for task_id, status, original in (("prior-done", "completed", "已有原文"),
                                          ("in-progress", "running", ""),
                                          ("prior-failure", "failed", "")):
            job_id = f"transcript-{task_id}"
            await db.create_transcript_job(job_id, task_id)
            await db.update_transcript_job(
                job_id,
                status=status,
                original_transcript=original,
                polished_text="已有整理稿" if original else "",
            )
        manager = TaskManager.__new__(TaskManager)
        manager.db = db
        monkeypatch.setattr(web, "task_manager", manager)
        monkeypatch.setattr(web, "skill", object())
        web.transcript_batches.clear()
        calls = []

        async def fake_create(task_id, request):
            calls.append((task_id, request.force))
            existing = await db.get_transcript_job(task_id=task_id)
            job_id = existing["job_id"] if existing else f"transcript-{task_id}"
            if not existing:
                await db.create_transcript_job(job_id, task_id)
            await db.update_transcript_job(
                job_id,
                status="completed",
                original_transcript=f"{task_id} 原始口播",
                polished_text=f"{task_id} 整理口播",
            )
            return web.ApiResponse(success=True, data={"job_id": job_id, "status": "queued"}, message="已排队")

        monkeypatch.setattr(web, "create_transcript", fake_create)
        preview = await web.preview_author_transcript_batch(web.AuthorSummaryRequest(author="作者"))
        assert preview.data == {
            "author": "作者",
            "total": 4,
            "to_transcribe": 2,
            "already_transcribed": 1,
            "already_running": 1,
            "previous_failures": 1,
        }
        response = await web.create_author_transcript_batch(web.AuthorSummaryRequest(author="作者"))
        batch_id = response.data["batch_id"]
        await web.running_transcript_batches[batch_id]
        batch = web.transcript_batches[batch_id]
        assert {task_id for task_id, _force in calls} == {"ready", "prior-failure"}
        assert response.data["total"] == 2
        assert batch["completed"] == 2
        assert batch["failed"] == 0
        assert batch["skipped_transcribed"] == 1
        assert batch["skipped_running"] == 1
        assert batch["processed"] == 2
        assert batch["status"] == "completed"
    finally:
        web.transcript_batches.clear()
        await db.close()


@pytest.mark.asyncio
async def test_task_list_summary_badges_include_distill_results(tmp_path):
    db = TaskDB(str(tmp_path / "tasks.db"))
    await db.connect()
    try:
        for index, task_id in enumerate(("done", "running", "failed", "distilled", "untouched"), 1):
            await _add_single_attempt(db, task_id, str(index), "completed", f"2026-01-0{index}T00:00:00")
        for task_id, status in (("done", "completed"), ("running", "running"),
                                ("failed", "failed"), ("distilled", "failed")):
            await db.create_video_summary_job(f"job-{task_id}", task_id, "test-model", 100)
            await db.update_video_summary_job(
                f"job-{task_id}", status=status,
                summary_json='{"title":"已有总结"}' if status == "completed" else None,
            )
        await db.create_distill_run("run-1", sec_user_id="user", nickname="作者", user_url="https://example.com")
        await db.add_distill_items("run-1", [{"ordinal": 1, "aweme_id": "4", "task_id": "distilled",
                                               "summary_json": '{"title":"蒸馏总结"}'}])
        manager = TaskManager.__new__(TaskManager)
        manager.db = db
        rows = await manager.get_tasks(limit=10)
        statuses = {task.id: task.summary_status for task in rows["tasks"]}
        assert statuses == {"done": "completed", "running": "running", "failed": "failed",
                            "distilled": "completed", "untouched": "none"}
    finally:
        await db.close()
