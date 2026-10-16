"""Product-safety regression tests.

Every test is isolated with mocks or ``tmp_path``. Nothing here reads a local
Cookie, opens a browser, contacts Douyin, or starts a real download.
"""

import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, WebSocketDisconnect
from pydantic import ValidationError

from skills.douyin_downloader import web
from skills.douyin_downloader.db import TaskDB, TaskInfo, TaskVideoInfo
from skills.douyin_downloader.core import DouyinDownloadSkill, _safe_directory_component
from skills.douyin_downloader.task_manager_v3 import DownloadTask, TaskManager, TaskType


@pytest.mark.asyncio
async def test_single_download_auto_summary_only_after_success(monkeypatch):
    statuses = []
    summaries = []

    class FakeManager:
        db = SimpleNamespace(update_task_counts=AsyncMock())

        async def update_task_status(self, _task_id, status):
            statuses.append(status)

    async def result_success(*_args, **_kwargs):
        return {"success": True}

    async def start_summary(task_id):
        summaries.append(task_id)

    monkeypatch.setattr(web, "skill", SimpleNamespace(download_single_video=AsyncMock()))
    monkeypatch.setattr(web, "task_manager", FakeManager())
    monkeypatch.setattr(web, "_download_with_auto_retry", result_success)
    monkeypatch.setattr(web, "_start_auto_video_summary", start_summary)
    await web._download_video_task("success", "123", "/tmp/mock", auto_summary=True)
    assert statuses == ["downloading", "completed"]
    assert summaries == ["success"]

    async def result_failed(*_args, **_kwargs):
        return {"success": False, "message": "模拟下载失败"}

    monkeypatch.setattr(web, "_download_with_auto_retry", result_failed)
    monkeypatch.setattr(web, "_mark_task_failed", AsyncMock())
    await web._download_video_task("failure", "456", "/tmp/mock", auto_summary=True)
    assert summaries == ["success"]


@pytest.mark.asyncio
async def test_auto_summary_start_failure_keeps_download_completed(monkeypatch):
    statuses = []
    errors = []

    class FakeDB:
        update_task_counts = AsyncMock()
        get_video_summary_job = AsyncMock(return_value=None)
        create_video_summary_job = AsyncMock()

        async def update_video_summary_job(self, _job_id, **fields):
            errors.append(json.loads(fields["error_json"]))

    class FakeManager:
        db = FakeDB()

        async def update_task_status(self, _task_id, status):
            statuses.append(status)

    async def success(*_args, **_kwargs):
        return {"success": True}

    async def summary_failure(*_args, **_kwargs):
        raise RuntimeError("模拟模型配置故障")

    monkeypatch.setattr(web, "skill", SimpleNamespace(download_single_video=AsyncMock()))
    monkeypatch.setattr(web, "task_manager", FakeManager())
    monkeypatch.setattr(web, "_download_with_auto_retry", success)
    monkeypatch.setattr(web, "create_video_summary", summary_failure)
    await web._download_video_task("success", "123", "/tmp/mock", auto_summary=True)
    assert statuses == ["downloading", "completed"]
    assert errors[0]["code"] == "SUMMARY_AUTO_START_FAILED"


def test_single_download_auto_summary_requires_explicit_request_flag():
    assert web.DownloadRequest(url="https://www.douyin.com/video/123").auto_summary is False
    assert web.DownloadRequest(url="https://www.douyin.com/video/123", auto_summary=True).auto_summary is True


@pytest.mark.asyncio
async def test_distill_create_rejects_invalid_cookie_before_scan(monkeypatch):
    monkeypatch.setattr(web, "distill_service", object())
    monkeypatch.setattr(web, "skill", object())
    monkeypatch.setattr(web, "_cookie_state", lambda: {"exists": True, "valid": False})
    with pytest.raises(HTTPException) as error:
        await web.create_distill_run(web.DistillRunCreateRequest(confirm=True), "token", "scan")
    assert error.value.status_code == 401
    assert error.value.detail["code"] == "COOKIE_REQUIRED"


@pytest.mark.asyncio
async def test_download_scheduler_limits_global_concurrency_to_two(monkeypatch):
    """批量投递不得绕过全局下载闸门。"""
    active = 0
    peak = 0
    completed = 0
    at_capacity = asyncio.Event()
    release = asyncio.Event()

    async def controlled_download():
        nonlocal active, peak, completed
        active += 1
        peak = max(peak, active)
        if peak == 2:
            at_capacity.set()
        try:
            await release.wait()
            completed += 1
        finally:
            active -= 1

    monkeypatch.setattr(web, "running_download_tasks", {})
    monkeypatch.setattr(web, "download_slots", asyncio.Semaphore(2))

    for index in range(5):
        web._schedule_download(f"limited-{index}", controlled_download())

    tasks = list(web.running_download_tasks.values())
    await asyncio.wait_for(at_capacity.wait(), timeout=1)
    await asyncio.sleep(0)
    assert peak == 2

    release.set()
    await asyncio.gather(*tasks)

    assert completed == 5
    assert peak == 2
    assert web.running_download_tasks == {}


