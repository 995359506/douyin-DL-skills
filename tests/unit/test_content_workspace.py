import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from skills.douyin_downloader.content_api import create_content_router
from skills.douyin_downloader.content_service import ContentService, normalize_time
from skills.douyin_downloader.db import TaskDB


@pytest_asyncio.fixture
async def workspace(tmp_path):
    db = TaskDB(str(tmp_path / "content.db"))
    await db.connect()
    await db.execute("INSERT INTO tasks (task_id,type,status,created_at) VALUES ('task1','single','completed','2026-01-02'),('task2','single','completed','2026-02-03')")
    await db.execute("INSERT INTO task_videos(task_id,aweme_id,desc,nickname,duration) VALUES ('task1','100','完全不同的标题','甲作者','120'),('task2','100','另一次记录','甲作者','120')")
    await db.execute("INSERT INTO transcript_jobs(job_id,task_id,status,original_transcript,polished_text,created_at,updated_at) VALUES ('transcript1','task1','completed','中文短语藏在口播。ＡＢＣ 的方法。','这是整理后的表达。','2026-01-02','2026-01-02')")
    summary = {"overview": "沟通需要耐心。", "key_points": ["中文短语可以核对"], "timeline": [{"time": 0, "visual": "开始沟通"}, {"time": "00:35", "audio": "耐心倾听"}], "comment_ideas": [{"text": "不该进入事实搜索的神评论"}]}
    await db.execute("INSERT INTO video_summary_jobs(job_id,task_id,status,summary_json,created_at,updated_at) VALUES ('summary1','task2','completed',?,'2026-02-03','2026-02-03')", (json.dumps(summary, ensure_ascii=False),))
    await db.commit()
    service = ContentService(db)
    await service.initialize()
    yield service, db
    await db.close()


@pytest.mark.asyncio
async def test_search_chinese_short_phrase_and_aggregation(workspace):
    service, _ = workspace
    result = await service.search('"中文短语" 沟通')
    assert result["total"] == 1
    assert result["items"][0]["aweme_id"] == "100"
    assert {match["source_type"] for match in result["items"][0]["matches"]} == {"original", "summary"}
    assert (await service.search("中文"))["total"] == 1
    assert (await service.search("abc 方法"))["total"] == 1
    assert (await service.search("不该进入事实搜索"))["total"] == 0
    assert (await service.search("没有这个内容"))["items"] == []


@pytest.mark.asyncio
async def test_source_spans_preserve_original_offsets_and_new_content(workspace):
    service, db = workspace
    match = (await service.search("abc"))["items"][0]["matches"][0]
    start, end = match["spans"][0]
    assert match["text"][start:end] == "ＡＢＣ"
    await db.execute("UPDATE transcript_jobs SET original_transcript='最新生成的内容' WHERE job_id='transcript1'")
    await db.commit()
    assert (await service.search("最新生成"))["total"] == 1
    assert (await service.search("abc"))["total"] == 0


@pytest.mark.asyncio
async def test_browser_utf16_offsets_keep_emoji_excerpt_correct(workspace):
    service, db = workspace
    await db.execute("UPDATE transcript_jobs SET original_transcript='🌟前言，保留这句话🙂。' WHERE job_id='transcript1'")
    await db.commit()
    match = (await service.search("保留这句话"))["items"][0]["matches"][0]
    assert match["spans"] == [[5, 10]]
    material = await service.create_material({"source_id": match["source_id"], "revision": match["revision"], "start": 5, "end": 10})
    assert material["excerpt"] == "保留这句话"
    with pytest.raises(ValueError, match="不能截断"):
        await service.create_material({"source_id": match["source_id"], "start": 1, "end": 3})


@pytest.mark.asyncio
async def test_combining_character_normalization_keeps_full_original_range(workspace):
    service, db = workspace
    await db.execute("UPDATE transcript_jobs SET original_transcript='Cafe\u0301 的想法' WHERE job_id='transcript1'")
    await db.commit()
    match = (await service.search("café"))["items"][0]["matches"][0]
    assert match["spans"] == [[0, 5]]
    material = await service.create_material({"source_id": match["source_id"], "start": 0, "end": 5})
    assert material["excerpt"] == "Cafe\u0301"
    with pytest.raises(ValueError, match="不能截断"):
        await service.create_material({"source_id": match["source_id"], "start": 0, "end": 4})


