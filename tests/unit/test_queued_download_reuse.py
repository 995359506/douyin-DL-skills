"""本地视频复用回归：使用临时数据库和文件，禁止下载或模型请求。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skills.douyin_downloader import web
from skills.douyin_downloader.db import TaskVideoInfo
from skills.douyin_downloader.download_queue import PersistentDownloadQueue
from skills.douyin_downloader.task_manager_v3 import TaskManager


@pytest.fixture
async def local_env(tmp_path, monkeypatch):
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    await manager.db.connect()
    root = tmp_path / "videos"
    root.mkdir()
    monkeypatch.setattr(web, "task_manager", manager)
    monkeypatch.setattr(web, "skill", SimpleNamespace(douyin_conf={"path": str(root)}))
    monkeypatch.setattr(web, "running_download_tasks", {})
    monkeypatch.setattr(web, "_schedule_download", lambda *_: pytest.fail("复用本地文件不能请求抖音"))
    monkeypatch.setattr(web, "_check_download_queue_config", lambda _: pytest.fail("复用本地文件不检查 Cookie"))
    summary = AsyncMock()
    monkeypatch.setattr(web, "_start_auto_video_summary", summary)
    try:
        yield manager, root, summary
    finally:
        await manager.db.close()


async def single(manager, root, aweme_id="10001", status="completed", filename=None, content=b"local-video"):
    task = await manager.create_single_task(aweme_id, {"desc": "测试视频", "nickname": "测试作者"}, str(root))
    path = root / (filename or f"{aweme_id}_video.mp4")
    if content is not None:
        path.write_bytes(content)
    await manager.db.update_video_status(task.id, aweme_id, "downloaded", file_path=str(path), file_size=len(content) if content else None)
    await manager.db.update_task_status(task.id, status, "上次下载失败" if status == "failed" else None)
    return task.id, path


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "pending"])
async def test_existing_file_skips_and_repairs_record(local_env, status):
    manager, root, summary = local_env
    task_id, _ = await single(manager, root, status=status)

    assert await web._prepare_queued_download("10001", "测试作者", "") == task_id
    result = await web._execute_queued_download(task_id, "10001", True)

    assert result == {"status": "completed", "task_id": task_id, "skipped": True}
    stored = await manager.db.get_task(task_id)
    assert stored.status == "completed" and stored.error_message is None
    assert (stored.total_count, stored.downloaded_count, stored.failed_count, stored.progress_percentage) == (1, 1, 0, 100)
    summary.assert_awaited_once_with(task_id)
    assert await manager.db.count_tasks() == 1


@pytest.mark.parametrize("parent_type", ["post", "mix", "mix_user"])
async def test_old_batch_reuses_hidden_reference_without_visible_duplicate(local_env, parent_type):
    manager, root, summary = local_env
    parent_id = await manager.db.create_task(parent_type, mix_name="测试作者", base_path=str(root))
    path = root / "10001_video.mp4"
    path.write_bytes(b"local-video")
    await manager.db.add_video(TaskVideoInfo(
        id=0, task_id=parent_id, aweme_id="10001", desc="测试视频", nickname="测试作者",
        sec_user_id=None, create_time="", status="failed", file_path=str(path),
        file_size=path.stat().st_size, error_message="旧批量失败", download_time=None,
    ))
    await manager.db.update_task_status(parent_id, "failed")

    task_id = await web._prepare_queued_download("10001", "测试作者", "")
    assert (await manager.get_task(task_id)).type == "batch_item"
    assert await web._prepare_queued_download("10001", "测试作者", "") == task_id
    assert (await web._execute_queued_download(task_id, "10001", True))["skipped"] is True
    assert await manager.db.count_tasks() == 1
    assert (await manager.db.fetch_one("SELECT COUNT(*) FROM tasks"))[0] == 2
    assert (await manager.db.get_task(parent_id)).status == "failed"
    summary.assert_awaited_once_with(task_id)


async def test_prefers_existing_summary_reference_and_preserves_result(local_env, monkeypatch):
    manager, root, summary = local_env
    task_id, _ = await single(manager, root)
    await manager.db.create_video_summary_job("existing-summary", task_id, "test-model", 11)
    await manager.db.update_video_summary_job("existing-summary", status="completed", summary_json=json.dumps({"title": "已有总结"}))
    other_id, _ = await single(manager, root, status="completed")
    selected = await web._prepare_queued_download("10001", "测试作者", "")
    assert selected == task_id and selected != other_id

    # 直接走真实总结入口，其配置检查被禁止，已有结果必须提前返回。
    monkeypatch.setattr(web.video_summary_service, "validate_config", lambda: pytest.fail("已有总结不能调用模型"))
    response = await web.create_video_summary(selected, web.SummaryCreateRequest())
    assert response.data["job_id"] == "existing-summary"
    assert response.data["summary"]["title"] == "已有总结"
    assert (await manager.db.fetch_one("SELECT COUNT(*) FROM video_summary_jobs"))[0] == 1


async def test_checks_older_candidates_when_latest_file_missing(local_env):
    manager, root, _ = local_env
    older, _ = await single(manager, root, filename="old_title.mp4")
    newer, _ = await single(manager, root, filename="missing_title.mp4", content=None)
    assert newer != older
    assert await web._prepare_queued_download("10001", "测试作者", "") == older


@pytest.mark.parametrize("content", [None, b""])
async def test_missing_or_empty_file_is_not_skipped(local_env, monkeypatch, content):
    manager, root, summary = local_env
    task_id, _ = await single(manager, root, content=content)
    monkeypatch.setattr(web, "_check_download_queue_config", lambda _: None)

    async def download(*args, **kwargs):
        await manager.db.update_task_status(task_id, "completed")

    requested = AsyncMock(side_effect=download)
    monkeypatch.setattr(web, "_download_video_task", requested)
    monkeypatch.setattr(web, "_schedule_download", lambda key, coroutine: web.running_download_tasks.update({key: asyncio.create_task(coroutine)}))
    result = await web._execute_queued_download(task_id, "10001", False)
    assert not result.get("skipped")
    requested.assert_awaited_once()
    summary.assert_not_awaited()


async def test_orphan_file_is_recovered_without_network(local_env):
    manager, root, summary = local_env
    (root / "10001_legacy_video.mp4").write_bytes(b"local-video")
    task_id = await web._prepare_queued_download("10001", "测试作者", "")
    assert (await web._execute_queued_download(task_id, "10001", False))["skipped"] is True
    summary.assert_not_awaited()


async def test_ambiguous_file_fails_safely_without_download(local_env):
    manager, root, summary = local_env
    task_id, _ = await single(manager, root, content=None)
    for folder in [root / "a", root / "b"]:
        folder.mkdir()
        (folder / "10001_video.mp4").write_bytes(b"local-video")
    result = await web._execute_queued_download(task_id, "10001", True)
    assert result["status"] == "failed" and result["error"]["code"] == "VIDEO_FILE_AMBIGUOUS"
    assert not result.get("skipped")
    summary.assert_not_awaited()


async def test_queue_integration_skips_cached_items_without_spacing(local_env):
    manager, root, summary = local_env
    for aweme_id in ["10001", "10002"]:
        await single(manager, root, aweme_id)
    queue = PersistentDownloadQueue(manager.db.db_path, web._prepare_queued_download, web._execute_queued_download, now=lambda: 1000)
    await queue.initialize()
    try:
        batch = await queue.create(["10001", "10002"], auto_summary=False)
        assert await queue.process_next()
        assert await queue.process_next()
        view = await queue.get(batch["batch_id"])
        assert (view["status"], view["total"], view["completed"], view["skipped"], view["pending"]) == ("completed", 2, 2, 2, 0)
        assert view["next_in_seconds"] == 0
        summary.assert_not_awaited()
    finally:
        await queue.shutdown()


async def test_active_batch_does_not_create_duplicate_download(local_env):
    manager, root, _ = local_env
    parent_id = await manager.db.create_task("post", base_path=str(root))
    await manager.db.add_video(TaskVideoInfo(
        id=0, task_id=parent_id, aweme_id="10001", desc="测试视频", nickname="测试作者",
        sec_user_id=None, create_time="", status="pending", file_path=None,
        file_size=None, error_message=None, download_time=None,
    ))
    await manager.db.update_task_status(parent_id, "downloading")
    with pytest.raises(web.HTTPException) as error:
        await web._prepare_queued_download("10001", "测试作者", "")
    assert error.value.detail["code"] == "DOWNLOAD_QUEUE_PARENT_BUSY"
    assert (await manager.db.fetch_one("SELECT COUNT(*) FROM tasks"))[0] == 1


async def test_active_single_is_awaited_before_reusing_finished_file(local_env, monkeypatch):
    manager, root, summary = local_env
    task_id, _ = await single(manager, root, status="pending")
    release = asyncio.Event()

    async def existing_download():
        await release.wait()
        await manager.db.update_task_status(task_id, "completed")

    handle = asyncio.create_task(existing_download())
    web.running_download_tasks[task_id] = handle
    monkeypatch.setattr(web, "_check_download_queue_config", lambda _: None)
    execution = asyncio.create_task(web._execute_queued_download(task_id, "10001", False))
    try:
        await asyncio.sleep(0)
        assert not execution.done()
        assert (await manager.get_task(task_id)).status == "pending"
        release.set()
        result = await execution
        assert result["status"] == "completed" and not result.get("skipped")
        summary.assert_not_awaited()
    finally:
        release.set()
        await handle
        if not execution.done():
            await execution


async def test_default_feature_guard_still_requires_completed_task(local_env):
    manager, root, _ = local_env
    task_id, _ = await single(manager, root, status="failed")
    with pytest.raises(web.HTTPException) as error:
        await web._resolve_task_video_path(await manager.get_task(task_id))
    assert error.value.detail["code"] == "VIDEO_FEATURE_TASK_NOT_READY"


async def test_preflight_without_file_does_not_create_record_or_submit_requests(local_env):
    manager, root, summary = local_env
    assert await web._preflight_queued_download("10001", "测试作者", "", True) is None
    assert (await manager.db.fetch_one("SELECT COUNT(*) FROM tasks"))[0] == 0
    summary.assert_not_awaited()


async def test_preflight_recovers_orphan_file_and_schedules_independent_summary(local_env):
    manager, root, summary = local_env
    (root / "10001_video.mp4").write_bytes(b"local-video")
    task_id = await web._preflight_queued_download("10001", "测试作者", "", True)
    assert task_id and (await manager.get_task(task_id)).status == "completed"
    summary.assert_awaited_once_with(task_id)
    assert (await manager.db.fetch_one("SELECT COUNT(*) FROM tasks"))[0] == 1


async def test_preflight_during_cooldown_skips_cached_video_behind_missing_one(local_env):
    manager, root, summary = local_env
    await single(manager, root, "10002")
    queue = PersistentDownloadQueue(
        manager.db.db_path, web._prepare_queued_download, web._execute_queued_download,
        reuse_item=web._preflight_queued_download, now=lambda: 1000,
    )
    await queue.initialize()
    try:
        batch = await queue.create(["10001", "10002"], auto_summary=False)
        await queue._db.execute("UPDATE download_queue_state SET not_before=1090 WHERE singleton=1")
        await queue._db.commit()
        assert await queue.process_next()
        view = await queue.get(batch["batch_id"])
        assert (view["skipped"], view["completed"], view["pending"], view["next_allowed_at"]) == (1, 1, 1, 1090)
        assert not await queue.process_next()
        # 没有为待下载的 10001 创建任务，也没有调用下载/模型。
        assert (await manager.db.fetch_one("SELECT COUNT(*) FROM tasks"))[0] == 1
        summary.assert_not_awaited()
    finally:
        await queue.shutdown()


async def test_queue_403_pauses_after_first_request(local_env, monkeypatch):
    manager, root, summary = local_env
    task_id, _ = await single(manager, root, content=None, status="pending")
    download = AsyncMock(return_value={"success": False, "status_code": 403, "message": "HTTP 403"})
    monkeypatch.setattr(web.skill, "download_single_video", download, raising=False)
    monkeypatch.setattr(web, "_check_download_queue_config", lambda _: None)
    monkeypatch.setattr(web, "_schedule_download", lambda key, coroutine: web.running_download_tasks.update({key: asyncio.create_task(coroutine)}))
    queue = PersistentDownloadQueue(
        manager.db.db_path, web._prepare_queued_download, web._execute_queued_download,
        reuse_item=web._preflight_queued_download, now=lambda: 1000,
    )
    await queue.initialize()
    try:
        batch = await queue.create(["10001", "10002"], auto_summary=False)
        assert await queue.process_next()
        view = await queue.get(batch["batch_id"])
        assert view["status"] == "paused" and view["failed"] == 1 and view["pending"] == 1
        assert view["error"]["code"] == "DOWNLOAD_QUEUE_BLOCKED"
        assert "自动重试" not in (await manager.get_task(task_id)).error["reason"]
        download.assert_awaited_once()
        assert not await queue.process_next()
        summary.assert_not_awaited()
    finally:
        await queue.shutdown()
