"""手机链接取件的幂等、失败恢复与单视频下载衔接。"""

import sqlite3
from pathlib import Path

import httpx
import pytest

from skills.douyin_downloader.mobile_inbox import InboxStore, open_db
from skills.douyin_downloader.mobile_inbox_worker import MobileInboxWorker, NotVideoError, resolve_video


async def test_short_link_resolution_only_reads_redirects_and_rejects_private_target():
    seen = []

    async def transport(request):
        seen.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(302, headers={"Location": "https://www.douyin.com/video/123456"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        assert await resolve_video(client, "https://v.douyin.com/abc/") == "123456"
    assert len(seen) == 1

    async def unsafe(_):
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(unsafe)) as client:
        with pytest.raises(NotVideoError):
            await resolve_video(client, "https://v.douyin.com/abc/")


async def test_redirect_loop_has_finite_budget():
    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://v.douyin.com/abc/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(RuntimeError):
            await resolve_video(client, "https://v.douyin.com/abc/")
    assert len(requests) == 6


@pytest.fixture
def setup_worker(tmp_path):
    cloud = InboxStore(str(tmp_path / "cloud.db"))
    receipt = cloud.submit("https://www.douyin.com/video/123456", "phone-request")
    local_db = tmp_path / "local.db"
    events, batches = [], {}
    flags = {"lose_ack": False, "lose_enqueue_response": False, "login_required": False}

    async def transport(request):
        import json
        path = request.url.path
        if request.url.host == "cloud.test":
            assert request.headers["Authorization"] == "Bearer test-worker-token-only-123"
            if path == "/api/pending":
                return httpx.Response(200, json={"success": True, "data": cloud.list(True)})
            if path.endswith("/ack"):
                with open_db(str(tmp_path / "worker.db")) as conn:
                    assert conn.execute("SELECT COUNT(*) FROM mobile_receipts").fetchone()[0] == 1
                events.append("local-saved-before-ack")
                if flags["lose_ack"]:
                    raise httpx.ConnectError("模拟确认丢失")
                cloud.acknowledge(receipt["receipt_id"])
            elif path.endswith("/status"):
                body = json.loads(request.content)
                cloud.update(receipt["receipt_id"], body["status"], body["error_code"])
            return httpx.Response(200, json={"success": True})
        assert request.url.host == "127.0.0.1"
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        assert payload["video_ids"] == ["123456"]
        assert payload["auto_summary"] is True
        if flags["login_required"]:
            return httpx.Response(401, json={"detail": "不应上传到云端的错误正文"})
        key = payload["client_request_id"]
        batches.setdefault(key, {"batch_id": "local-batch"})
        events.append(key)
        if flags["lose_enqueue_response"]:
            raise httpx.ReadTimeout("入队成功但响应丢失")
        return httpx.Response(200, json={"success": True, "data": batches[key]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    worker = MobileInboxWorker(str(tmp_path / "worker.db"), str(local_db), "http://cloud.test",
                              "test-worker-token-only-123", client=client)
    return worker, cloud, receipt, events, batches, flags


async def test_receive_and_restart_do_not_duplicate_enqueue(setup_worker):
    worker, cloud, receipt, events, batches, flags = setup_worker
    await worker.tick()
    assert events[0] == "local-saved-before-ack"
    assert cloud.list()[0]["status"] == "queued"
    assert len(batches) == 1
    restarted = MobileInboxWorker(worker.db_path, worker.main_db_path, worker.remote, worker.token, client=worker.client)
    await restarted.tick()
    assert len(batches) == 1
    assert len([event for event in events if event.startswith("mobile-")]) == 1
    await worker.client.aclose()


async def test_ack_lost_after_local_commit_can_recover(setup_worker):
    worker, cloud, receipt, events, batches, flags = setup_worker
    flags["lose_ack"] = True
    await worker.tick()
    # 即使云端确认失败，已落盘记录仍可安全入队。
    assert len(batches) == 1
    flags["lose_ack"] = False
    await worker.tick()
    assert len(batches) == 1
    await worker.client.aclose()


async def test_enqueue_response_lost_reuses_same_request_after_restart(setup_worker):
    worker, cloud, receipt, events, batches, flags = setup_worker
    flags["lose_enqueue_response"] = True
    await worker.tick()
    assert len(batches) == 1
    worker.update(receipt["receipt_id"], next_try=0)
    flags["lose_enqueue_response"] = False
    await worker.tick()
    assert len(batches) == 1
    assert events[-1] == events[-2]
    assert cloud.list()[0]["status"] == "queued"
    await worker.client.aclose()


async def test_login_failure_keeps_receipt_and_can_recover(setup_worker):
    worker, cloud, receipt, events, batches, flags = setup_worker
    flags["login_required"] = True
    await worker.tick()
    row = cloud.list()[0]
    assert row["error_code"] == "LOGIN_REQUIRED"
    assert len(batches) == 0
    worker.update(receipt["receipt_id"], next_try=0)
    flags["login_required"] = False
    await worker.tick()
    assert len(batches) == 1
    assert cloud.list()[0]["error_code"] == ""
    await worker.client.aclose()


def test_download_complete_is_not_summary_complete(tmp_path):
    main = tmp_path / "main.db"
    with sqlite3.connect(main) as conn:
        conn.executescript("""
            CREATE TABLE download_queue_batches (batch_id TEXT, status TEXT);
            CREATE TABLE download_queue_items (batch_id TEXT, task_id TEXT, status TEXT);
            CREATE TABLE video_summary_jobs (task_id TEXT, status TEXT, created_at TEXT);
            CREATE TABLE distill_items (task_id TEXT, summary_json TEXT);
            INSERT INTO download_queue_batches VALUES ('batch','completed');
            INSERT INTO download_queue_items VALUES ('batch','task','skipped');
            INSERT INTO video_summary_jobs VALUES ('task','running','2026-10-22');
        """)
    worker = MobileInboxWorker(str(tmp_path / "worker.db"), str(main), "http://cloud.test", "test-worker-token-only-123")
    assert worker.processing_state("batch") == ("summarizing", "")
    with sqlite3.connect(main) as conn:
        conn.execute("UPDATE video_summary_jobs SET status='analyzing_video'")
    assert worker.processing_state("batch") == ("summarizing", "")
    with sqlite3.connect(main) as conn:
        conn.execute("UPDATE video_summary_jobs SET status='completed'")
    assert worker.processing_state("batch") == ("completed", "")
    with sqlite3.connect(main) as conn:
        conn.execute("UPDATE download_queue_items SET status='failed'")
    assert worker.processing_state("batch") == ("failed", "DOWNLOAD_FAILED")
    with sqlite3.connect(main) as conn:
        conn.execute("UPDATE download_queue_batches SET status='paused'")
    assert worker.processing_state("batch") == ("paused", "DOWNLOAD_PAUSED")
    with sqlite3.connect(main) as conn:
        conn.execute("UPDATE download_queue_batches SET status='completed'")
        conn.execute("UPDATE download_queue_items SET status='skipped'")
        conn.execute("UPDATE video_summary_jobs SET status='failed'")
        conn.execute("INSERT INTO distill_items VALUES ('task','{}')")
    assert worker.processing_state("batch") == ("completed", "")