@pytest.mark.asyncio
async def test_distill_independent_sources_and_synthesis_scope(workspace):
    service, db = workspace
    await db.create_distill_run("run1", sec_user_id="sec", nickname="独立作者", user_url="u")
    await db.add_distill_items("run1", [{"ordinal": 1, "aweme_id": "200", "metadata": {"title": "独立视频"}}])
    await db.execute("UPDATE distill_items SET original_transcript='仅蒸馏保存的口播',summary_json=? WHERE run_id='run1'", (json.dumps({"overview": "独立总结"}),))
    await db.execute("UPDATE distill_runs SET synthesis_json=? WHERE run_id='run1'", (json.dumps({"one_sentence": "跨视频的博主归纳"}),))
    await db.commit()
    result = await service.search("仅蒸馏")
    assert result["total"] == 1 and result["items"][0]["aweme_id"] == "200"
    assert (await service.search("跨视频"))["total"] == 0
    assert (await service.search("跨视频", scope="distill"))["total"] == 1
    before = (await db.fetch_one("SELECT COUNT(*) FROM tasks"))[0]
    video = await service.get_task_video("distill:run1:200:transcript")
    assert video["aweme_id"] == "200"
    assert (await db.fetch_one("SELECT COUNT(*) FROM tasks"))[0] == before


@pytest.mark.asyncio
async def test_immutable_material_survives_regeneration_and_task_deletion(workspace):
    service, db = workspace
    source = (await service.search("藏在口播"))["items"][0]["matches"][0]
    material = await service.create_material({"source_id": source["source_id"], "revision": source["revision"], "start": 0, "end": 4, "tags": ["表达"], "notes": "我自己的理解"})
    await db.execute("UPDATE transcript_jobs SET original_transcript='已重新生成' WHERE job_id='transcript1'")
    await db.commit()
    await service.refresh()
    original = await service.get_material(material["id"])
    assert original["excerpt"] == "中文短语" and original["source_updated"]
    await db.delete_task("task1")
    await service.refresh()
    saved = await service.get_material(material["id"])
    assert saved["excerpt"] == "中文短语" and saved["source_deleted"]
    archived = await service.get_source(source["source_id"], source["revision"])
    assert archived["text"] == source["text"] and archived["source_deleted"]
    with pytest.raises(ValueError, match="不可覆盖"):
        await service.update_material(material["id"], {"text": "悄悄改写原文"})
    rewritten = await service.update_material(material["id"], {"rewrite": "这是个人改写"})
    assert rewritten["excerpt"] == "中文短语" and rewritten["rewrite"] == "这是个人改写"


@pytest.mark.asyncio
async def test_duplicate_material_idempotency_and_shared_topics(workspace):
    service, _ = workspace
    material = await service.create_material({"text": "我的个人笔记", "request_id": "once"})
    repeated = await service.create_material({"text": "我的个人笔记", "request_id": "once"})
    assert repeated["id"] == material["id"]
    duplicate = await service.create_material({"text": "我的个人笔记"})
    assert duplicate["duplicate"] and duplicate["existing_id"] == material["id"]
    topics = [await service.create_topic({"name": name}) for name in ("婚礼口播", "沟通表达")]
    members = [await service.add_member(topic["id"], {"material_id": material["id"]}) for topic in topics]
    await service.remove_member(topics[0]["id"], members[0]["id"])
    assert len((await service.get_topic(topics[1]["id"]))["members"]) == 1
    assert (await service.get_material(material["id"]))["text"] == "我的个人笔记"
    await service.delete_material(material["id"])
    assert (await service.get_topic(topics[1]["id"]))["members"][0]["snapshot"]["text"] == "我的个人笔记"


