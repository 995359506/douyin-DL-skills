"""电脑主动领取手机分享链接，复用看板的持久化下载队列。"""

import asyncio
import fcntl
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

try:
    from .mobile_inbox import normalize_link, open_db
except ImportError:
    from mobile_inbox import normalize_link, open_db


logger = logging.getLogger("手机取件")


class NotVideoError(ValueError):
    """已确认是主页、图文或非抖音目标，不再重试。"""


async def resolve_video(client: httpx.AsyncClient, url: str) -> str:
    """短链只读取跳转头，不读取响应正文，不携带 Cookie 或模型凭据。"""
    for _ in range(6):
        try:
            url = normalize_link(url)
        except ValueError as exc:
            raise NotVideoError("链接不是单条抖音视频") from exc
        match = re.fullmatch(r"/(?:share/)?video/([0-9]{5,32})/?", urlsplit(url).path)
        if match:
            return match[1]
        async with client.stream("GET", url, follow_redirects=False) as response:
            if not response.is_redirect:
                response.raise_for_status()
            target = response.headers.get("location")
            if not response.is_redirect or not target:
                raise RuntimeError("短链接暂未给出作品地址")
            url = urljoin(url, target)
    raise RuntimeError("短链接跳转次数过多")


class MobileInboxWorker:
    """单台电脑取件；云端确认与本地入队都允许安全重试。"""

    def __init__(self, db_path: str, main_db_path: str, remote: str, token: str,
                 local: str = "http://127.0.0.1:8080", client: httpx.AsyncClient | None = None):
        remote_parts, local_parts = urlsplit(remote), urlsplit(local)
        if (remote_parts.scheme not in {"http", "https"} or not remote_parts.hostname
                or remote_parts.username or remote_parts.password or remote_parts.query or remote_parts.fragment):
            raise ValueError("服务器收件地址格式不正确")
        if local_parts.scheme != "http" or local_parts.hostname != "127.0.0.1":
            raise ValueError("电脑看板仅允许本机地址")
        if len(token) < 20:
            raise ValueError("取件口令未配置")
        self.db_path, self.main_db_path = str(db_path), str(main_db_path)
        self.remote, self.token, self.local = remote.rstrip("/"), token, local.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False)
        with open_db(self.db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS mobile_receipts (
                receipt_id TEXT PRIMARY KEY, url TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
                aweme_id TEXT, batch_id TEXT, state TEXT NOT NULL DEFAULT 'received',
                error_code TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0, reported_state TEXT NOT NULL DEFAULT '')""")

    def update(self, receipt_id: str, **fields) -> None:
        allowed = {"acknowledged", "aweme_id", "batch_id", "state", "error_code", "attempts", "next_try", "reported_state"}
        if not fields or fields.keys() - allowed:
            raise ValueError("本地收件字段不正确")
        with open_db(self.db_path) as conn:
            conn.execute("UPDATE mobile_receipts SET " + ",".join(f"{field}=?" for field in fields)
                         + " WHERE receipt_id=?", (*fields.values(), receipt_id))

    async def cloud(self, method: str, path: str, **kwargs) -> dict:
        response = await self.client.request(method, self.remote + path,
                                             headers={"Authorization": "Bearer " + self.token}, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if payload.get("success") is not True:
            raise RuntimeError("收件服务器返回异常")
        return payload

    async def receive(self) -> None:
        payload = await self.cloud("GET", "/api/pending")
        for row in payload["data"]:
            receipt_id = row["receipt_id"]
            if not re.fullmatch(r"[a-f0-9-]{36}", receipt_id):
                raise ValueError("收件编号格式异常")
            url = normalize_link(row["url"])
            # 确认丢失时服务器会重投；唯一键保留原来的解析和入队结果。
            with open_db(self.db_path) as conn:
                conn.execute("INSERT OR IGNORE INTO mobile_receipts (receipt_id,url) VALUES (?,?)", (receipt_id, url))
            await self.cloud("POST", f"/api/receipts/{receipt_id}/ack", json={})
            self.update(receipt_id, acknowledged=1)

    def retry(self, row: dict, code: str) -> None:
        attempt = row["attempts"] + 1
        self.update(row["receipt_id"], error_code=code, attempts=attempt,
                    next_try=time.time() + (10, 30, 60, 300)[min(attempt - 1, 3)])

    async def enqueue(self, row: dict) -> None:
        receipt_id = row["receipt_id"]
        try:
            aweme_id = row["aweme_id"] or await resolve_video(self.client, row["url"])
            self.update(receipt_id, aweme_id=aweme_id)
            response = await self.client.post(self.local + "/api/download-batches", json={
                "video_ids": [aweme_id], "client_request_id": "mobile-" + receipt_id, "auto_summary": True,
            })
            if response.status_code in {401, 422, 503}:
                self.retry(row, {401: "LOGIN_REQUIRED", 422: "LOCAL_CONFIG_INVALID", 503: "LOCAL_NOT_READY"}[response.status_code])
                return
            response.raise_for_status()
            batch = response.json()["data"]
            self.update(receipt_id, batch_id=batch["batch_id"], state="queued", error_code="", attempts=0, next_try=0)
        except NotVideoError:
            self.update(receipt_id, state="failed", error_code="LINK_NOT_VIDEO")
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError):
            self.retry(row, "LINK_UNRESOLVED" if not row["aweme_id"] else "LOCAL_NOT_READY")

    def processing_state(self, batch_id: str) -> tuple[str, str]:
        """只读本地状态列，不读取或上传总结正文、视频内容和模型配置。"""
        with sqlite3.connect(Path(self.main_db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
            row = conn.execute("SELECT i.task_id,i.status,b.status FROM download_queue_items i "
                               "JOIN download_queue_batches b ON b.batch_id=i.batch_id WHERE i.batch_id=?",
                               (batch_id,)).fetchone()
            if not row:
                return "failed", "RECORD_REMOVED"
            task_id, item_status, batch_status = row
            if batch_status == "cancelled":
                return "cancelled", ""
            if batch_status == "paused":
                return "paused", "DOWNLOAD_PAUSED"
            if item_status == "failed" or batch_status == "failed":
                return "failed", "DOWNLOAD_FAILED"
            if item_status not in {"completed", "skipped"}:
                return ("downloading" if item_status == "running" else "queued"), ""
            summary = conn.execute("SELECT status FROM video_summary_jobs WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                                   (task_id,)).fetchone()
            if summary and summary[0] == "completed":
                return "completed", ""
            if summary and summary[0] not in {"failed", "cancelled"}:
                return "summarizing", ""
            reused = conn.execute("SELECT 1 FROM distill_items WHERE task_id=? AND NULLIF(summary_json,'') IS NOT NULL LIMIT 1",
                                  (task_id,)).fetchone()
            if reused:
                return "completed", ""
            if summary and summary[0] in {"failed", "cancelled"}:
                return "failed", "SUMMARY_FAILED"
            return "summarizing", ""

    async def tick(self) -> None:
        try:
            await self.receive()
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError):
            logger.warning("手机收件服务器暂时无法连接，已保存的本地任务继续处理")
        with open_db(self.db_path) as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM mobile_receipts ORDER BY rowid")]
        for row in rows:
            receipt_id = row["receipt_id"]
            if not row["batch_id"] and row["state"] != "failed" and row["next_try"] <= time.time():
                await self.enqueue(row)
            if row["batch_id"] and row["state"] != "completed":
                try:
                    state, code = self.processing_state(row["batch_id"])
                    self.update(receipt_id, state=state, error_code=code)
                except sqlite3.Error:
                    logger.warning("电脑看板状态暂不可读，保留手机回执后稍后检查")
            with open_db(self.db_path) as conn:
                latest = dict(conn.execute("SELECT * FROM mobile_receipts WHERE receipt_id=?", (receipt_id,)).fetchone())
            signature = latest["state"] + ":" + latest["error_code"]
            if latest["reported_state"] != signature:
                try:
                    # 只回传固定状态与错误码，不回传上游错误正文。
                    await self.cloud("POST", f"/api/receipts/{receipt_id}/status",
                                     json={"status": latest["state"], "error_code": latest["error_code"]})
                    self.update(receipt_id, reported_state=signature)
                except (httpx.HTTPError, RuntimeError, ValueError):
                    pass


async def run() -> None:
    os.umask(0o077)
    state = Path(os.environ.get("DOUYIN_INBOX_STATE_DIR", "~/Library/Application Support/DouyinDownloader/mobile-inbox")).expanduser()
    state.mkdir(parents=True, exist_ok=True)
    # 单台电脑仅一个取件进程；避免重复解析和同时覆盖回执状态。
    with (state / "worker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = MobileInboxWorker(str(state / "receipts.db"),
                                  os.environ.get("DOUYIN_INBOX_MAIN_DB", "douyin_downloader.db"),
                                  os.environ["DOUYIN_INBOX_URL"], os.environ["DOUYIN_INBOX_WORKER_TOKEN"])
        logger.info("手机链接取件已启动，每 10 秒检查一次服务器")
        try:
            while True:
                try:
                    await worker.tick()
                except Exception as exc:
                    logger.error("手机取件发生异常，已保留本地记录；异常类型：%s", type(exc).__name__)
                await asyncio.sleep(10)
        finally:
            await worker.client.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # HTTP 客户端默认 INFO 会打印链接；只保留本模块的固定中文状态日志。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run())
