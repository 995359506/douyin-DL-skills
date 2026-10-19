"""下载重试与登录刷新回归，不连接抖音。"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skills.douyin_downloader.core import DouyinDownloadSkill
from skills.douyin_downloader import web
from skills.douyin_downloader.task_manager_v3 import TaskManager


def test_handler_uses_new_cookie_after_login_refresh(monkeypatch):
    created = []

    def handler(kwargs):
        result = SimpleNamespace(kwargs=dict(kwargs))
        created.append(result)
        return result

    monkeypatch.setitem(sys.modules, "f2.apps.douyin.handler", SimpleNamespace(DouyinHandler=handler))
    skill = DouyinDownloadSkill.__new__(DouyinDownloadSkill)
    skill._handler = None
    first = skill._get_handler({"cookie": "old-test-session"})
    assert skill._get_handler({"cookie": "old-test-session"}) is first
    refreshed = skill._get_handler({"cookie": "new-test-session"})
    assert refreshed is not first
    assert refreshed.kwargs["cookie"] == "new-test-session"
    assert len(created) == 2


@pytest.mark.asyncio
async def test_detail_failure_retries_without_claiming_40_percent_downloaded(monkeypatch):
    download = AsyncMock(return_value={"success": False, "status_code": 403, "message": "HTTP 403"})
    progress = AsyncMock()
    failed = AsyncMock()
    monkeypatch.setattr(web, "skill", SimpleNamespace(download_single_video=download))
    monkeypatch.setattr(web, "task_manager", SimpleNamespace(update_task_status=AsyncMock()))
    monkeypatch.setattr(web, "_update_task_progress", progress)
    monkeypatch.setattr(web, "_mark_task_failed", failed)
    monkeypatch.setattr(web, "DOWNLOAD_AUTO_RETRY_MAX", 1)
    monkeypatch.setattr(web, "DOWNLOAD_AUTO_RETRY_DELAY", 0)

    await web._download_video_task("test-task", "123456789", "/tmp/test-download")

    assert download.await_count == 2
    progress.assert_awaited_once()
    assert progress.await_args.args[1] == 0
    assert "HTTP 403" in progress.await_args.args[2]
    assert "自动重试" in progress.await_args.args[2]
    assert "疑似风控" not in progress.await_args.args[2]
    failed.assert_awaited_once()
    assert "自动重试 1 次" in failed.await_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_retry_clears_previous_attempt_progress(tmp_path):
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    await manager.db.connect()
    try:
        task_id = await manager.db.create_task("single")
        await manager.db.update_task_progress(task_id, percentage=40, current_video_name="上次等待重试")
        await manager.db.update_task_status(task_id, "failed")
        await manager.retry_task(task_id)
        stored = await manager.db.get_task(task_id)
        assert stored.status == "pending"
        assert stored.progress_percentage == 0
        assert stored.current_video_name == "等待重试失败项"
    finally:
        await manager.db.close()


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_queue_rejections_do_not_repeat_requests(monkeypatch, status):
    operation = AsyncMock(return_value={"success": False, "status_code": status, "message": f"HTTP {status}"})
    progress = AsyncMock()
    monkeypatch.setattr(web, "_update_task_progress", progress)
    result = await web._download_with_auto_retry(
        operation, task_id="task", progress_percentage=0, label="批次下载", pause_on_rejection=True,
    )
    assert not result["success"]
    operation.assert_awaited_once()
    progress.assert_not_awaited()
    assert "自动重试" not in result["message"]


async def test_queue_transient_errors_use_bounded_exponential_backoff(monkeypatch):
    operation = AsyncMock(return_value={"success": False, "status_code": 503, "message": "HTTP 503"})
    sleep = AsyncMock()
    monkeypatch.setattr(web, "_update_task_progress", AsyncMock())
    monkeypatch.setattr(web, "DOWNLOAD_AUTO_RETRY_MAX", 3)
    monkeypatch.setattr(web, "DOWNLOAD_AUTO_RETRY_DELAY", 10)
    monkeypatch.setattr(web.asyncio, "sleep", sleep)
    result = await web._download_with_auto_retry(
        operation, task_id="task", progress_percentage=0, label="批次下载", pause_on_rejection=True,
    )
    assert operation.await_count == 4
    assert [call.args[0] for call in sleep.await_args_list] == [10, 20, 40]
    assert "自动重试 3 次" in result["message"]