@pytest.mark.asyncio
async def test_download_auto_retry_waits_on_403_and_recovers(monkeypatch):
    """403 风控只重试当前作品，并把等待状态写入任务进度。"""
    calls = 0
    sleeps = []
    progress = []

    async def operation():
        nonlocal calls
        calls += 1
        if calls < 3:
            return {"success": False, "status_code": 403, "message": "下载失败: HTTP 403"}
        return {"success": True, "aweme_id": "100001"}

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def fake_progress(task_id, percentage, message):
        progress.append((task_id, percentage, message))

    monkeypatch.setattr(web, "DOWNLOAD_AUTO_RETRY_MAX", 3)
    monkeypatch.setattr(web, "DOWNLOAD_AUTO_RETRY_DELAY", 10.0)
    monkeypatch.setattr(web.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(web, "_update_task_progress", fake_progress)

    result = await web._download_with_auto_retry(
        operation,
        task_id="batch-1",
        progress_percentage=20,
        label="第 1/2 条作品",
    )

    assert result["success"] is True
    assert calls == 3
    assert sleeps == [10.0, 10.0]
    assert len(progress) == 2
    assert all("HTTP 403" in message and "自动重试" in message for _, _, message in progress)


@pytest.mark.asyncio
async def test_download_auto_retry_does_not_retry_auth_failure(monkeypatch):
    """401/普通业务失败不能被误判成可恢复的风控错误。"""
    calls = 0
    sleeps = []

    async def operation():
        nonlocal calls
        calls += 1
        return {"success": False, "status_code": 401, "message": "Cookie 无效"}

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(web.asyncio, "sleep", fake_sleep)
    result = await web._download_with_auto_retry(
        operation,
        task_id="batch-2",
        progress_percentage=0,
        label="第 1/1 条作品",
    )

    assert result["status_code"] == 401
    assert calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_download_auto_retry_exhaustion_is_explicit(monkeypatch):
    """超过上限后保留 403 状态，并提示系统已经自动重试过。"""
    calls = 0
    sleeps = []

    async def operation():
        nonlocal calls
        calls += 1
        return {"success": False, "status_code": 403, "message": "下载失败: HTTP 403"}

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(web, "DOWNLOAD_AUTO_RETRY_MAX", 2)
    monkeypatch.setattr(web, "DOWNLOAD_AUTO_RETRY_DELAY", 10.0)
    monkeypatch.setattr(web.asyncio, "sleep", fake_sleep)
    result = await web._download_with_auto_retry(
        operation,
        task_id="batch-3",
        progress_percentage=0,
        label="第 1/1 条作品",
    )

    assert result["success"] is False
    assert result["status_code"] == 403
    assert "自动重试 2 次" in result["message"]
    assert calls == 3
    assert sleeps == [10.0, 10.0]


def test_request_models_use_safe_defaults_and_validate_inputs():
    with pytest.raises(ValidationError):
        web.DownloadRequest(url="https://example.com/video/12345")
    with pytest.raises(ValidationError):
        web.DownloadRequest(url="https://douyin.com.evil.test/video/12345")
    assert web.DownloadRequest(
        url="https://m.douyin.com/video/7420000012345678901"
    ).url == "https://m.douyin.com/video/7420000012345678901"

    mix = web.DownloadMixRequest(
        mix_id="12345 12345,67890", interval="2026-01-01|2026-01-31"
    )
    assert mix.mix_id == "12345,67890"
    assert mix.max_counts == 20

    user = web.DownloadUserPostsRequest(
        user_url="https://www.douyin.com/user/mock-user"
    )
    assert user.max_counts == 50

    with pytest.raises(ValidationError):
        web.DownloadMixRequest(
            mix_id="12345", interval="2026-02-30|2026-03-01"
        )
    with pytest.raises(ValidationError):
        web.DownloadUserPostsRequest(
            user_url="https://www.douyin.com/video/12345",
            interval="2026-03-02|2026-03-01",
        )
    with pytest.raises(ValidationError):
        web.DownloadMixRequest(mix_id="12345", mix_name="../../outside")
    assert _safe_directory_component("../bad/name", "fallback") == "bad_name"


@pytest.mark.asyncio
async def test_user_post_scan_retries_from_last_cursor_without_download(monkeypatch):
    """Transient page failures retry the saved cursor without profile refetches."""

    class FakeSkill:
        douyin_conf = {"timeout": 10, "page_counts": 2}

        async def get_cookie(self):
            return "mock-cookie"

        def _build_kwargs(self, overrides):
            return {"path": "/tmp/mock-downloads", **overrides}

    class FakePage:
        def __init__(self, items, cursor, has_more):
            self._items = items
            self.max_cursor = cursor
            self.has_more = has_more

        def _to_list(self):
            return self._items

    class Upstream503(Exception):
        status_code = 503

    class FakeCrawler:
        instances = []
        calls = {}

        def __init__(self, kwargs):
            self.kwargs = kwargs
            self.__class__.instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def fetch_user_post(self, params):
            cursor = params.max_cursor
            self.calls[cursor] = self.calls.get(cursor, 0) + 1
            if cursor == 0:
                return FakePage([{"aweme_id": "100001"}, {"aweme_id": "100002"}], 100, True)
            if cursor == 100:
                return FakePage([{"aweme_id": "100003"}, {"aweme_id": "100004"}], 200, True)
            assert cursor == 200
            if self.calls[cursor] == 1:
                raise Upstream503()
            return FakePage([{"aweme_id": "100005"}, {"aweme_id": "100006"}], 300, False)

    monkeypatch.setattr(web, "skill", FakeSkill())
    import f2.apps.douyin.crawler as crawler_module
    import f2.apps.douyin.filter as filter_module

    monkeypatch.setattr(crawler_module, "DouyinCrawler", FakeCrawler)
    monkeypatch.setattr(filter_module, "UserPostFilter", lambda page: page)
    session = await web.scan_manager.create_scan(
        "https://v.douyin.com/mock", "sec-user", "Mock", total_hint=6
    )

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(web, "_sleep_user_post_scan", no_wait)
    await web._run_user_post_scan(session.scan_id)

    assert session.status == "completed"
    assert session.found_count == 6
    assert session.pages_scanned == 3
    assert [item.aweme_id for item in session.items] == [
        "100001",
        "100002",
        "100003",
        "100004",
        "100005",
        "100006",
    ]
    assert [instance.kwargs["timeout"] for instance in FakeCrawler.instances] == [10, 10, 10, 10]
    assert FakeCrawler.calls == {0: 1, 100: 1, 200: 2}


@pytest.mark.asyncio
async def test_user_post_scan_reports_403_without_retry_or_cookie_claim(monkeypatch):
    class FakeSkill:
        douyin_conf = {"timeout": 10, "page_counts": 20}

        async def get_cookie(self):
            return "mock-cookie"

        def _build_kwargs(self, overrides):
            return overrides

    class Upstream403(Exception):
        status_code = 403

    class FakeCrawler:
        calls = 0

        def __init__(self, _kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def fetch_user_post(self, _params):
            type(self).calls += 1
            raise Upstream403("sensitive upstream details must not be exposed")

    import f2.apps.douyin.crawler as crawler_module
    import f2.apps.douyin.filter as filter_module

    monkeypatch.setattr(web, "skill", FakeSkill())
    monkeypatch.setattr(crawler_module, "DouyinCrawler", FakeCrawler)
    monkeypatch.setattr(filter_module, "UserPostFilter", lambda page: page)
    monkeypatch.setattr(web, "_sleep_user_post_scan", AsyncMock())
    session = await web.scan_manager.create_scan(
        "https://v.douyin.com/mock", "sec-user", "Mock", total_hint=100
    )

    await web._run_user_post_scan(session.scan_id)

    assert session.status == "failed"
    assert session.error["code"] == "SCAN_UPSTREAM_FORBIDDEN"
    assert session.error["stage"] == "posts"
    assert session.error["stage_label"] == "读取作者作品列表"
    assert session.error["status_code"] == 403
    assert session.error["retryable"] is False
    assert "主页资料预检已通过" in session.error["action"]
    assert "风控" not in session.error["message"]
    assert "Cookie 失效" not in session.error["message"]
    assert "sensitive upstream details" not in str(session.error)
    assert FakeCrawler.calls == 1


@pytest.mark.asyncio
async def test_create_user_post_scan_passes_verified_profile_count(monkeypatch):
    class FakeSkill:
        douyin_conf = {}

    monkeypatch.setattr(web, "skill", FakeSkill())
    monkeypatch.setattr(web, "_require_valid_cookie", lambda: None)
    monkeypatch.setattr(web, "_validate_douyin_url", lambda url, **_kwargs: url)

    async def fake_metadata(_url):
        return {
            "sec_user_id": "verified-sec-user",
            "nickname": "已验证作者",
            "aweme_count": 42,
        }

    monkeypatch.setattr(web, "_fetch_user_metadata", fake_metadata)

    def close_scheduled_coroutine(_scan_id, coroutine):
        coroutine.close()

    monkeypatch.setattr(web.scan_manager, "schedule", close_scheduled_coroutine)
    response = await web.create_user_post_scan(
        web.CreateUserPostScanRequest(user_url="https://www.douyin.com/user/mock")
    )

    session = web.scan_manager.sessions[response.data["scan_id"]]
    assert response.data["profile"]["nickname"] == "已验证作者"
    assert session.total_hint == 42


def test_cookie_gate_returns_structured_action_without_reading_cookie(monkeypatch):
    monkeypatch.setattr(web, "_cookie_state", lambda: {"exists": True, "valid": False})
    with pytest.raises(HTTPException) as caught:
        web._require_valid_cookie()
    assert caught.value.status_code == 401
    assert caught.value.detail == {
        "code": "COOKIE_REQUIRED",
        "message": "登录状态无效，请先在 Cookie 管理中扫码登录",
        "action": "刷新 Cookie 后重试",
    }


@pytest.mark.asyncio
async def test_cookie_refresh_starts_background_task_without_returning_cookie(monkeypatch):
    marker = "MOCK_COOKIE_MATERIAL_MUST_NOT_LEAK"
    refresh_cookie = AsyncMock(return_value=marker)
    fake_skill = SimpleNamespace(refresh_cookie=refresh_cookie)
    monkeypatch.setattr(web, "skill", fake_skill)
    monkeypatch.setattr(web, "_cookie_refresh_task", None)
    monkeypatch.setattr(
        web,
        "_cookie_refresh_state",
        {"status": "idle", "refreshing": False, "message": "", "error": None},
    )

    response = await web.refresh_cookie(web.RefreshCookieRequest(headless=False))
    payload = response.model_dump()

    assert payload["data"]["refresh_status"] == "starting"
    assert payload["data"]["refreshing"] is True
    assert marker not in json.dumps(payload)
    await web._cookie_refresh_task
    assert web._cookie_refresh_state["status"] == "succeeded"
    refresh_cookie.assert_awaited_once_with(headless=False)


@pytest.mark.asyncio
async def test_cookie_refresh_duplicate_request_reuses_running_task(monkeypatch):
    gate = asyncio.get_running_loop().create_future()
    started = asyncio.Event()
    calls = []

    async def refresh_cookie(**_kwargs):
        calls.append(_kwargs)
        started.set()
        return await gate

    monkeypatch.setattr(web, "skill", SimpleNamespace(refresh_cookie=refresh_cookie))
    monkeypatch.setattr(web, "_cookie_refresh_task", None)
    monkeypatch.setattr(
        web,
        "_cookie_refresh_state",
        {"status": "idle", "refreshing": False, "message": "", "error": None},
    )

    first = await web.refresh_cookie(web.RefreshCookieRequest(headless=False))
    await started.wait()
    second = await web.refresh_cookie(web.RefreshCookieRequest(headless=False))

    assert first.data["refreshing"] is True
    assert second.data["refresh_status"] == "waiting"
    assert "已在进行" in second.message
    assert calls == [{"headless": False}]

    gate.set_result("MOCK_COOKIE_MATERIAL")
    await web._cookie_refresh_task
    assert web._cookie_refresh_state["status"] == "succeeded"


@pytest.mark.asyncio
async def test_cookie_status_reports_refresh_state_separately_from_existing_cookie(monkeypatch):
    monkeypatch.setattr(web, "skill", SimpleNamespace())
    monkeypatch.setattr(web, "_cookie_state", lambda: {"exists": True, "valid": True})
    monkeypatch.setattr(
        web,
        "_cookie_refresh_state",
        {
            "status": "waiting",
            "refreshing": True,
            "message": "请完成扫码",
            "error": None,
        },
    )

    response = await web.check_cookie_status()

    assert response.data["valid"] is True
    assert response.data["refresh_status"] == "waiting"
    assert response.data["refreshing"] is True


def test_cookie_refresh_request_defaults_to_visible_browser():
    assert web.RefreshCookieRequest().headless is False


@pytest.mark.asyncio
async def test_cookie_browser_launch_failure_is_reported_by_status(monkeypatch):
    fake_skill = SimpleNamespace(
        refresh_cookie=AsyncMock(
            side_effect=RuntimeError("BrowserType.launch: Executable doesn't exist")
        )
    )
    monkeypatch.setattr(web, "skill", fake_skill)
    monkeypatch.setattr(web, "_cookie_refresh_task", None)
    monkeypatch.setattr(
        web,
        "_cookie_refresh_state",
        {"status": "idle", "refreshing": False, "message": "", "error": None},
    )

    response = await web.refresh_cookie(web.RefreshCookieRequest(headless=False))
    await web._cookie_refresh_task

    assert response.data["refreshing"] is True
    assert web._cookie_refresh_state["status"] == "failed"
    assert web._cookie_refresh_state["error"] == {
        "code": "COOKIE_BROWSER_LAUNCH_FAILED",
        "message": "无法启动扫码登录窗口",
        "action": "请确认已安装 Google Chrome，或 Playwright Chromium 后重试",
    }


@pytest.mark.asyncio
async def test_cookie_status_error_does_not_echo_exception(monkeypatch):
    marker = "MOCK_SENSITIVE_EXCEPTION_MATERIAL"
    monkeypatch.setattr(web, "skill", SimpleNamespace())
    monkeypatch.setattr(web, "_cookie_state", lambda: (_ for _ in ()).throw(RuntimeError(marker)))

    with pytest.raises(HTTPException) as caught:
        await web.check_cookie_status()

    assert caught.value.detail == "Cookie 状态检查失败"
    assert marker not in str(caught.value.detail)


@pytest.mark.asyncio
async def test_user_preflight_uses_mocked_backend_metadata(monkeypatch):
    monkeypatch.setattr(web, "_require_valid_cookie", lambda: None)
    mocked_fetch = AsyncMock(
        return_value={
            "sec_user_id": "mock-sec-user",
            "nickname": "Mock User",
            "aweme_count": 12,
            "verified": True,
        }
    )
    monkeypatch.setattr(web, "_fetch_user_metadata", mocked_fetch)

    response = await web.get_user_info(
        web.UserInfoRequest(user_url="https://www.douyin.com/user/mock-sec-user")
    )

    assert response.data["verified"] is True
    assert response.data["nickname"] == "Mock User"
    mocked_fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_mix_deduplicates_and_preserves_options(monkeypatch, tmp_path):
    created = []

    class FakeTaskManager:
        async def get_tasks(self, **_kwargs):
            return {"tasks": [], "total": 0}

        async def create_mix_task(self, **kwargs):
            created.append(kwargs)
            return SimpleNamespace(
                id=f"task-{kwargs['mix_id']}",
                status="pending",
                video_name=kwargs["mix_name"],
            )

    def discard_schedule(_task_id, coroutine):
        coroutine.close()

    monkeypatch.setattr(web, "_require_valid_cookie", lambda: None)
    monkeypatch.setattr(
        web, "skill", SimpleNamespace(douyin_conf={"path": str(tmp_path)})
    )
    monkeypatch.setattr(web, "task_manager", FakeTaskManager())
    monkeypatch.setattr(web, "_schedule_download", discard_schedule)

    response = await web.download_mix(
        web.DownloadMixRequest(
            mix_id="12345,12345,67890",
            max_counts=7,
            interval="2026-01-01|2026-01-31",
        )
    )

    assert response.data["tasks_count"] == 2
    assert [item["mix_id"] for item in created] == ["12345", "67890"]
    assert all(item["max_counts"] == 7 for item in created)
    assert all(item["interval"] == "2026-01-01|2026-01-31" for item in created)


def test_resume_uses_base_path_and_preserves_saved_options(monkeypatch):
    captured = {}

    def fake_download_mix_task(*args, **kwargs):
        captured.update(kwargs)

        async def noop():
            return None

        return noop()

    def discard_schedule(task_id, coroutine):
        captured["scheduled_task_id"] = task_id
        coroutine.close()

    monkeypatch.setattr(web, "_download_mix_task", fake_download_mix_task)
    monkeypatch.setattr(web, "_schedule_download", discard_schedule)
    task = SimpleNamespace(
        id="resume-1",
        type="mix",
        mix_id="12345",
        mix_name="Mock Mix",
        video_info={"mix_id": "12345"},
        download_path="/tmp/result/12345_Mock",
        base_path="/tmp/base",
        download_options={
            "max_counts": 7,
            "interval": "2026-01-01|2026-01-31",
        },
    )

    web._schedule_task_from_record(task)

    assert captured["download_path"] == "/tmp/base"
    assert captured["max_counts"] == 7
    assert captured["interval"] == "2026-01-01|2026-01-31"
    assert captured["scheduled_task_id"] == "resume-1"


@pytest.mark.asyncio
async def test_user_posts_core_preserves_request_interval(monkeypatch, tmp_path):
    from f2.apps.douyin.utils import SecUserIdFetcher

    captured = {}

    class EmptyHandler:
        async def fetch_user_profile(self, _sec_user_id):
            return SimpleNamespace(nickname="Mock User")

        async def fetch_user_post_videos(self, **_kwargs):
            if False:
                yield None

    downloader = DouyinDownloadSkill("./my_douyin.yaml")
    monkeypatch.setattr(downloader, "get_cookie", AsyncMock(return_value="mock"))
    monkeypatch.setattr(
        downloader,
        "_build_kwargs",
        lambda _overrides: {"path": str(tmp_path), "naming": "{aweme_id}_{desc}"},
    )

    def fake_get_handler(kwargs):
        captured.update(kwargs)
        return EmptyHandler()

    monkeypatch.setattr(downloader, "_get_handler", fake_get_handler)
    monkeypatch.setattr(
        SecUserIdFetcher,
        "get_sec_user_id",
        AsyncMock(return_value="mock-sec-user"),
    )
    try:
        result = await downloader.download_user_posts(
            "https://www.douyin.com/user/mock-sec-user",
            max_counts=5,
            interval="2026-01-01|2026-01-31",
        )
    finally:
        downloader._playwright_executor.shutdown(wait=False)

    assert captured["interval"] == "2026-01-01|2026-01-31"
    assert result["success"] is True


def test_task_error_contract_includes_reason_action_and_failed_items():
    info = TaskInfo(
        task_id="failed-1",
        type="single",
        mix_id=None,
        mix_name=None,
        status="failed",
        base_path="/tmp/base",
        total_count=1,
        failed_count=1,
        error_message=web._task_error(
            "download", "MOCK_FAILURE", "Mock reason", "Mock action"
        ),
    )
    videos = [
        TaskVideoInfo(
            id=1,
            task_id="failed-1",
            aweme_id="12345",
            desc="Mock video",
            nickname="Mock user",
            sec_user_id=None,
            create_time="2026-01-01T00:00:00",
            status="failed",
            file_path=None,
            file_size=None,
            error_message=web._task_error(
                "download", "MOCK_FAILURE", "Item reason", "Retry item"
            ),
            download_time=None,
        )
    ]

    task = DownloadTask.from_db(info, videos)

    assert task.error == {
        "stage": "download",
        "code": "MOCK_FAILURE",
        "reason": "Mock reason",
        "action": "Mock action",
    }
    assert task.result["message"] == "Mock reason"
    assert task.result["failed_items"] == [
        {"aweme_id": "12345", "name": "Mock video", "reason": "Item reason"}
    ]


def test_completed_task_exposes_exact_result_path_but_keeps_retry_base_path():
    info = TaskInfo(
        task_id="path-1",
        type="single",
        mix_id=None,
        mix_name=None,
        status="completed",
        base_path="/tmp/base",
        result_path="/tmp/base/exact-output",
    )
    video = TaskVideoInfo(
        id=1,
        task_id="path-1",
        aweme_id="12345",
        desc="Mock",
        nickname="Mock",
        sec_user_id=None,
        create_time="2026-01-01T00:00:00",
        status="downloaded",
        file_path="12345_exact_video.mp4",
        file_size=4,
        error_message=None,
        download_time="2026-01-01T00:01:00",
    )

    task = DownloadTask.from_db(info, [video])

    assert task.download_path == "/tmp/base/exact-output"
    assert task.base_path == "/tmp/base"
    assert task.file_name == "12345_exact_video.mp4"


@pytest.mark.asyncio
async def test_filtered_count_matches_filtered_pagination(tmp_path):
    db = TaskDB(str(tmp_path / "tasks.db"))
    await db.connect()
    try:
        mix_id = await db.create_task(task_type="mix", mix_id="12345")
        await db.update_task_status(mix_id, "completed")
        await db.create_task(task_type="mix", mix_id="67890")
        single_id = await db.create_task(task_type="single")
        await db.update_task_status(single_id, "completed")

        tasks = await db.get_tasks(status="completed", task_type="mix")
        total = await db.count_tasks(status="completed", task_type="mix")

        assert [task.task_id for task in tasks] == [mix_id]
        assert total == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_task_list_merges_duplicate_single_aweme_ids_by_status(tmp_path):
    """任务工作台只展示同一作品的权威记录，不删除历史尝试。"""
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    await manager.db.connect()
    try:
        failed_id = await manager.db.create_task("single")
        await manager.db.add_video(TaskVideoInfo(
            id=0,
            task_id=failed_id,
            aweme_id="100001",
            desc="duplicate video",
            nickname="tester",
            sec_user_id=None,
            create_time="2026-01-01T00:00:00",
            status="failed",
            file_path=None,
            file_size=None,
            error_message="403",
            download_time=None,
        ))
        await manager.db.update_task_status(failed_id, "failed", "403")

        completed_id = await manager.db.create_task("single")
        await manager.db.add_video(TaskVideoInfo(
            id=0,
            task_id=completed_id,
            aweme_id="100001",
            desc="duplicate video",
            nickname="tester",
            sec_user_id=None,
            create_time="2026-01-01T00:00:01",
            status="downloaded",
            file_path="100001.mp4",
            file_size=10,
            error_message=None,
            download_time="2026-01-01T00:00:02",
        ))
        await manager.db.update_task_status(completed_id, "completed")

        result = await manager.get_tasks(task_type=TaskType.SINGLE, limit=20)
        assert result["total"] == 1
        assert [task.id for task in result["tasks"]] == [completed_id]
        assert result["tasks"][0].history_count == 1
        assert result["tasks"][0].history_attempts[0]["id"] == failed_id
        assert result["tasks"][0].history_attempts[0]["status"] == "failed"

        rows = await manager.db.fetch_all(
            "SELECT task_id FROM tasks WHERE task_id IN (?, ?)",
            (failed_id, completed_id),
        )
        assert {row[0] for row in rows} == {failed_id, completed_id}
    finally:
        await manager.db.close()


@pytest.mark.asyncio
async def test_compat_sync_create_task_persists_the_returned_id(tmp_path):
    """兼容同步入口不得再创建孤儿任务或写入不存在的 task_id。"""
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    await manager.db.connect()
    try:
        task = manager.create_task(
            TaskType.SINGLE,
            "兼容视频",
            "",
            "兼容视频",
            {"aweme_id": "100002"},
        )
        await asyncio.sleep(0.05)

        persisted = await manager.get_task(task.id)
        assert persisted is not None
        assert persisted.video_info["aweme_id"] == "100002"
        task_rows = await manager.db.fetch_all("SELECT task_id FROM tasks")
        assert [row[0] for row in task_rows] == [task.id]
    finally:
        await manager.db.close()


@pytest.mark.asyncio
async def test_task_filter_rejects_unknown_values():
    with pytest.raises(HTTPException) as caught:
        await web.get_tasks(status="not-a-status", type=None, limit=20, offset=0)
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "INVALID_TASK_FILTER"


@pytest.mark.asyncio
async def test_websocket_init_requests_http_sync_without_embedding_100_tasks(
    monkeypatch,
):
    sent = []

    class FakeSocket:
        async def accept(self):
            return None

        async def send_json(self, value):
            sent.append(value)

        async def receive_text(self):
            raise WebSocketDisconnect()

    fake_manager = SimpleNamespace(
        db=SimpleNamespace(count_tasks=AsyncMock(return_value=234))
    )
    monkeypatch.setattr(web, "task_manager", fake_manager)

    await web.websocket_endpoint(FakeSocket())

    assert sent == [
        {"type": "init", "data": {"sync_required": True, "total": 234}}
    ]
    assert "tasks" not in sent[0]["data"]


@pytest.mark.asyncio
async def test_file_lookup_rejects_ambiguous_prefix_instead_of_picking_first(
    monkeypatch, tmp_path
):
    (tmp_path / "12345_first.mp4").write_bytes(b"one")
    (tmp_path / "12345_second.mp4").write_bytes(b"two")
    monkeypatch.setattr(
        web, "skill", SimpleNamespace(douyin_conf={"path": str(tmp_path)})
    )

    with pytest.raises(HTTPException) as caught:
        await web.download_file("missing.mp4", aweme_id="12345")

    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_task_only_deletes_record_and_keeps_file(monkeypatch, tmp_path):
    media = tmp_path / "keep.mp4"
    media.write_bytes(b"keep")
    fake_manager = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(status="completed")),
        delete_task=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(web, "task_manager", fake_manager)

    response = await web.delete_task("task-1")

    assert response.message == "任务记录已删除，下载文件已保留"
    assert media.read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_delete_task_with_files_removes_only_exact_task_videos(monkeypatch, tmp_path):
    author_dir = tmp_path / "author"
    author_dir.mkdir()
    media = author_dir / "123_video.mp4"
    media.write_bytes(b"video")
    companion = author_dir / "123_cover.jpg"
    companion.write_bytes(b"cover")
    unrelated = tmp_path / "other.mp4"
    unrelated.write_bytes(b"other")
    video = TaskVideoInfo(
        id=1,
        task_id="task-1",
        aweme_id="123",
        desc="video",
        nickname="author",
        sec_user_id=None,
        create_time="now",
        status="downloaded",
        file_path="123_video.mp4",
        file_size=5,
        error_message=None,
        download_time="now",
    )
    fake_manager = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(status="completed", type="single", video_info={"aweme_id": "123"})),
        db=SimpleNamespace(get_task_videos=AsyncMock(return_value=[video])),
        delete_task=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(web, "skill", SimpleNamespace(douyin_conf={"path": str(tmp_path)}))
    monkeypatch.setattr(web, "task_manager", fake_manager)

    response = await web.delete_task("task-1", delete_files=True)

    assert response.data == {"deleted_video_files": 1}
    assert not media.exists()
    assert companion.read_bytes() == b"cover"
    assert unrelated.read_bytes() == b"other"
    fake_manager.delete_task.assert_awaited_once_with("task-1")


