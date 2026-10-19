"""后台批次 API 与既有下载流程的连接，不接触真实 Cookie 或网络。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from skills.douyin_downloader import web
from skills.douyin_downloader.download_queue import PersistentDownloadQueue
from skills.douyin_downloader.download_queue_api import DownloadBatchRequest, create_download_queue_router
from skills.douyin_downloader.task_manager_v3 import TaskManager


async def test_batch_http_post_only_persists_links_and_controls_after_client_closes(tmp_path):
    prepare, execute = AsyncMock(return_value="task-one"), AsyncMock(return_value={"status": "completed"})
    clock = [1000.0]
    queue = PersistentDownloadQueue(str(tmp_path / "queue.db"), prepare, execute, now=lambda: clock[0])
    await queue.initialize()
    checks = []
    app = FastAPI()
    app.include_router(create_download_queue_router(lambda: queue, checks.append))
    payload = {"video_ids": ["10001", "10002", "10001"], "author": "测试博主", "profile_id": "MS4w-test", "client_request_id": "repeat-test", "auto_summary": True}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.post("/api/download-batches", json=payload)
            assert response.status_code == 200
            batch_id = response.json()["data"]["batch_id"]
            assert response.json()["data"]["pending"] == 2
            assert response.json()["data"]["interval_seconds"] == 30
            prepare.assert_not_awaited()
            execute.assert_not_awaited()
            duplicate = await client.post("/api/download-batches", json=payload)
            assert duplicate.json()["data"]["batch_id"] == batch_id
            conflict = await client.post("/api/download-batches", json={**payload, "video_ids": ["99999"]})
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == "DOWNLOAD_BATCH_REQUEST_CONFLICT"
            assert checks == [True, True, True]
        # HTTP 客户端已经关闭，出队仍只由后端负责。
        assert await queue.process_next()
        execute.assert_awaited_once_with("task-one", "10001", True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            batches = (await client.get("/api/download-batches")).json()["data"]["batches"]
            assert len(batches) == 1 and batches[0]["completed"] == 1
            assert batches[0]["next_in_seconds"] == 30
            assert (await client.post(f"/api/download-batches/{batch_id}/pause")).json()["data"]["status"] == "paused"
            clock[0] = 2000
            assert not await queue.process_next()
            assert (await client.post(f"/api/download-batches/{batch_id}/resume")).json()["data"]["status"] == "queued"
            stopped = (await client.post(f"/api/download-batches/{batch_id}/stop")).json()["data"]
            assert stopped["cancelled"] == 1
            assert (await client.get("/api/download-batches/missing")).status_code == 404
            assert (await client.post(f"/api/download-batches/{batch_id}/invalid")).status_code == 422
    finally:
        await queue.shutdown()


def test_batch_boundary_rejects_urls_and_attempts_to_shorten_cooldown():
    base = {"video_ids": ["10001"], "client_request_id": "request-1"}
    for invalid in (["https://www.douyin.com/video/10001"], ["１２３４５"], [10001], []):
        with pytest.raises(ValidationError):
            DownloadBatchRequest(**{**base, "video_ids": invalid})
    with pytest.raises(ValidationError):
        DownloadBatchRequest(**base, interval_seconds=0)
    assert DownloadBatchRequest(**{**base, "video_ids": ["10001", "10001"]}).video_ids == ["10001"]


async def test_queue_api_checks_login_before_accepting_work():
    queue = SimpleNamespace(create=AsyncMock())

    def reject(_auto_summary):
        raise HTTPException(401, detail={"code": "COOKIE_REQUIRED", "message": "请先扫码登录"})

    app = FastAPI()
    app.include_router(create_download_queue_router(lambda: queue, reject))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post("/api/download-batches", json={"video_ids": ["10001"], "client_request_id": "request"})
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "COOKIE_REQUIRED"
        queue.create.assert_not_awaited()


async def test_prepare_reuses_record_and_does_not_fetch_metadata(tmp_path, monkeypatch):
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    await manager.db.connect()
    monkeypatch.setattr(web, "task_manager", manager)
    monkeypatch.setattr(web, "skill", SimpleNamespace(douyin_conf={"path": str(tmp_path)}))
    try:
        task_id = await web._prepare_queued_download("10001", "测试博主", "MS4w-test")
        assert await web._prepare_queued_download("10001", "测试博主", "MS4w-test") == task_id
        task = await manager.get_task(task_id)
        assert task.status == "pending"
        assert task.video_info["nickname"] == "测试博主"
        assert task.video_info["sec_user_id"] == "MS4w-test"
        assert await manager.db.count_tasks() == 1
    finally:
        await manager.db.close()


async def test_execute_reuses_downloaded_file_and_starts_single_summary(tmp_path, monkeypatch):
    task = SimpleNamespace(id="existing", status="completed")
    manager = SimpleNamespace(get_task=AsyncMock(return_value=task), update_task_status=AsyncMock(), db=SimpleNamespace(
        update_video_status=AsyncMock(), update_task_counts=AsyncMock(), update_task_progress=AsyncMock(),
    ))
    monkeypatch.setattr(web, "task_manager", manager)
    path = tmp_path / "10001_video.mp4"
    path.write_bytes(b"local-video")
    resolve, summary = AsyncMock(return_value=path), AsyncMock()
    monkeypatch.setattr(web, "_resolve_task_video_path", resolve)
    monkeypatch.setattr(web, "_start_auto_video_summary", summary)
    monkeypatch.setattr(web, "_schedule_download", lambda *_: pytest.fail("已下载文件不能重新请求抖音"))
    result = await web._execute_queued_download("existing", "10001", True)
    assert result["status"] == "completed"
    assert result["skipped"] is True
    summary.assert_awaited_once_with("existing")


async def test_execute_schedules_download_with_auto_summary_without_waiting_for_model(monkeypatch):
    task = SimpleNamespace(id="task", status="pending", base_path="Download")
    manager = SimpleNamespace(get_task=AsyncMock(side_effect=[task, SimpleNamespace(status="completed")]))
    monkeypatch.setattr(web, "task_manager", manager)
    monkeypatch.setattr(web, "_resolve_task_video_path", AsyncMock(side_effect=HTTPException(404)))
    monkeypatch.setattr(web, "_check_download_queue_config", lambda _: None)
    monkeypatch.setattr(web, "running_download_tasks", {})
    download = AsyncMock()
    monkeypatch.setattr(web, "_download_video_task", download)

    def schedule(task_id, coroutine):
        web.running_download_tasks[task_id] = asyncio.create_task(coroutine)

    monkeypatch.setattr(web, "_schedule_download", schedule)
    result = await web._execute_queued_download("task", "10001", True)
    assert result["status"] == "completed"
    download.assert_awaited_once_with("task", "10001", "Download", auto_summary=True, pause_on_rejection=True)


async def test_execute_login_failure_is_safe_and_pauses_with_scan_guidance(monkeypatch):
    task = SimpleNamespace(id="task", status="pending")
    monkeypatch.setattr(web, "task_manager", SimpleNamespace(get_task=AsyncMock(return_value=task)))
    monkeypatch.setattr(web, "_resolve_task_video_path", AsyncMock(side_effect=HTTPException(404)))
    secret = "sk-test-public-placeholder-only"

    def reject(_):
        raise HTTPException(401, detail=secret)

    monkeypatch.setattr(web, "_check_download_queue_config", reject)
    failed = AsyncMock()
    monkeypatch.setattr(web, "_mark_task_failed", failed)
    result = await web._execute_queued_download("task", "10001", True)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "COOKIE_REQUIRED"
    assert "扫码登录" in result["error"]["action"]
    assert secret not in str(result) + str(failed.await_args)


async def test_execute_missing_task_requests_local_reference_repair(monkeypatch):
    monkeypatch.setattr(web, "task_manager", SimpleNamespace(get_task=AsyncMock(return_value=None)))
    monkeypatch.setattr(web, "_schedule_download", lambda *_: pytest.fail("缺失记录不能直接发出下载请求"))
    assert await web._execute_queued_download("removed", "10001", True) == {"status": "missing", "task_id": "removed"}
