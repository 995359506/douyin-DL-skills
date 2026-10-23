"""本地内容索引、不可变摘录和专题版本；不通过读取触发下载或模型。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from .usage_ledger import ModelControlError
except ImportError:
    from usage_ledger import ModelControlError


SOURCE_LABELS = {"original": "原始转写（未核实）", "polished": "AI 整理稿", "summary": "AI 总结", "distill": "博主蒸馏归纳", "note": "个人笔记", "metadata": "视频信息"}
SUMMARY_FIELDS = {"title", "one_sentence", "overview", "summary", "key_points", "timeline", "visual_content", "spoken_content", "people_and_objects", "narrative_structure", "emotional_arc", "editing_and_expression", "reusable_elements", "audience_and_intent", "keywords", "uncertainties"}
ACTIONS = {"points": "整理素材要点", "structure": "提炼表达结构", "angles": "生成写作角度", "outline": "生成提纲", "draft": "生成初稿"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def normalize_text(text: str) -> tuple[str, list[int]]:
    """逐字符规范化并保留回到原文的下标，不拼接标点两侧的文字。"""
    output, positions = [], []
    index = 0
    while index < len(text):
        end = index + 1
        while end < len(text) and unicodedata.category(text[end]).startswith("M"):
            end += 1
        normalized = unicodedata.normalize("NFKC", text[index:end]).casefold()
        for item in normalized:
            output.append(item)
            positions.append(index)
        index = end
    return "".join(output), positions


def _utf16_offset(text: str, index: int) -> int:
    """HTTP 范围与浏览器原生字符串下标一致，避免表情导致摘录错位。"""
    return len(text[:index].encode("utf-16-le")) // 2


def _codepoint_offset(text: str, offset: int) -> int:
    if offset < 0:
        raise ValueError("摘录范围无效，请重新选中文字")
    units = 0
    for index, char in enumerate(text):
        if units == offset:
            if unicodedata.category(char).startswith("M"):
                raise ValueError("摘录范围不能截断一个字符，请重新选择")
            return index
        units += 2 if ord(char) > 0xFFFF else 1
        if units > offset:
            raise ValueError("摘录范围不能截断一个字符，请重新选择")
    if units == offset:
        return len(text)
    raise ValueError("摘录范围超出原文，请重新选中文字")


def query_terms(query: str) -> list[str]:
    """空格分词为 AND；引号中的短语连续匹配；操作符按普通文字处理。"""
    if len(query) > 500:
        raise ValueError("搜索词最多 500 个字符")
    terms = [normalize_text(item[0] or item[1])[0] for item in re.findall(r'"([^"]+)"|(\S+)', query)]
    return list(dict.fromkeys(term for term in terms if term.strip()))


def normalize_time(value: Any, duration: Optional[float] = None) -> Optional[float]:
    """兼容零秒、数字、分秒、时分秒及范围；非法时间不生成播放位置。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (float, int)):
            seconds = float(value)
        else:
            text = unicodedata.normalize("NFKC", str(value)).strip().lstrip("约~≈ ")
            text = re.split(r"\s*(?:–|—|~|至|到)\s*", text)[0]
            # 仅将形如 01:02-01:05 的连字符视为范围。
            if re.match(r"^\d+(?:\.\d+)?(?::\d+){0,2}\s*-", text):
                text = text.split("-", 1)[0].strip()
            pieces = text.split(":")
            if len(pieces) in (2, 3):
                numbers = [float(piece) for piece in pieces]
                if any(number < 0 for number in numbers) or any(number >= 60 for number in numbers[1:]):
                    return None
                seconds = sum(number * 60 ** index for index, number in enumerate(reversed(numbers)))
            else:
                seconds = float(text.removesuffix("秒").strip())
        if not math.isfinite(seconds) or seconds < 0 or (duration is not None and duration > 0 and seconds > duration):
            return None
        return seconds
    except (TypeError, ValueError, OverflowError):
        return None


