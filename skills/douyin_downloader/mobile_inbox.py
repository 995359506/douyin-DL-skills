"""手机链接收件箱：仅保存链接与状态，可独立部署到服务器。"""

import argparse
import hmac
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit


logger = logging.getLogger("手机收件")

DOUYIN_HOSTS = {"douyin.com", "www.douyin.com", "v.douyin.com", "iesdouyin.com", "www.iesdouyin.com"}
STATUSES = {
    "pending": "服务器已收下，等待电脑接收",
    "received": "电脑已接收，等待处理",
    "queued": "已加入下载队列",
    "downloading": "正在下载",
    "summarizing": "下载已完成，正在总结",
    "completed": "总结已完成，请到电脑看板查看",
    "paused": "下载已暂停，请到电脑看板处理后继续",
    "failed": "处理失败，请到电脑看板查看",
    "cancelled": "任务已停止",
}
ERRORS = {
    "": "",
    "LINK_NOT_VIDEO": "该链接不是单条视频，请重新分享视频链接",
    "LINK_UNRESOLVED": "链接暂时无法解析，电脑将稍后重试",
    "LOCAL_NOT_READY": "电脑看板暂未就绪，将自动重试",
    "LOGIN_REQUIRED": "请在电脑看板重新扫码登录",
    "LOCAL_CONFIG_INVALID": "请检查电脑看板的下载与总结配置",
    "DOWNLOAD_PAUSED": "下载已暂停，请在电脑看板处理后继续",
    "DOWNLOAD_FAILED": "下载失败，请在电脑看板查看并重试",
    "SUMMARY_FAILED": "视频已下载，总结失败，可在电脑看板重试",
    "RECORD_REMOVED": "本地队列记录已删除，请在看板核对后重新提交",
}
SUBMISSION_ERRORS = {
    "LINK_TOO_LONG": "抖音链接过长，请重新复制单条视频链接",
    "LINK_INVALID": "仅接收抖音视频链接",
    "SHORT_LINK_INVALID": "抖音短链接格式不正确，请重新复制",
    "LINK_NOT_VIDEO": "请分享单条视频，不接收主页、图文或合集链接",
    "SHARE_EMPTY": "没有收到分享内容，请先在抖音复制视频链接，再运行快捷指令",
    "SHARE_TOO_LONG": "分享内容超过 8192 字符，请只复制一条视频链接",
    "LINK_COUNT_INVALID": "没有识别到唯一的视频链接，请在抖音复制单条视频链接后重试",
    "REQUEST_ID_INVALID": "快捷指令的提交编号无效，请更新快捷指令后重试",
    "REQUEST_ID_CONFLICT": "同一提交编号不能对应不同视频，请重新运行快捷指令",
    "INBOX_FULL": "待接收链接已满，请等电脑处理后再提交",
}