@pytest.mark.asyncio
async def test_generation_exact_scope_versions_and_markdown(workspace):
    service, _ = workspace
    calls = []

    async def generate(payload):
        calls.append(payload)
        return {"output_markdown": "## 归纳\n可复用结构 [S1]", "model": "offline-test"}

    service.generator = generate
    topic = await service.create_topic({"name": "写作/测试", "goal": "根据资料写作"})
    one = await service.add_member(topic["id"], {"text": "第一个资料"})
    await service.add_member(topic["id"], {"text": "不应该发送的第二个资料"})
    request = {"member_ids": [one["id"]], "action": "outline", "settings": {"tone": "自然"}, "request_limit": 5}
    preview = await service.preview_generation(topic["id"], request)
    assert len(preview["inputs"]) == 1
    first = await service.generate(topic["id"], request)
    assert first["status"] == "completed" and first["inputs"][0]["text"] == "第一个资料"
    assert "不应该发送" not in calls[0]["prompt"]
    assert calls[0]["request_limit"] == 5
    await service.edit_version(topic["id"], first["id"], {"edited_markdown": "用户修改版本"})
    second = await service.generate(topic["id"], request)
    assert first["id"] != second["id"]
    versions = (await service.get_topic(topic["id"]))["versions"]
    assert next(version for version in versions if version["id"] == first["id"])["edited_markdown"] == "用户修改版本"
    filename, markdown = await service.export_topic(topic["id"], first["id"])
    assert filename == "写作_测试.md"
    assert "用户修改版本" in markdown and "第一个资料" in markdown and "[S1]" in markdown


@pytest.mark.asyncio
async def test_generation_rejects_missing_selection_video_only_and_bad_citations(workspace):
    service, _ = workspace
    topic = await service.create_topic({"name": "测试"})
    video = await service.add_member(topic["id"], {"aweme_id": "100"})
    with pytest.raises(ValueError, match="明确选择"):
        await service.preview_generation(topic["id"], {"member_ids": []})
    with pytest.raises(ValueError, match="未选择文字"):
        await service.preview_generation(topic["id"], {"member_ids": [video["id"]]})
    note = await service.add_member(topic["id"], {"text": "真实资料句子"})

    async def invalid(_):
        return "错误来源 [S99]"

    service.generator = invalid
    result = await service.generate(topic["id"], {"member_ids": [note["id"]]})
    assert result["status"] == "failed" and "不存在的来源" in result["error"]
    assert result["inputs"][0]["text"] == "真实资料句子"


@pytest.mark.parametrize(("value", "expected"), [(0, 0), ("0", 0), ("01:02", 62), ("01:01:02", 3662), ("00:35-00:45", 35), ("约 00:35～00:45", 35), ("00:99", None), (-1, None), ("NaN", None), (True, None)])
def test_time_normalization(value, expected):
    assert normalize_time(value) == expected
    assert normalize_time(130, 120) is None


@pytest.mark.asyncio
async def test_readonly_reader_zero_timestamp_missing_media_and_safe_resolver(workspace, tmp_path):
    service, db = workspace
    video = await service.get_task_video("task2::100")
    timeline = [source for source in video["sources"] if source["field_path"].startswith("timeline")]
    assert any(source["time_seconds"] == 0 for source in timeline)
    assert video["media_available"] is False and video["media_url"] is None
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"offline fixture")
    await db.execute("UPDATE task_videos SET file_path=? WHERE task_id='task1'", (str(outside),))
    await db.commit()
    service.resolve_media = lambda _: None
    assert await service.media_path("100") is None
    app = FastAPI()
    app.include_router(create_content_router(lambda: service))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/content/search", params={"q": "中文"})
        assert response.status_code == 200 and response.json()["data"]["total"] == 1
        assert (await client.get("/api/content/videos/100/media")).status_code == 404
        assert (await client.get("/api/content/videos/100")).status_code == 200
        response = await client.put("/api/content/videos/100/reading", json={"state": "read", "rewatch": True})
        assert response.json()["data"]["rewatch"] is True