@pytest.mark.asyncio
async def test_status_advertises_task_file_deletion_capability(monkeypatch):
    monkeypatch.setattr(web, "skill", None)

    response = await web.get_status()

    assert response["capabilities"]["task_file_deletion"] is True


@pytest.mark.asyncio
async def test_delete_task_reports_files_removed_when_record_delete_fails(monkeypatch, tmp_path):
    media = tmp_path / "123_video.mp4"
    media.write_bytes(b"video")
    video = TaskVideoInfo(
        id=1,
        task_id="task-1",
        aweme_id="123",
        desc="video",
        nickname="author",
        sec_user_id=None,
        create_time="now",
        status="downloaded",
        file_path="123_video.mp4",
        file_size=5,
        error_message=None,
        download_time="now",
    )
    fake_manager = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(status="completed", type="single", video_info={"aweme_id": "123"})),
        db=SimpleNamespace(get_task_videos=AsyncMock(return_value=[video])),
        delete_task=AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(web, "skill", SimpleNamespace(douyin_conf={"path": str(tmp_path)}))
    monkeypatch.setattr(web, "task_manager", fake_manager)

    with pytest.raises(HTTPException) as caught:
        await web.delete_task("task-1", delete_files=True)

    assert caught.value.status_code == 500
    assert "已删除 1 个本地视频文件" in caught.value.detail
    assert not media.exists()