class SubmissionError(ValueError):
    """仅返回预先定义的错误，不回传分享内容或请求正文。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(SUBMISSION_ERRORS[code])


def normalize_link(value: str) -> str:
    """限制为公开抖音地址；去掉不影响作品识别的跟踪参数。"""
    if not isinstance(value, str) or len(value) > 2048:
        raise SubmissionError("LINK_TOO_LONG")
    parsed = urlsplit(value)
    try:
        invalid_port = parsed.port not in {None, 80, 443}
    except ValueError:
        invalid_port = True
    if (parsed.scheme not in {"https", "http"} or parsed.hostname not in DOUYIN_HOSTS
            or parsed.username or parsed.password or invalid_port):
        raise SubmissionError("LINK_INVALID")
    query = parse_qs(parsed.query)
    if parsed.hostname == "v.douyin.com":
        if not re.fullmatch(r"/[A-Za-z0-9_-]+/?", parsed.path):
            raise SubmissionError("SHORT_LINK_INVALID")
        return urlunsplit(("https", parsed.hostname, parsed.path, "", ""))
    if re.fullmatch(r"/(?:share/)?video/[0-9]{5,32}/?", parsed.path):
        return urlunsplit(("https", parsed.hostname, parsed.path, "", ""))
    modal_id = query.get("modal_id", [""])[0]
    if re.fullmatch(r"[0-9]{5,32}", modal_id):
        return f"https://www.douyin.com/video/{modal_id}"
    raise SubmissionError("LINK_NOT_VIDEO")


def extract_link(text: str) -> str:
    """接受整段分享文案，但仅保存其中唯一的规范化视频链接。"""
    if not isinstance(text, str) or not text.strip():
        raise SubmissionError("SHARE_EMPTY")
    if len(text) > 8192:
        raise SubmissionError("SHARE_TOO_LONG")
    candidates = re.findall(r'''https?://[^\s<>"'`()[\]，。！？；：、（）【】《》“”‘’]+''', text)
    urls = []
    for candidate in candidates:
        if urlsplit(candidate).hostname in DOUYIN_HOSTS:
            urls.append(normalize_link(candidate.rstrip(".,!;:?")))
    urls = list(dict.fromkeys(urls))
    if len(urls) != 1:
        raise SubmissionError("LINK_COUNT_INVALID")
    return urls[0]


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


class InboxStore:
    """单台电脑领取；本地落盘后确认，确认丢失时允许重复领取。"""

    def __init__(self, path: str):
        self.path = str(path)
        with open_db(self.path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS receipts (
                receipt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL, status TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS submissions (
                request_id TEXT PRIMARY KEY, url TEXT NOT NULL, receipt_id TEXT NOT NULL)""")

    def submit(self, text: str, request_id: str) -> dict:
        url = extract_link(text)
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise SubmissionError("REQUEST_ID_INVALID")
        with open_db(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT r.* FROM submissions s JOIN receipts r ON r.receipt_id=s.receipt_id "
                               "WHERE s.request_id=?", (request_id,)).fetchone()
            if row:
                if row["url"] != url:
                    raise SubmissionError("REQUEST_ID_CONFLICT")
                return dict(row)
            # 同一视频的重复分享复用既有回执；失败后重新分享允许重新提交。
            row = conn.execute("SELECT * FROM receipts WHERE url=? AND status NOT IN ('failed','cancelled') "
                               "ORDER BY created_at DESC LIMIT 1", (url,)).fetchone()
            if row:
                conn.execute("INSERT INTO submissions VALUES (?,?,?)", (request_id, url, row["receipt_id"]))
                return dict(row)
            if conn.execute("SELECT COUNT(*) FROM receipts WHERE status='pending'").fetchone()[0] >= 2000:
                raise SubmissionError("INBOX_FULL")
            now = time.time()
            receipt_id = str(uuid.uuid4())
            conn.execute("INSERT INTO receipts VALUES (?,?,?,'pending','',?,?)",
                         (receipt_id, request_id, url, now, now))
            conn.execute("INSERT INTO submissions VALUES (?,?,?)", (request_id, url, receipt_id))
            return dict(conn.execute("SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)).fetchone())

    def list(self, pending: bool = False) -> list[dict]:
        with open_db(self.path) as conn:
            rows = conn.execute("SELECT * FROM receipts WHERE status='pending' ORDER BY created_at LIMIT 20"
                                if pending else "SELECT * FROM receipts ORDER BY created_at DESC LIMIT 50").fetchall()
            return [dict(row) for row in rows]

    def update(self, receipt_id: str, status: str, code: str = "") -> dict:
        if status not in STATUSES or status == "pending" or code not in ERRORS:
            raise ValueError("任务状态不正确")
        with open_db(self.path) as conn:
            row = conn.execute("SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
            if not row:
                raise KeyError(receipt_id)
            # 重复确认不能把已入队或已完成状态退回“电脑已接收”。
            if status != "received" or row["status"] in {"pending", "received"}:
                conn.execute("UPDATE receipts SET status=?,error_code=?,updated_at=? WHERE receipt_id=?",
                             (status, code, time.time(), receipt_id))
            return dict(conn.execute("SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)).fetchone())

    def acknowledge(self, receipt_id: str) -> dict:
        """重复领取只确认投递，不清除后续处理状态与错误提示。"""
        with open_db(self.path) as conn:
            conn.execute("UPDATE receipts SET status='received',updated_at=? "
                         "WHERE receipt_id=? AND status='pending'", (time.time(), receipt_id))
            row = conn.execute("SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
            if not row:
                raise KeyError(receipt_id)
            return dict(row)


def public_receipt(row: dict) -> dict:
    return {"receipt_id": row["receipt_id"], "status": row["status"], "url": row["url"],
            "message": ERRORS[row["error_code"]] or STATUSES[row["status"]], "created_at": row["created_at"]}


def make_server(store: InboxStore, submit_token: str, worker_token: str, page: Path,
                port: int = 8025) -> ThreadingHTTPServer:
    """只绑定回环地址，由现有 Nginx 转发手机收件路径。"""
    if len(submit_token) < 20 or len(worker_token) < 20 or submit_token == worker_token:
        raise ValueError("请配置两个独立的收件与取件口令")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            # 不记录访问口令、分享文案、URL 或完整请求。
            return

        def reply(self, status: int, payload: dict) -> None:
            if self.command == "POST" and self.path == "/api/receipts":
                logger.info("手机链接提交结果：HTTP %d", status)
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self, worker: bool = False) -> bool:
            expected = worker_token if worker else submit_token
            actual = self.headers.get("Authorization", "")
            if not hmac.compare_digest(actual.encode(), ("Bearer " + expected).encode()):
                self.reply(401, {"success": False, "message": "收件口令不正确"})
                return False
            return True

        def do_GET(self) -> None:
            if self.path in {"/", "/index.html"}:
                data = page.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/health":
                self.reply(200, {"success": True})
            elif self.path in {"/api/pending", "/api/receipts"}:
                if self.authorized(worker=self.path == "/api/pending"):
                    rows = store.list(pending=self.path == "/api/pending")
                    self.reply(200, {"success": True, "data": rows if self.path == "/api/pending"
                                     else [public_receipt(row) for row in rows]})
            else:
                self.reply(404, {"success": False, "message": "没有这个入口"})

        def do_POST(self) -> None:
            match = re.fullmatch(r"/api/receipts/([a-f0-9-]{36})/(ack|status)", self.path)
            if self.path != "/api/receipts" and not match:
                self.reply(404, {"success": False, "message": "没有这个入口"})
                return
            if not self.authorized(worker=bool(match)):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= 16384:
                    self.reply(413, {"success": False, "message": "请只发送一条分享内容"})
                    return
                self.connection.settimeout(10)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求格式不正确")
                if match:
                    row = (store.acknowledge(match[1]) if match[2] == "ack" else
                           store.update(match[1], body.get("status"), body.get("error_code", "")))
                    status = 200
                else:
                    logger.info("手机提交字段类型：分享内容=%s，提交编号=%s",
                                type(body.get("text")).__name__, type(body.get("request_id")).__name__)
                    logger.info("手机提交校验：分享字数=%d，编号格式有效=%s",
                                len(body["text"]) if isinstance(body.get("text"), str) else 0,
                                bool(isinstance(body.get("request_id"), str) and
                                     re.fullmatch(r"[A-Za-z0-9_-]{1,128}", body["request_id"])))
                    row = store.submit(body.get("text"), body.get("request_id"))
                    status = 202
                self.reply(status, {"success": True, "message": STATUSES[row["status"]], "data": public_receipt(row)})
            except SubmissionError as exc:
                logger.info("手机提交未通过：%s", exc.code)
                self.reply(400, {"success": False, "code": exc.code, "message": str(exc)})
            except (ValueError, TypeError, UnicodeError):
                self.reply(400, {"success": False, "code": "REQUEST_INVALID",
                                 "message": "提交格式不正确，请重新复制单条视频链接后重试"})
            except KeyError:
                self.reply(404, {"success": False, "message": "找不到该回执"})
            except (sqlite3.Error, OSError):
                self.reply(503, {"success": False, "message": "收件服务暂不可用，请保留原内容稍后重试"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="手机抖音链接收件箱")
    parser.add_argument("--db", required=True)
    parser.add_argument("--page", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8025)
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    make_server(InboxStore(args.db), os.environ.get("DOUYIN_INBOX_SUBMIT_TOKEN", ""),
                os.environ.get("DOUYIN_INBOX_WORKER_TOKEN", ""), args.page, args.port).serve_forever()


if __name__ == "__main__":
    main()
