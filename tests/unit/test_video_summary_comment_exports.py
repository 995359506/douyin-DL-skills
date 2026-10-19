"""神评论候选导出回归，仅使用内存结果，不发起模型请求。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skills.douyin_downloader import web


def _job():
    return {
        "job_id": "summary-1",
        "task_id": "task-1",
        "status": "completed",
        "summary": {
            "title": "测试视频",
            "overview": "雨后散步的画面。",
            "comment_ideas": [
                {"style": "搞笑有梗", "text": "雨停了，我的班还没下。"},
                {"style": "抒情共鸣", "text": "走慢一点，风也在陪你回家。"},
                {"style": "金句", "text": "把日子走成自己的节奏。"},
                {"style": "金句", "text": {"意外结构": "不能导出为字典"}},
            ],
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["md", "txt", "json"])
async def test_summary_exports_include_generated_comment_ideas(monkeypatch, format):
    job = _job()
    db = SimpleNamespace(get_video_summary_job=AsyncMock(return_value=job))
    monkeypatch.setattr(web, "task_manager", SimpleNamespace(db=db))

    response = await web.export_video_summary("summary-1", format)
    content = response.body.decode("utf-8")

    if format == "json":
        assert json.loads(content)["comment_ideas"][0]["text"] == "雨停了，我的班还没下。"
    else:
        assert "## 神评论灵感" in content
        assert "AI 生成的评论候选" in content
        for item in job["summary"]["comment_ideas"][:3]:
            assert f"- {item['style']}：{item['text']}" in content
        assert "不能导出为字典" not in content


@pytest.mark.asyncio
async def test_author_markdown_keeps_comments_under_each_video(monkeypatch):
    job = _job()
    task = SimpleNamespace(
        id="task-1", video_name="测试视频", video_info={"nickname": "测试作者", "aweme_id": "12345"}
    )
    monkeypatch.setattr(web, "_get_author_downloaded_video_tasks", AsyncMock(return_value=[task]))
    monkeypatch.setattr(web, "get_video_summary", AsyncMock(return_value=SimpleNamespace(data=job)))
    monkeypatch.setattr(web, "task_manager", SimpleNamespace(db=SimpleNamespace(
        get_video_summary_job=AsyncMock(return_value=job)
    )))

    response = await web.export_author_summaries("测试作者")
    content = response.body.decode("utf-8")

    assert "# 测试作者 · 视频总结汇总" in content
    assert "## 测试视频" in content
    assert "### 神评论灵感" in content
    assert "金句：把日子走成自己的节奏。" in content


@pytest.mark.asyncio
@pytest.mark.parametrize("comments", [None, [], {"text": "旧的异常结构"}])
async def test_legacy_summary_export_does_not_invent_comments(monkeypatch, comments):
    job = _job()
    job["summary"]["comment_ideas"] = comments
    monkeypatch.setattr(web, "task_manager", SimpleNamespace(db=SimpleNamespace(
        get_video_summary_job=AsyncMock(return_value=job)
    )))

    response = await web.export_video_summary("summary-1", "md")
    content = response.body.decode("utf-8")

    assert "## 内容概览" in content
    assert "神评论灵感" not in content