@pytest.mark.asyncio
async def test_delete_task_reports_files_removed_when_record_is_missing(monkeypatch, tmp_path):
    media = tmp_path / "123_video.mp4"
    media.write_bytes(b"video")
    video = TaskVideoInfo(
        id=1,
        task_id="task-1",
        aweme_id="123",
        desc="video",
        nickname="author",
        sec_user_id=None,
        create_time="now",
        status="downloaded",
        file_path="123_video.mp4",
        file_size=5,
        error_message=None,
        download_time="now",
    )
    fake_manager = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(status="completed", type="single", video_info={"aweme_id": "123"})),
        db=SimpleNamespace(get_task_videos=AsyncMock(return_value=[video])),
        delete_task=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(web, "skill", SimpleNamespace(douyin_conf={"path": str(tmp_path)}))
    monkeypatch.setattr(web, "task_manager", fake_manager)

    with pytest.raises(HTTPException) as caught:
        await web.delete_task("task-1", delete_files=True)

    assert caught.value.status_code == 500
    assert "任务记录未删除；已删除 1 个本地视频文件" in caught.value.detail
    assert not media.exists()


@pytest.mark.asyncio
async def test_delete_task_with_files_rejects_ambiguous_video_match(monkeypatch, tmp_path):
    for folder in ("one", "two"):
        directory = tmp_path / folder
        directory.mkdir()
        (directory / "123_video.mp4").write_bytes(folder.encode())
    video = TaskVideoInfo(
        id=1,
        task_id="task-1",
        aweme_id="123",
        desc="video",
        nickname="author",
        sec_user_id=None,
        create_time="now",
        status="downloaded",
        file_path="123_video.mp4",
        file_size=5,
        error_message=None,
        download_time="now",
    )
    fake_manager = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(status="completed", type="single", video_info={"aweme_id": "123"})),
        db=SimpleNamespace(get_task_videos=AsyncMock(return_value=[video])),
        delete_task=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(web, "skill", SimpleNamespace(douyin_conf={"path": str(tmp_path)}))
    monkeypatch.setattr(web, "task_manager", fake_manager)

    with pytest.raises(HTTPException) as caught:
        await web.delete_task("task-1", delete_files=True)

    assert caught.value.status_code == 409
    assert list(tmp_path.rglob("123_video.mp4"))
    fake_manager.delete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_task_cleans_ai_jobs_and_foreign_keys(tmp_path):
    db = TaskDB(str(tmp_path / "tasks.db"))
    await db.connect()
    task_id = await db.create_task("single")
    await db.create_transcript_job("transcript-1", task_id)
    await db.create_video_summary_job("summary-1", task_id, "gemini-3.7-flash", 123)
    await db.delete_task(task_id)
    assert await db.get_transcript_job(job_id="transcript-1") is None
    assert await db.get_video_summary_job(job_id="summary-1") is None
    with pytest.raises(Exception):
        await db.execute(
            "INSERT INTO transcript_jobs (job_id, task_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("orphan", "missing-task", "now", "now"),
        )
    await db.close()


