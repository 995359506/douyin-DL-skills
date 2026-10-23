"""手机链接接收、持久化与访问权限，不发起真实下载。"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from skills.douyin_downloader.mobile_inbox import InboxStore, extract_link, make_server, normalize_link


@pytest.fixture
def store(tmp_path):
    return InboxStore(str(tmp_path / "inbox.db"))


@pytest.mark.parametrize("text,expected", [
    ("打开抖音查看 https://v.douyin.com/Abc123/。", "https://v.douyin.com/Abc123/"),
    ("[https://v.douyin.com/Abc123/](https://v.douyin.com/Abc123/)", "https://v.douyin.com/Abc123/"),
    ("https://www.douyin.com/video/123456?utm_source=share", "https://www.douyin.com/video/123456"),
    ("https://www.douyin.com/user/test?modal_id=123456", "https://www.douyin.com/video/123456"),
])
def test_share_extraction_keeps_only_video_link(text, expected):
    assert extract_link(text) == expected


@pytest.mark.parametrize("url", [
    "https://www.douyin.com/user/test", "https://www.douyin.com/note/123456",
    "https://www.douyin.com.evil.test/video/123456", "https://127.0.0.1/video/123456",
    "https://user:password@www.douyin.com/video/123456", "https://www.douyin.com:8443/video/123456",
    "file:///video/123456", "https://v.douyin.com/../../etc/passwd",
])
def test_nonvideo_and_untrusted_urls_rejected(url):
    with pytest.raises(ValueError):
        normalize_link(url)


def test_two_different_video_links_rejected():
    with pytest.raises(ValueError):
        extract_link("https://v.douyin.com/first/ https://v.douyin.com/second/")


def test_retry_and_restart_keep_same_receipt_and_drop_share_prose(store):
    row = store.submit("不会保存的分享文案 https://v.douyin.com/abc/", "request-1")
    restarted = InboxStore(store.path)
    assert restarted.submit("https://v.douyin.com/abc/", "request-1")["receipt_id"] == row["receipt_id"]
    assert "不会保存" not in json.dumps(restarted.list())
    with pytest.raises(ValueError):
        restarted.submit("https://v.douyin.com/different/", "request-1")


def test_duplicate_request_alias_is_immutable_even_after_failure(store):
    first = store.submit("https://v.douyin.com/abc/", "first")
    duplicate = store.submit("https://v.douyin.com/abc/", "second")
    assert duplicate["receipt_id"] == first["receipt_id"]
    store.update(first["receipt_id"], "failed", "SUMMARY_FAILED")
    assert store.submit("https://v.douyin.com/abc/", "second")["receipt_id"] == first["receipt_id"]
    assert store.submit("https://v.douyin.com/abc/", "new-manual-retry")["receipt_id"] != first["receipt_id"]
    with pytest.raises(ValueError):
        store.submit("https://v.douyin.com/other/", "second")


def test_parallel_submission_is_atomic(store):
    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(lambda _: store.submit("https://v.douyin.com/abc/", "same")["receipt_id"], range(8)))
    assert len(set(ids)) == 1
    assert len(store.list(True)) == 1


def test_ack_is_idempotent_and_does_not_erase_newer_state(store):
    row = store.submit("https://v.douyin.com/abc/", "request")
    assert len(store.list(True)) == 1
    store.update(row["receipt_id"], "received")
    assert store.list(True) == []
    store.update(row["receipt_id"], "completed")
    assert store.update(row["receipt_id"], "received")["status"] == "completed"


def test_received_error_is_reported_and_repeated_ack_preserves_it(store):
    row = store.submit("https://v.douyin.com/abc/", "request")
    store.acknowledge(row["receipt_id"])
    failed = store.update(row["receipt_id"], "received", "LOGIN_REQUIRED")
    assert failed["error_code"] == "LOGIN_REQUIRED"
    assert store.acknowledge(row["receipt_id"])["error_code"] == "LOGIN_REQUIRED"
    store.update(row["receipt_id"], "queued")
    assert store.acknowledge(row["receipt_id"])["status"] == "queued"


@pytest.fixture
def inbox_http(store, tmp_path):
    page = tmp_path / "page.html"
    page.write_text("手机收件入口")
    server = make_server(store, "test-submit-token-only-123", "test-worker-token-only-456", page, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


async def test_http_roles_limits_and_status_have_no_secret_or_raw_prose(inbox_http, capsys):
    submit = {"Authorization": "Bearer test-submit-token-only-123"}
    worker = {"Authorization": "Bearer test-worker-token-only-456"}
    async with httpx.AsyncClient(base_url=inbox_http, trust_env=False) as client:
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/api/receipts")).status_code == 401
        assert (await client.get("/api/pending", headers=submit)).status_code == 401
        assert (await client.post("/api/receipts", headers=worker, json={})).status_code == 401
        response = await client.post("/api/receipts", headers=submit, json={
            "text": "不应保存的分享文字 https://v.douyin.com/abc/", "request_id": "request",
        })
        assert response.status_code == 202
        data = response.json()
        receipt_id = data["data"]["receipt_id"]
        assert "等待电脑" in data["message"]
        assert "不应保存" not in response.text
        assert (await client.post(f"/api/receipts/{receipt_id}/ack", headers=submit, json={})).status_code == 401
        assert (await client.post(f"/api/receipts/{receipt_id}/ack", headers=worker, json={})).status_code == 200
        assert (await client.get("/api/pending", headers=worker)).json()["data"] == []
        bad = await client.post(f"/api/receipts/{receipt_id}/status", headers=worker,
                                json={"status": "failed", "error_code": "任意上游响应正文"})
        assert bad.status_code == 400
        too_large = await client.post("/api/receipts", headers=submit, content=b"x" * 17000)
        assert too_large.status_code == 413
    output = capsys.readouterr()
    assert "test-submit-token" not in output.out + output.err
    assert "不应保存" not in output.out + output.err


async def test_submission_logs_only_status_and_field_types(inbox_http, caplog):
    caplog.set_level(logging.INFO, logger="手机收件")
    token = "test-submit-token-only-123"
    async with httpx.AsyncClient(base_url=inbox_http, trust_env=False) as client:
        response = await client.post("/api/receipts", headers={"Authorization": "Bearer " + token}, json={
            "text": "不应进入日志的分享内容 https://v.douyin.com/private-share/", "request_id": "private-request",
        })
        assert response.status_code == 202
        response = await client.post("/api/receipts", json={"text": "不能记录"})
        assert response.status_code == 401
    logs = "\n".join(r.getMessage() for r in caplog.records if r.name == "手机收件")
    assert "HTTP 202" in logs and "HTTP 401" in logs
    assert "分享内容=str，提交编号=str" in logs
    for value in (token, "private-share", "private-request", "不应进入日志", "不能记录"):
        assert value not in logs


@pytest.mark.parametrize("payload,code,message", [
    ({"text": "", "request_id": "request"}, "SHARE_EMPTY", "先在抖音复制"),
    ({"text": "私人的剪贴板正文", "request_id": "request"}, "LINK_COUNT_INVALID", "单条视频"),
    ({"text": "https://www.douyin.com/user/private", "request_id": "request"}, "LINK_NOT_VIDEO", "不接收主页"),
    ({"text": "https://v.douyin.com/private/", "request_id": "mobile-123,456"},
     "REQUEST_ID_INVALID", "更新快捷指令"),
])
async def test_submission_failure_has_safe_specific_feedback(inbox_http, store, caplog, payload, code, message):
    caplog.set_level(logging.INFO, logger="手机收件")
    async with httpx.AsyncClient(base_url=inbox_http, trust_env=False) as client:
        response = await client.post("/api/receipts", headers={
            "Authorization": "Bearer test-submit-token-only-123",
        }, json=payload)
    assert response.status_code == 400
    assert response.json()["code"] == code
    assert message in response.json()["message"]
    assert store.list() == []
    logs = "\n".join(r.getMessage() for r in caplog.records if r.name == "手机收件")
    assert code in logs
    if payload["text"]:
        assert payload["text"] not in logs
    assert payload["request_id"] not in logs
    assert "私人的" not in response.text and "private" not in response.text


async def test_malformed_json_feedback_never_echoes_request(inbox_http):
    async with httpx.AsyncClient(base_url=inbox_http, trust_env=False) as client:
        response = await client.post("/api/receipts", headers={
            "Authorization": "Bearer test-submit-token-only-123",
        }, content=b'{"private-content":')
    assert response.status_code == 400
    assert response.json()["code"] == "REQUEST_INVALID"
    assert "private-content" not in response.text
