from types import SimpleNamespace
from urllib.parse import quote

import pytest

from skills.douyin_downloader import web


class _ResultDB:
    def __init__(self, transcript=None, summary=None, *, ordinary_transcript=None, ordinary_summary=None):
        self.transcript = transcript
        self.summary = summary
        self.ordinary_transcript = ordinary_transcript
        self.ordinary_summary = ordinary_summary

    async def get_transcript_job(self, job_id="", task_id=""):
        return self.ordinary_transcript

    async def get_video_summary_job(self, job_id="", task_id=""):
        return self.ordinary_summary

    async def get_latest_distill_result_for_task(self, task_id, result_type):
        return self.transcript if result_type == "transcript" else self.summary

    async def get_distill_item(self, run_id, aweme_id):
        item = self.transcript if aweme_id == "video-1" else self.summary
        return item if item and item.get("run_id") == run_id else None


def _distill_item():
    return {
        "run_id": "run-1",
        "aweme_id": "video-1",
        "task_id": "task-1",
        "original_transcript": "原始转写",
        "polished_text": "整理后的口播",
        "summary": {"title": "视频标题", "overview": "内容概览", "key_points": ["要点"]},
        "source_size": 10,
        "created_at": "2026-09-03T00:00:00",
        "updated_at": "2026-09-03T00:01:00",
    }


@pytest.mark.asyncio
async def test_get_endpoints_reuse_distill_snapshot_when_ordinary_job_failed(monkeypatch):
    item = _distill_item()
    db = _ResultDB(transcript=item, summary=item, ordinary_transcript={"job_id": "t", "task_id": "task-1", "status": "failed"}, ordinary_summary={"job_id": "s", "task_id": "task-1", "status": "failed"})
    monkeypatch.setattr(web, "task_manager", SimpleNamespace(db=db))

    transcript = await web.get_transcript("task-1")
    summary = await web.get_video_summary("task-1")

    assert transcript.data["status"] == "completed"
    assert transcript.data["source"] == "distill"
    assert summary.data["status"] == "completed"
    assert summary.data["source"] == "distill"


@pytest.mark.asyncio
async def test_distill_snapshot_exports_are_supported(monkeypatch):
    item = _distill_item()
    db = _ResultDB(transcript=item, summary=item)
    monkeypatch.setattr(web, "task_manager", SimpleNamespace(db=db))

    transcript_response = await web.export_transcript("distill:run-1:video-1:transcript", "txt")
    summary_response = await web.export_video_summary("distill:run-1:video-1:summary", "json")

    assert "整理后的口播" in transcript_response.body.decode("utf-8")
    assert "视频标题" in summary_response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_completed_transcript_exports_markdown_with_metadata_and_safe_filename(monkeypatch):
    job = {
        "job_id": "transcript-1",
        "task_id": "task-1",
        "status": "completed",
        "original_transcript": "原始转写内容",
        "polished_text": "整理后的口播内容",
    }

    class _DB(_ResultDB):
        async def get_transcript_job(self, job_id="", task_id=""):
            return job

    async def get_task(task_id):
        return SimpleNamespace(
            video_name="标题/带路径..的作品",
            url="https://www.douyin.com/video/123",
            video_info={"nickname": "作者", "aweme_id": "123"},
        )

    monkeypatch.setattr(web, "task_manager", SimpleNamespace(db=_DB(), get_task=get_task))

    response = await web.export_transcript("transcript-1", "md")
    content = response.body.decode("utf-8")

    assert response.media_type == "text/markdown; charset=utf-8"
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    assert "filename*=UTF-8''" + quote("标题_带路径_的作品-口播文案.md", safe="") in response.headers["content-disposition"]
    assert "# 标题/带路径..的作品 · 口播文案" in content
    assert "## 整理文案" in content
    assert "整理后的口播内容" in content
    assert "## 原始转写" in content
    assert "原始转写内容" in content
    assert "作者" in content
    assert "https://www.douyin.com/video/123" in content
    assert "/Users/" not in content


@pytest.mark.asyncio
async def test_distill_transcript_markdown_export_uses_snapshot_metadata(monkeypatch):
    item = _distill_item()
    item["metadata"] = {
        "desc": "蒸馏视频标题",
        "nickname": "蒸馏作者",
        "url": "https://www.douyin.com/video/video-1",
    }
    db = _ResultDB(transcript=item)
    monkeypatch.setattr(web, "task_manager", SimpleNamespace(db=db))

    response = await web.export_transcript("distill:run-1:video-1:transcript", "md")
    content = response.body.decode("utf-8")

    assert "# 蒸馏视频标题 · 口播文案" in content
    assert "- 作者：蒸馏作者" in content
    assert "- 作品 ID：video-1" in content
    assert "## 整理文案" in content
    assert "## 原始转写" in content


@pytest.mark.asyncio
async def test_transcript_markdown_export_rejects_missing_or_incomplete_jobs(monkeypatch):
    class _DB(_ResultDB):
        async def get_transcript_job(self, job_id="", task_id=""):
            return {"task_id": "task-1", "status": "running"} if job_id == "running" else None

    monkeypatch.setattr(web, "task_manager", SimpleNamespace(db=_DB()))

    with pytest.raises(web.HTTPException) as missing:
        await web.export_transcript("missing", "md")
    assert missing.value.status_code == 404

    with pytest.raises(web.HTTPException) as incomplete:
        await web.export_transcript("running", "md")
    assert incomplete.value.status_code == 404


@pytest.mark.asyncio
async def test_get_endpoint_keeps_inflight_ordinary_job_instead_of_using_snapshot(monkeypatch):
    item = _distill_item()
    ordinary = {"job_id": "t", "task_id": "task-1", "status": "running", "progress": 42}
    db = _ResultDB(transcript=item, ordinary_transcript=ordinary)
    monkeypatch.setattr(web, "task_manager", SimpleNamespace(db=db))

    response = await web.get_transcript("task-1")

    assert response.data == ordinary