@pytest.mark.asyncio
async def test_restart_marks_inflight_ai_jobs_retryable(tmp_path):
    db = TaskDB(str(tmp_path / "tasks.db"))
    await db.connect()
    task_id = await db.create_task("single")
    await db.create_transcript_job("transcript-1", task_id)
    await db.create_video_summary_job("summary-1", task_id, "gemini-3.7-flash", 123)
    result = await db.recover_interrupted_jobs()
    assert result == {"transcript": 1, "summary": 1}
    transcript = await db.get_transcript_job(job_id="transcript-1")
    summary = await db.get_video_summary_job(job_id="summary-1")
    assert transcript["status"] == "failed"
    assert transcript["stage"] == "interrupted"
    assert transcript["error"]["code"] == "JOB_INTERRUPTED"
    assert summary["status"] == "failed"
    assert summary["stage"] == "interrupted"
    await db.close()

@pytest.mark.asyncio
async def test_websocket_rejects_non_local_origin():
    closed = []

    class FakeSocket:
        headers = {"origin": "https://evil.test"}

        async def close(self, **kwargs):
            closed.append(kwargs)

    await web.websocket_endpoint(FakeSocket())
    assert closed == [{"code": 1008, "reason": "Origin 与监听端口不一致或来源不被允许"}]


@pytest.mark.asyncio
async def test_websocket_accepts_configured_port_and_rejects_other_origin():
    assert web.LAN_ORIGIN_PATTERN.replace("{port}", "8099").endswith(":8099$")
    class FakeSocket:
        client = None

        def __init__(self, origin):
            self.headers = {"origin": origin}
            self.accepted = False
            self.closed = []

        async def accept(self):
            self.accepted = True

        async def send_json(self, _data):
            pass

        async def receive_text(self):
            raise web.WebSocketDisconnect()

        async def close(self, **kwargs):
            self.closed.append(kwargs)

    web.configure_origin_port(8099)
    try:
        allowed = FakeSocket("http://127.0.0.1:8099")
        await web.websocket_endpoint(allowed)
        assert allowed.accepted
        assert not allowed.closed

        rejected = FakeSocket("http://127.0.0.1:8080")
        await web.websocket_endpoint(rejected)
        assert not rejected.accepted
        assert rejected.closed[0]["code"] == 1008
    finally:
        web.configure_origin_port(8080)