class ContentService:
    """对已有资料建立可修复索引，个人数据不依赖下载任务的生命周期。"""

    def __init__(self, db: Any, resolve_media: Optional[Callable] = None, generator: Optional[Callable] = None):
        self.db = db
        self.resolve_media = resolve_media
        self.generator = generator
        self._refresh_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._generation_locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        statements = [
            "CREATE TABLE IF NOT EXISTS content_sources (source_id TEXT PRIMARY KEY, revision TEXT NOT NULL, aweme_id TEXT NOT NULL, source_type TEXT NOT NULL, normalized_text TEXT NOT NULL, payload_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)",
            "CREATE INDEX IF NOT EXISTS idx_content_sources_video ON content_sources(aweme_id,active)",
            "CREATE TABLE IF NOT EXISTS content_source_revisions (source_id TEXT NOT NULL, revision TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(source_id,revision))",
            "CREATE TABLE IF NOT EXISTS content_reading (aweme_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending', rewatch INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS content_materials (material_id TEXT PRIMARY KEY, request_id TEXT UNIQUE, duplicate_key TEXT NOT NULL, snapshot_json TEXT NOT NULL, title TEXT NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '', rewrite TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_content_material_duplicate ON content_materials(duplicate_key)",
            "CREATE TABLE IF NOT EXISTS content_topics (topic_id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', settings_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS content_topic_members (member_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, snapshot_json TEXT NOT NULL, group_name TEXT NOT NULL DEFAULT '', position INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_content_topic_members ON content_topic_members(topic_id,position)",
            "CREATE TABLE IF NOT EXISTS content_topic_versions (version_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, status TEXT NOT NULL, action TEXT NOT NULL, inputs_json TEXT NOT NULL, settings_json TEXT NOT NULL, output_markdown TEXT NOT NULL DEFAULT '', edited_markdown TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        ]
        for statement in statements:
            await self.db.execute(statement)
        await self.db.commit()
        # 服务重启保留输入版本，避免悄悄重发已在途的收费请求。
        await self.db.execute("UPDATE content_topic_versions SET status='interrupted', error='服务重启，生成结果尚未确认；请检查后主动重试', updated_at=? WHERE status='running'", (_now(),))
        await self.db.commit()

    async def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        cursor = await self.db.execute(sql, args)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in await cursor.fetchall()]

    async def _collect(self) -> list[dict]:
        videos = await self._rows("SELECT tv.*, t.created_at AS collected_at, t.completed_at, t.status AS task_status FROM task_videos tv JOIN tasks t ON t.task_id=tv.task_id ORDER BY CASE WHEN t.status='completed' THEN 0 ELSE 1 END, COALESCE(t.completed_at,t.created_at) DESC")
        by_task, by_video = {}, {}
        for video in videos:
            by_task.setdefault(video["task_id"], video)
            by_video.setdefault(video["aweme_id"], video)
        docs: list[dict] = []

        def metadata(video: dict, **extra: Any) -> dict:
            aweme_id = str(video.get("aweme_id") or "")
            return {"aweme_id": aweme_id, "task_id": video.get("task_id"), "title": video.get("desc") or video.get("title") or aweme_id, "author": video.get("nickname") or "未知作者", "cover": video.get("cover"), "collected_at": video.get("collected_at") or video.get("created_at"), "source_url": f"https://www.douyin.com/video/{aweme_id}", "duration": normalize_time(video.get("duration")), **extra}

        def append(base: dict, owner: str, kind: str, text: Any, path: str, seconds: Any = None, basis: str = "none") -> None:
            if not isinstance(text, str) or not text.strip():
                return
            source_id = hashlib.sha256(f"{owner}:{kind}:{path}".encode()).hexdigest()[:32]
            data = {**base, "source_id": source_id, "source_type": kind, "source_label": SOURCE_LABELS[kind], "text": text, "field_path": path, "block_id": path, "time_seconds": normalize_time(seconds, base.get("duration")) if basis != "unknown" else None, "time_basis": basis}
            revision = hashlib.sha256(_json(data).encode()).hexdigest()
            docs.append({**data, "revision": revision})

        def summary(base: dict, owner: str, parsed: dict, processing_mode: str = "") -> None:
            if not isinstance(parsed, dict):
                return
            # 旧分段结果未经确定性换算，仍能阅读但不提供误导的跳转。
            timing = parsed.get("_time_basis") or parsed.get("timing_basis") or parsed.get("_timing_basis")
            reliable = {"absolute", "full_video", "model_estimated", "absolute_model_estimated", "mapped_estimate", "model_estimate"}
            uncertain = {"unknown", "unknown_segment_basis"}
            basis = "unknown" if timing in uncertain else "model_estimated" if timing in reliable or processing_mode not in {"segmented", "segments", "chunked"} else "unknown"
            def walk(value: Any, path: str, time_value: Any = None, time_basis: str = basis) -> None:
                if isinstance(value, str):
                    append(base, owner, "summary", value, path, time_value, time_basis if time_value is not None else "none")
                elif isinstance(value, list):
                    for index, entry in enumerate(value):
                        walk(entry, f"{path}.{index}", time_value, time_basis)
                elif isinstance(value, dict):
                    local_time = value.get("time_seconds", value.get("time", time_value))
                    local_basis = value.get("time_basis", time_basis)
                    local_basis = "unknown" if local_basis in uncertain else "model_estimated" if local_basis in reliable else time_basis
                    for key, entry in value.items():
                        if key not in {"time", "start", "end", "time_basis", "time_seconds", "confidence"}:
                            walk(entry, f"{path}.{key}", local_time, local_basis)
            for field in SUMMARY_FIELDS:
                if field in parsed:
                    walk(parsed[field], field)

        for aweme_id, video in by_video.items():
            base = metadata(video)
            append(base, f"video:{aweme_id}", "metadata", f"{base['title']}\n{base['author']}", "metadata")
        for row in await self._rows("SELECT * FROM transcript_jobs WHERE original_transcript IS NOT NULL OR polished_text IS NOT NULL"):
            if video := by_task.get(row["task_id"]):
                for kind, field in (("original", "original_transcript"), ("polished", "polished_text")):
                    append(metadata(video), f"transcript:{row['job_id']}", kind, row.get(field), field)
        for row in await self._rows("SELECT * FROM video_summary_jobs WHERE summary_json IS NOT NULL"):
            if video := by_task.get(row["task_id"]):
                summary(metadata(video), f"summary:{row['job_id']}", _load(row.get("summary_json"), {}), row.get("processing_mode") or "")
        for item in await self._rows("SELECT di.*, dr.nickname, dr.created_at AS collected_at FROM distill_items di JOIN distill_runs dr ON dr.run_id=di.run_id"):
            info = _load(item.get("metadata_json"), {})
            info = info if isinstance(info, dict) else {}
            fallback = by_video.get(item["aweme_id"], {})
            base = metadata({**fallback, **info, "aweme_id": item["aweme_id"], "task_id": item.get("task_id") or fallback.get("task_id"), "nickname": info.get("nickname") or item["nickname"], "desc": info.get("desc") or fallback.get("desc"), "collected_at": item["collected_at"]}, run_id=item["run_id"])
            owner = f"distill:{item['item_id']}"
            append(base, owner, "metadata", f"{base['title']}\n{base['author']}", "metadata")
            for kind, field in (("original", "original_transcript"), ("polished", "polished_text")):
                append(base, owner, kind, item.get(field), field)
            # 历史蒸馏总结的分段信息缺失，采用保守时间依据。
            summary(base, owner, _load(item.get("summary_json"), {}), "segmented")
        for run in await self._rows("SELECT run_id,nickname,synthesis_json,created_at FROM distill_runs WHERE synthesis_json IS NOT NULL"):
            parsed = _load(run["synthesis_json"], {})
            base = {"aweme_id": f"distill:{run['run_id']}", "task_id": None, "title": f"{run['nickname']} · 博主蒸馏", "author": run["nickname"], "cover": None, "collected_at": run["created_at"], "source_url": None, "run_id": run["run_id"], "duration": None}
            def synthesis_walk(value: Any, path: str) -> None:
                if isinstance(value, str):
                    append(base, f"synthesis:{run['run_id']}", "distill", value, path)
                elif isinstance(value, list):
                    for index, entry in enumerate(value):
                        synthesis_walk(entry, f"{path}.{index}")
                elif isinstance(value, dict):
                    for key, entry in value.items():
                        if key not in {"comment_ideas", "request", "response", "usage", "response_meta", "local_path", "file_path", "output_path", "source_path"}:
                            synthesis_walk(entry, f"{path}.{key}")
            synthesis_walk(parsed, "synthesis")
        return docs

    async def refresh(self) -> dict:
        """读取时校对源指纹，更新和删除立即可见；失败不破坏旧索引。"""
        async with self._refresh_lock:
            docs = await self._collect()
            current = {row[0]: row[1] for row in await self.db.fetch_all("SELECT source_id,revision FROM content_sources WHERE active=1")}
            active = {doc["source_id"] for doc in docs}
            changed = 0
            for doc in docs:
                if current.get(doc["source_id"]) == doc["revision"]:
                    continue
                serialized = _json(doc)
                await self.db.execute("INSERT OR IGNORE INTO content_source_revisions VALUES (?,?,?,?)", (doc["source_id"], doc["revision"], serialized, _now()))
                await self.db.execute("INSERT INTO content_sources VALUES (?,?,?,?,?,?,1) ON CONFLICT(source_id) DO UPDATE SET revision=excluded.revision,aweme_id=excluded.aweme_id,source_type=excluded.source_type,normalized_text=excluded.normalized_text,payload_json=excluded.payload_json,active=1", (doc["source_id"], doc["revision"], doc["aweme_id"], doc["source_type"], normalize_text(doc["text"])[0], serialized))
                changed += 1
            for source_id in current.keys() - active:
                await self.db.execute("UPDATE content_sources SET active=0 WHERE source_id=?", (source_id,))
            await self.db.commit()
            return {"documents": len(docs), "updated": changed, "removed": len(current.keys() - active), "strategy": "完整本地扫描，按指纹增量更新；支持任意长度中文"}

    async def _sources(self) -> list[dict]:
        await self.refresh()
        return [_load(row[0], {}) for row in await self.db.fetch_all("SELECT payload_json FROM content_sources WHERE active=1")]

    async def get_source(self, source_id: str, revision: Optional[str] = None) -> dict:
        await self.refresh()
        if revision:
            row = await self.db.fetch_one("SELECT payload_json FROM content_source_revisions WHERE source_id=? AND revision=?", (source_id, revision))
        else:
            row = await self.db.fetch_one("SELECT payload_json FROM content_sources WHERE source_id=? AND active=1", (source_id,))
        if not row:
            raise KeyError("来源不存在或已删除")
        source = _load(row[0], {})
        live = await self.db.fetch_one("SELECT revision,active FROM content_sources WHERE source_id=?", (source_id,))
        return {**source, "source_deleted": not live or not live[1], "source_missing": not live or not live[1], "source_updated": bool(live and live[0] != source["revision"])}

    async def media_path(self, aweme_id: str) -> Optional[Path]:
        if self.resolve_media:
            result = self.resolve_media(aweme_id)
            result = await result if inspect.isawaitable(result) else result
            if result:
                path = Path(result)
                if path.is_file():
                    return path
            # 生产环境由主服务验证下载根目录，不能绕过它访问任意存储路径。
            return None
        rows = await self.db.fetch_all("SELECT file_path FROM task_videos WHERE aweme_id=? AND file_path IS NOT NULL UNION ALL SELECT local_path FROM distill_items WHERE aweme_id=? AND local_path IS NOT NULL", (aweme_id, aweme_id))
        for row in rows:
            if row[0] and Path(row[0]).is_file():
                return Path(row[0])
        return None

    async def get_video(self, aweme_id: str) -> dict:
        sources = [doc for doc in await self._sources() if doc["aweme_id"] == aweme_id]
        if not sources:
            raise KeyError("没有找到视频文字资料")
        # 相同内容仍保留不同来源引用，阅读界面可自行折叠。
        base = min(sources, key=lambda doc: doc.get("collected_at") or "")
        path = await self.media_path(aweme_id)
        reading = await self.get_reading(aweme_id)
        return {key: base.get(key) for key in ("aweme_id", "task_id", "title", "author", "cover", "source_url", "duration", "collected_at")} | {"sources": sources, "blocks": sources, "media_available": bool(path), "media_url": f"/api/content/videos/{aweme_id}/media" if path else None, "reading_state": reading, "reading": reading}

    async def get_task_video(self, task_id: str) -> dict:
        if "::" in task_id:
            base_task, aweme_id = task_id.rsplit("::", 1)
            row = await self.db.fetch_one("SELECT aweme_id FROM task_videos WHERE task_id=? AND aweme_id=?", (base_task, aweme_id))
        elif task_id.startswith("distill:"):
            parts = task_id.split(":")
            row = await self.db.fetch_one("SELECT aweme_id FROM distill_items WHERE run_id=? AND aweme_id=?", (parts[1], parts[2])) if len(parts) >= 3 else None
        else:
            row = await self.db.fetch_one("SELECT aweme_id FROM task_videos WHERE task_id=? ORDER BY id LIMIT 1", (task_id,))
        if not row:
            raise KeyError("没有找到对应视频")
        return await self.get_video(str(row[0]))

    @staticmethod
    def _matches(doc: dict, terms: list[str]) -> dict:
        text, positions = normalize_text(doc["text"])
        spans = []
        for term in terms:
            start = 0
            while (index := text.find(term, start)) >= 0:
                right = positions[index + len(term) - 1] + 1
                while right < len(doc["text"]) and unicodedata.category(doc["text"][right]).startswith("M"):
                    right += 1
                spans.append([positions[index], right])
                start = index + max(1, len(term))
        anchor = min((span[0] for span in spans), default=0)
        left, right = max(0, anchor - 45), min(len(doc["text"]), anchor + 150)
        snippet = doc["text"][left:right]
        return {**doc, "spans": sorted([[_utf16_offset(doc["text"], start), _utf16_offset(doc["text"], end)] for start, end in spans]), "snippet": snippet, "snippet_start": _utf16_offset(doc["text"], left), "snippet_spans": [[_utf16_offset(snippet, max(0, start-left)), _utf16_offset(snippet, min(right-left, end-left))] for start, end in spans if start < right and end > left], "offset_encoding": "utf-16"}

    async def search(self, query: str = "", author: str = "", types: str = "", sort: str = "relevance", page: int = 1, page_size: int = 20, scope: str = "videos", date_from: str = "", date_to: str = "") -> dict:
        terms = query_terms(query)
        docs = await self._sources()
        authors = sorted({doc["author"] for doc in docs})
        chosen = set(filter(None, types.split(",")))
        groups: dict[str, list[dict]] = {}
        for doc in docs:
            if (scope == "distill") != (doc["source_type"] == "distill"):
                continue
            if author and doc["author"] != author:
                continue
            if chosen and doc["source_type"] not in chosen:
                continue
            date = (doc.get("collected_at") or "")[:10]
            if (date_from and date < date_from) or (date_to and date > date_to):
                continue
            groups.setdefault(doc["aweme_id"], []).append(doc)
        items = []
        for aweme_id, sources in groups.items():
            normalized = [normalize_text(doc["text"])[0] for doc in sources]
            if not all(any(term in text for text in normalized) for term in terms):
                continue
            matches = [self._matches(doc, terms) for doc, text in zip(sources, normalized) if not terms or any(term in text for term in terms)]
            base = min(sources, key=lambda doc: doc.get("collected_at") or "")
            items.append({key: base.get(key) for key in ("aweme_id", "task_id", "title", "author", "cover", "source_url", "collected_at")} | {"matches": matches, "score": sum(len(item["spans"]) for item in matches), "content_types": sorted({item["source_type"] for item in matches})})
        items.sort(key=lambda item: ((item.get("collected_at") or "") if sort == "time" else item["score"], item.get("collected_at") or "", item["aweme_id"]), reverse=True)
        start = (max(1, page)-1) * page_size
        selected = items[start:start+page_size]
        for item in selected:
            # 搜索不探测媒体目录；打开阅读器时才由受控解析器确认文件。
            item["media_available"] = None
        return {"items": selected, "total": len(items), "page": page, "page_size": page_size, "authors": authors, "date_basis": "首次采集时间", "query_rules": "空格多词在同一视频内全部命中；双引号为连续短语；全半角和大小写规范化，标点按字面匹配"}

    async def get_reading(self, aweme_id: str) -> dict:
        row = await self.db.fetch_one("SELECT state,rewatch,updated_at FROM content_reading WHERE aweme_id=?", (aweme_id,))
        return {"state": row[0] if row else "pending", "rewatch": bool(row[1]) if row else False, "updated_at": row[2] if row else None}

    async def set_reading(self, aweme_id: str, data: dict) -> dict:
        old = await self.get_reading(aweme_id)
        state = data.get("state", old["state"])
        if state not in {"pending", "read"}:
            raise ValueError("阅读状态只能是待看或已看")
        await self.db.execute("INSERT INTO content_reading VALUES (?,?,?,?) ON CONFLICT(aweme_id) DO UPDATE SET state=excluded.state,rewatch=excluded.rewatch,updated_at=excluded.updated_at", (aweme_id, state, int(bool(data.get("rewatch", old["rewatch"]))), _now()))
        await self.db.commit()
        return await self.get_reading(aweme_id)

    async def _snapshot(self, data: dict) -> dict:
        if data.get("source_id"):
            source = await self.get_source(str(data["source_id"]), data.get("revision"))
            start_units = int(data.get("start", 0))
            end_units = int(data.get("end", _utf16_offset(source["text"], len(source["text"]))))
            start, end = _codepoint_offset(source["text"], start_units), _codepoint_offset(source["text"], end_units)
            if end <= start:
                raise ValueError("摘录范围无效，请重新选中文字")
            return {**source, "text": source["text"][start:end], "start": start_units, "end": end_units, "offset_encoding": "utf-16", "context_before": source["text"][max(0, start-80):start], "context_after": source["text"][end:end+80]}
        text = str(data.get("text") or "").strip()
        if not text:
            raise ValueError("请选择文字或填写个人笔记")
        if len(text) > 100_000:
            raise ValueError("单条笔记最多 10 万字符")
        return {"source_id": None, "revision": None, "source_type": "note", "source_label": SOURCE_LABELS["note"], "text": text, "title": str(data.get("title") or "个人笔记"), "author": "我", "aweme_id": None, "task_id": None, "source_url": None, "time_seconds": None, "time_basis": "none"}

    def _material(self, row: dict) -> dict:
        snapshot = _load(row["snapshot_json"], {})
        provenance = {key: snapshot.get(key) for key in ("source_id", "revision", "source_url", "time_seconds", "time_basis", "task_id")}
        return {**provenance, "id": row["material_id"], "material_id": row["material_id"], "snapshot": snapshot, "text": snapshot["text"], "excerpt": snapshot["text"], "source_type": snapshot["source_type"], "author": snapshot.get("author"), "aweme_id": snapshot.get("aweme_id"), "title": row["title"], "tags": _load(row["tags_json"], []), "notes": row["notes"], "rewrite": row["rewrite"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    async def get_material(self, material_id: str) -> dict:
        rows = await self._rows("SELECT * FROM content_materials WHERE material_id=?", (material_id,))
        if not rows:
            raise KeyError("素材不存在")
        material = self._material(rows[0])
        source_id = material["snapshot"].get("source_id")
        if source_id:
            live = await self.db.fetch_one("SELECT revision,active FROM content_sources WHERE source_id=?", (source_id,))
            material.update(source_deleted=not live or not live[1], source_missing=not live or not live[1], source_updated=bool(live and live[0] != material["snapshot"].get("revision")))
        return material

    async def list_materials(self, query: str = "", author: str = "", tag: str = "", source_type: str = "") -> dict:
        await self.refresh()
        items = [self._material(row) for row in await self._rows("SELECT * FROM content_materials ORDER BY created_at DESC")]
        authors = sorted({item.get("author") or "未知作者" for item in items})
        tags = sorted({tag for item in items for tag in item["tags"]})
        terms = query_terms(query)
        selected = [item for item in items if (not author or item.get("author") == author) and (not tag or tag in item["tags"]) and (not source_type or item["source_type"] == source_type) and all(term in normalize_text("\n".join([item["title"], item["text"], item["notes"], item["rewrite"], *item["tags"]]))[0] for term in terms)]
        live_sources = {row[0]: (row[1], row[2]) for row in await self.db.fetch_all("SELECT source_id,revision,active FROM content_sources")}
        for item in selected:
            if item.get("source_id"):
                live = live_sources.get(item["source_id"])
                item.update(source_missing=not live or not live[1], source_deleted=not live or not live[1], source_updated=bool(live and live[0] != item.get("revision")))
        return {"items": selected, "total": len(selected), "authors": authors, "tags": tags}

    @staticmethod
    def _tags(data: dict) -> list[str]:
        tags = data.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 50:
            raise ValueError("标签应为最多 50 项的列表")
        return list(dict.fromkeys(str(tag).strip()[:80] for tag in tags if str(tag).strip()))

    async def create_material(self, data: dict) -> dict:
        snapshot = await self._snapshot(data)
        duplicate_key = hashlib.sha256(_json([snapshot.get("source_id"), snapshot.get("aweme_id"), snapshot["source_type"], snapshot["text"], snapshot.get("start"), snapshot.get("end")]).encode()).hexdigest()
        async with self._write_lock:
            if data.get("request_id"):
                row = await self.db.fetch_one("SELECT material_id FROM content_materials WHERE request_id=?", (str(data["request_id"]),))
                if row:
                    return await self.get_material(row[0])
            duplicate = await self.db.fetch_one("SELECT material_id FROM content_materials WHERE duplicate_key=? ORDER BY created_at LIMIT 1", (duplicate_key,))
            if duplicate and not data.get("allow_duplicate"):
                return {"duplicate": True, "existing_id": duplicate[0], "existing": await self.get_material(duplicate[0]), "choices": ["打开已有素材修改备注", "另存一条"]}
            material_id, now = uuid.uuid4().hex, _now()
            await self.db.execute("INSERT INTO content_materials VALUES (?,?,?,?,?,?,?,?,?,?)", (material_id, data.get("request_id"), duplicate_key, _json(snapshot), str(data.get("title") or snapshot.get("title") or snapshot["text"][:30])[:300], _json(self._tags(data)), str(data.get("notes") or ""), str(data.get("rewrite") or ""), now, now))
            await self.db.commit()
        return await self.get_material(material_id)

    async def update_material(self, material_id: str, data: dict) -> dict:
        old = await self.get_material(material_id)
        if any(key in data for key in ("text", "snapshot", "source_id", "source_type", "revision")):
            raise ValueError("来源摘录不可覆盖；请在个人改写中保存新表达")
        await self.db.execute("UPDATE content_materials SET title=?,tags_json=?,notes=?,rewrite=?,updated_at=? WHERE material_id=?", (str(data.get("title", old["title"]))[:300], _json(self._tags(data)) if "tags" in data else _json(old["tags"]), str(data.get("notes", old["notes"])), str(data.get("rewrite", old["rewrite"])), _now(), material_id))
        await self.db.commit()
        return await self.get_material(material_id)

    async def delete_material(self, material_id: str) -> dict:
        await self.get_material(material_id)
        await self.db.execute("DELETE FROM content_materials WHERE material_id=?", (material_id,))
        await self.db.commit()
        return {"deleted": True, "topic_snapshots_retained": True}

    async def list_topics(self) -> dict:
        rows = await self._rows("SELECT t.*, (SELECT COUNT(*) FROM content_topic_members m WHERE m.topic_id=t.topic_id) AS member_count FROM content_topics t ORDER BY updated_at DESC")
        items = [{"id": row["topic_id"], "topic_id": row["topic_id"], "name": row["name"], "goal": row["goal"], "description": row["description"], "settings": _load(row["settings_json"], {}), "member_count": row["member_count"], "created_at": row["created_at"], "updated_at": row["updated_at"]} for row in rows]
        return {"items": items, "total": len(items)}

    async def create_topic(self, data: dict) -> dict:
        name = str(data.get("name") or "").strip()
        if not name or len(name) > 100:
            raise ValueError("专题名称应为 1–100 个字符")
        topic_id, now = uuid.uuid4().hex, _now()
        await self.db.execute("INSERT INTO content_topics VALUES (?,?,?,?,?,?,?)", (topic_id, name, str(data.get("goal") or ""), str(data.get("description") or ""), _json(data.get("settings") or {}), now, now))
        await self.db.commit()
        return await self.get_topic(topic_id)

    def _version(self, row: dict) -> dict:
        metadata = _load(row["metadata_json"], {})
        warnings = [str(metadata[key]) for key in ("uncited_notice",) if metadata.get(key)]
        return {"id": row["version_id"], "version_id": row["version_id"], "topic_id": row["topic_id"], "status": row["status"], "action": row["action"], "inputs": _load(row["inputs_json"], []), "settings": _load(row["settings_json"], {}), "output_markdown": row["output_markdown"], "edited_markdown": row["edited_markdown"], "metadata": metadata, "warnings": warnings, "error": row["error"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    async def get_topic(self, topic_id: str) -> dict:
        rows = await self._rows("SELECT * FROM content_topics WHERE topic_id=?", (topic_id,))
        if not rows:
            raise KeyError("专题不存在")
        row = rows[0]
        members = [{**_load(member["snapshot_json"], {}), "id": member["member_id"], "member_id": member["member_id"], "snapshot": _load(member["snapshot_json"], {}), "group_name": member["group_name"], "position": member["position"], "notes": member["notes"], "created_at": member["created_at"]} for member in await self._rows("SELECT * FROM content_topic_members WHERE topic_id=? ORDER BY position,created_at", (topic_id,))]
        versions = [self._version(version) for version in await self._rows("SELECT * FROM content_topic_versions WHERE topic_id=? ORDER BY created_at DESC", (topic_id,))]
        return {"id": topic_id, "topic_id": topic_id, "name": row["name"], "goal": row["goal"], "description": row["description"], "settings": _load(row["settings_json"], {}), "members": members, "versions": versions, "created_at": row["created_at"], "updated_at": row["updated_at"]}

    async def update_topic(self, topic_id: str, data: dict) -> dict:
        old = await self.get_topic(topic_id)
        name = str(data.get("name", old["name"])).strip()
        if not name or len(name) > 100:
            raise ValueError("专题名称应为 1–100 个字符")
        await self.db.execute("UPDATE content_topics SET name=?,goal=?,description=?,settings_json=?,updated_at=? WHERE topic_id=?", (name, str(data.get("goal", old["goal"])), str(data.get("description", old["description"])), _json(data.get("settings", old["settings"])), _now(), topic_id))
        await self.db.commit()
        return await self.get_topic(topic_id)

    async def delete_topic(self, topic_id: str) -> dict:
        await self.get_topic(topic_id)
        for table in ("content_topic_members", "content_topic_versions", "content_topics"):
            await self.db.execute(f"DELETE FROM {table} WHERE topic_id=?", (topic_id,))
        await self.db.commit()
        return {"deleted": True, "source_materials_retained": True}

    async def add_member(self, topic_id: str, data: dict) -> dict:
        topic = await self.get_topic(topic_id)
        if data.get("material_id"):
            material = await self.get_material(str(data["material_id"]))
            snapshot = {**material["snapshot"], "material_id": material["id"], "title": material["title"], "personal_notes": material["notes"], "rewrite": material["rewrite"]}
        elif data.get("aweme_id") and not data.get("source_id"):
            video = await self.get_video(str(data["aweme_id"]))
            snapshot = {key: value for key, value in video.items() if key in {"aweme_id", "title", "author", "source_url", "task_id"}} | {"source_type": "metadata", "text": "", "video_reference_only": True}
        else:
            snapshot = await self._snapshot(data)
        member_id = uuid.uuid4().hex
        await self.db.execute("INSERT INTO content_topic_members VALUES (?,?,?,?,?,?,?)", (member_id, topic_id, _json(snapshot), str(data.get("group_name") or ""), int(data.get("position", len(topic["members"]))), str(data.get("notes") or ""), _now()))
        await self.db.execute("UPDATE content_topics SET updated_at=? WHERE topic_id=?", (_now(), topic_id))
        await self.db.commit()
        return next(member for member in (await self.get_topic(topic_id))["members"] if member["id"] == member_id)

    async def update_member(self, topic_id: str, member_id: str, data: dict) -> dict:
        topic = await self.get_topic(topic_id)
        old = next((member for member in topic["members"] if member["id"] == member_id), None)
        if not old:
            raise KeyError("专题引用不存在")
        await self.db.execute("UPDATE content_topic_members SET group_name=?,position=?,notes=? WHERE member_id=? AND topic_id=?", (str(data.get("group_name", old["group_name"])), int(data.get("position", old["position"])), str(data.get("notes", old["notes"])), member_id, topic_id))
        await self.db.commit()
        return next(member for member in (await self.get_topic(topic_id))["members"] if member["id"] == member_id)

    async def remove_member(self, topic_id: str, member_id: str) -> dict:
        await self.db.execute("DELETE FROM content_topic_members WHERE topic_id=? AND member_id=?", (topic_id, member_id))
        await self.db.commit()
        return {"removed": True, "source_retained": True}

    async def preview_generation(self, topic_id: str, data: dict) -> dict:
        topic = await self.get_topic(topic_id)
        action = data.get("action", "outline")
        if action not in ACTIONS:
            raise ValueError("请选择支持的专题生成操作")
        selected = data.get("member_ids")
        if not isinstance(selected, list) or not selected:
            raise ValueError("请明确选择本次参与生成的资料")
        available = {member["id"]: member for member in topic["members"]}
        if any(member_id not in available for member_id in selected):
            raise ValueError("选中的资料已被移除，请重新选择")
        inputs = []
        for index, member_id in enumerate(dict.fromkeys(selected)):
            member = available[member_id]
            snapshot = member["snapshot"]
            if not snapshot.get("text"):
                raise ValueError("仅加入视频未选择文字；请先从来源全文摘录本次参与生成的内容")
            inputs.append({**snapshot, "citation_id": f"S{index+1}", "member_id": member_id, "group_name": member["group_name"], "topic_notes": member["notes"]})
        settings = {**topic["settings"], **(data.get("settings") or {})}
        characters = sum(len(_json(item)) for item in inputs) + len(_json(settings))
        if characters > 60_000:
            raise ValueError("所选资料超过单次 6 万字符上限，请减少资料范围；不会自动截断或分次付费调用")
        request_limit = data.get("request_limit")
        if request_limit is not None and (isinstance(request_limit, bool) or not isinstance(request_limit, int) or request_limit < 1):
            raise ValueError("调用上限应为正整数或不设置")
        return {"topic_id": topic_id, "topic_name": topic["name"], "goal": topic["goal"], "action": action, "action_label": ACTIONS[action], "settings": settings, "inputs": inputs, "characters": characters, "request_limit": request_limit, "external_knowledge": False, "notice": "仅向当前配置模型发送以上资料、个人备注和生成要求，不发送视频或整座素材库"}

    @staticmethod
    def _prompt(preview: dict) -> str:
        return "\n".join(["你是一个基于指定资料的中文写作助手。", f"操作：{preview['action_label']}；专题目标：{preview['goal']}", "仅使用下列资料。事实或观点归纳附 [S1] 形式的来源编号；逐字引用必须与对应资料完全一致。资料不足请说明。", "区分来源摘录、资料归纳和新创作。创作不冒充原作者原话，不编造用户经历、案例或外部事实。不要使用未提供的外部知识。", "返回 Markdown。", f"要求：{_json(preview['settings'])}", "资料：", *[f"[{item['citation_id']}] {SOURCE_LABELS.get(item['source_type'], item['source_type'])} / {item.get('title', '')}\n{item['text']}\n个人备注（非原话）：{item.get('topic_notes') or item.get('personal_notes') or ''}\n个人改写（非原话）：{item.get('rewrite') or ''}" for item in preview["inputs"]]])

    async def generate(self, topic_id: str, data: dict, *, _resume_version: Optional[dict] = None) -> dict:
        if self.generator is None:
            raise ValueError("专题生成接口尚未配置；已保存资料可继续编辑和导出")
        lock = self._generation_locks.setdefault(topic_id, asyncio.Lock())
        if lock.locked():
            raise ValueError("此专题已有生成任务，请等待当前结果")
        async with lock:
            if _resume_version:
                saved = _resume_version["metadata"]
                preview = {"topic_id": topic_id, "topic_name": saved.get("topic_name", "专题"), "goal": saved.get("goal", ""), "action": _resume_version["action"], "action_label": ACTIONS[_resume_version["action"]], "settings": _resume_version["settings"], "inputs": _resume_version["inputs"], "request_limit": None}
                version_id, now = _resume_version["id"], _now()
                await self.db.execute("UPDATE content_topic_versions SET status='running',error=NULL,updated_at=? WHERE version_id=?", (now, version_id))
            else:
                preview = await self.preview_generation(topic_id, data)
                version_id, now = uuid.uuid4().hex, _now()
                saved = {"goal": preview["goal"], "topic_name": preview["topic_name"], "template_version": 1}
                await self.db.execute("INSERT INTO content_topic_versions (version_id,topic_id,status,action,inputs_json,settings_json,metadata_json,created_at,updated_at) VALUES (?,?,'running',?,?,?,?,?,?)", (version_id, topic_id, preview["action"], _json(preview["inputs"]), _json(preview["settings"]), _json(saved), now, now))
            await self.db.execute("UPDATE content_topics SET settings_json=?,updated_at=? WHERE topic_id=?", (_json(preview["settings"]), now, topic_id))
            await self.db.commit()
            try:
                response = self.generator({**preview, "version_id": version_id, "preserve_limit": _resume_version is not None, "prompt": self._prompt(preview)})
                response = await response if inspect.isawaitable(response) else response
                output = response if isinstance(response, str) else response.get("output_markdown", "")
                if not isinstance(output, str) or not output.strip():
                    raise ValueError("模型未返回可用的专题正文")
                valid = {item["citation_id"] for item in preview["inputs"]}
                invented = set(re.findall(r"\[(S\d+)\]", output)) - valid
                if invented:
                    raise ValueError("生成结果包含不存在的来源编号，请主动重试")
                cited = set(re.findall(r"\[(S\d+)\]", output))
                # 不把提示词当成引文校验。返回独立审计结果，错误引文不标为已核实。
                quoted = re.findall(r'[“「]([^”」\n]{4,})[”」]\s*\[(S\d+)\]', output)
                source_texts = {item["citation_id"]: item["text"] for item in preview["inputs"]}
                invalid_quotes = [citation for quote, citation in quoted if quote not in source_texts.get(citation, "")]
                if invalid_quotes:
                    raise ValueError("生成结果的逐字引文与来源不一致，请主动重试")
                metadata = {**saved, **{key: response[key] for key in ("model", "provider", "request_id") if isinstance(response, dict) and key in response}}
                metadata.update({"citations_valid": True, "quote_check": "检测到的中文直引已比对；其他表述仍需人工核对", "cited_sources": sorted(cited), "uncited_notice": None if cited else "本版本未提供来源编号，归纳尚待核对", "template_version": 1})
                await self.db.execute("UPDATE content_topic_versions SET status='completed',output_markdown=?,metadata_json=?,updated_at=? WHERE version_id=?", (output, _json(metadata), _now(), version_id))
            except BaseException as exc:
                status = "paused" if isinstance(exc, ModelControlError) else "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed"
                # 避免将上游异常中的请求正文或敏感内容存入数据库。
                error = "生成被中断，已保留当次资料；可主动重试" if status == "interrupted" else "生成未完成，已保留当次资料；请检查模型配置或调用上限后重试"
                if isinstance(exc, ValueError):
                    error = str(exc)
                if isinstance(exc, ModelControlError):
                    error = str(exc)
                await self.db.execute("UPDATE content_topic_versions SET status=?,error=?,updated_at=? WHERE version_id=?", (status, error, _now(), version_id))
                await self.db.commit()
                if isinstance(exc, asyncio.CancelledError):
                    raise
            await self.db.commit()
            return next(version for version in (await self.get_topic(topic_id))["versions"] if version["id"] == version_id)

    async def resume_generation(self, topic_id: str, version_id: Optional[str] = None) -> dict:
        """恢复同一资料快照与执行编号，不复制版本或重新选择专题成员。"""
        topic = await self.get_topic(topic_id)
        version = next((item for item in topic["versions"] if item["status"] == "paused" and (version_id is None or item["id"] == version_id)), None)
        if not version:
            raise ValueError("此专题没有等待额度恢复的生成版本")
        return await self.generate(topic_id, {}, _resume_version=version)

    async def edit_version(self, topic_id: str, version_id: str, data: dict) -> dict:
        topic = await self.get_topic(topic_id)
        version = next((version for version in topic["versions"] if version["id"] == version_id), None)
        if not version:
            raise KeyError("生成版本不存在")
        if "edited_markdown" not in data:
            raise ValueError("请提供个人修改稿")
        await self.db.execute("UPDATE content_topic_versions SET edited_markdown=?,updated_at=? WHERE topic_id=? AND version_id=?", (str(data["edited_markdown"]), _now(), topic_id, version_id))
        await self.db.commit()
        return next(version for version in (await self.get_topic(topic_id))["versions"] if version["id"] == version_id)

    async def export_topic(self, topic_id: str, version_id: Optional[str] = None) -> tuple[str, str]:
        topic = await self.get_topic(topic_id)
        lines = [f"# {topic['name']}", "", f"目标：{topic['goal']}", "", topic["description"]]
        if version_id:
            version = next((version for version in topic["versions"] if version["id"] == version_id), None)
            if not version:
                raise KeyError("生成版本不存在")
            lines += ["", f"## {ACTIONS[version['action']]} · {version['created_at']}", "", version["edited_markdown"] if version["edited_markdown"] is not None else version["output_markdown"], "", "## 当次资料来源（文本快照）"]
            sources = version["inputs"]
        else:
            lines += ["", "## 专题资料（加入时快照）"]
            sources = [{**member["snapshot"], "citation_id": f"S{index+1}", "topic_notes": member["notes"]} for index, member in enumerate(topic["members"])]
        for source in sources:
            lines += ["", f"### [{source['citation_id']}] {source.get('title') or '资料'}", "", f"类型：{SOURCE_LABELS.get(source['source_type'], source['source_type'])}；作者：{source.get('author') or '未知'}", f"来源：{source.get('source_url') or '个人资料'}", f"来源版本：{source.get('revision') or '无'}", "", source.get("text") or "仅视频引用，未选择文字"]
            if source.get("topic_notes"):
                lines += ["", "个人备注：" + source["topic_notes"]]
            if source.get("rewrite"):
                lines += ["", "个人改写（非原话）：" + source["rewrite"]]
        filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", topic["name"])[:100] or "专题"
        return filename + ".md", "\n".join(lines)
