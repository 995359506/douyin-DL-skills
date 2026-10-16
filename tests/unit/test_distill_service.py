import asyncio
import zipfile
from pathlib import Path

import pytest

from skills.douyin_downloader.db import TaskDB
from skills.douyin_downloader.distill_service import DistillError, DistillMarkdown, DistillService, DistillSynthesis, _low_signal_reason, _summary_invalid_reason


def _valid_summary():
    return {
        "title": "测试画面", "overview": "画面先交代主题，再展示过程，最后给出结论。",
        "key_points": ["讲述主题"], "timeline": [{"time": 1, "visual": "展示画面"}],
        "visual_content": ["人物展示画面"],
        "narrative_structure": {"hook": "开场提问", "setup": "说明背景", "ending": "最后总结"},
        "reusable_elements": ["开场提问与结尾呼应"],
    }


def test_summary_validity_rejects_empty_shape_and_allows_zero_language_threshold():
    assert "结构不足" in _summary_invalid_reason({"title": "视频总结", "overview": "一整段退化文本"})
    assert _summary_invalid_reason(_valid_summary()) == ""
    english = {**_valid_summary(), "title": "English", "overview": "English text only", "key_points": ["English"], "visual_content": ["English"]}
    assert _summary_invalid_reason(english, 0) == ""


def test_techniques_validate_sources_deduplicate_and_render(tmp_path: Path):
    items = [{"ordinal": 1, "aweme_id": "1", "item_status": "ready", "metadata": {"title": "作品"}}]
    data = {"one_sentence": "画像", "themes": [], "methods": [], "contradictions": [], "expressions": [], "techniques": {
        "hooks": [{"pattern": "从【困境】提出【问题】", "note": "具体开场", "sources": ["001"]}, {"pattern": "从【困境】提出【问题】", "note": "重复", "sources": ["001"]}],
        "endings": [{"pattern": "以【结论】回应【开场】", "note": "首尾呼应", "sources": ["001"]}],
        "pacing": [{"pattern": "无来源", "sources": ["999"]}],
    }}
    validated = DistillSynthesis._validate(data, items)
    assert len(validated["techniques"]["hooks"]) == 1
    assert len(validated["techniques"]["endings"]) == 1
    assert validated["_audit"]["techniques"]["discarded"]["sources_mismatch"] == 1
    markdown = DistillMarkdown(tmp_path)._outline({"nickname": "博主", "synthesis": validated}, items, False)
    assert "## 可复用手法库" in markdown
    assert "从【困境】提出【问题】（来源：001）" in markdown
    assert "本次资料中未归纳出可复用骨架" in markdown


@pytest.mark.asyncio
async def test_image_text_uses_desc_without_media_callbacks(tmp_path: Path):
    db = TaskDB(str(tmp_path / "image.db"))
    await db.connect()
    await db.create_distill_run("image-run", sec_user_id="sec", nickname="博主", user_url="u")
    await db.add_distill_items("image-run", [{"ordinal": 1, "aweme_id": "1", "metadata": {"kind": "image", "title": "作品", "body": "图文中的完整正文"}}])

    async def forbidden(*_args):
        raise AssertionError("图文不应调用媒体处理")

    class Synthesis:
        async def run(self, items):
            assert items[0]["polished_text"] == "图文中的完整正文"
            return {"one_sentence": "画像", "themes": [], "methods": [], "contradictions": [], "expressions": []}

    service = DistillService(db, forbidden, forbidden, forbidden, Synthesis(), tmp_path)
    await service.run("image-run")
    item = (await db.get_distill_items("image-run"))[0]
    assert item["item_status"] == "written"
    assert item["original_transcript"] == "图文中的完整正文"
    run = await db.get_distill_run("image-run")
    assert "kind=image_text" in Path(run["output_path"]).read_text(encoding="utf-8") or "kind=image_text" in next((Path(run["output_path"]).parent / "口播文案").glob("*.md")).read_text(encoding="utf-8")
    await db.close()


@pytest.mark.asyncio
async def test_preview_then_full_reuses_transcript(tmp_path: Path):
    db = TaskDB(str(tmp_path / "preview.db"))
    await db.connect()
    await db.create_distill_run("preview-run", sec_user_id="sec", nickname="博主", user_url="u", options={"stage_policy": "preview"})
    await db.add_distill_items("preview-run", [{"ordinal": 1, "aweme_id": "1", "metadata": {"kind": "video", "title": "视频"}}])
    video = tmp_path / "preview.mp4"
    video.write_bytes(b"mock")
    calls = {"transcript": 0, "summary": 0}

    async def transcribe(*_args):
        calls["transcript"] += 1
        return {"original_transcript": "文案" * 75, "polished_text": "文案" * 75}

    async def summarize(*_args):
        calls["summary"] += 1
        return _valid_summary()

    class Synthesis:
        async def run(self, items):
            assert len(items) == 1
            return {"one_sentence": "画像", "themes": [], "methods": [], "contradictions": [], "expressions": []}

    service = DistillService(db, lambda _item: asyncio.sleep(0, result=video), transcribe, summarize, Synthesis(), tmp_path)
    await service.run("preview-run")
    assert (await db.get_distill_run("preview-run"))["status"] == "preview_ready"
    assert (await db.get_distill_item("preview-run", "1"))["item_status"] == "transcribed"
    assert calls == {"transcript": 1, "summary": 0}

    await service.resume("preview-run", ["1"], "full")
    await service.active["preview-run"]
    assert calls == {"transcript": 1, "summary": 1}
    assert (await db.get_distill_run("preview-run"))["status"] == "completed"
    await db.close()


def test_low_signal_threshold_boundary_and_disable():
    short = {"polished_text": "字" * 58}
    boundary = {"polished_text": "字" * 150}
    assert "58 字" in _low_signal_reason(short, 150)
    assert _low_signal_reason(boundary, 150) == ""
    assert _low_signal_reason(short, 0) == ""


@pytest.mark.asyncio
async def test_low_signal_is_omitted_from_synthesis_but_keeps_appendix(tmp_path: Path):
    db = TaskDB(str(tmp_path / "low-signal.db"))
    await db.connect()
    await db.create_distill_run("low-signal", sec_user_id="sec", nickname="博主", user_url="u")
    await db.add_distill_items("low-signal", [{"ordinal": index, "aweme_id": str(index), "metadata": {"kind": "video", "title": f"作品{index}"}} for index in (1, 2)])
    video = tmp_path / "video.mp4"
    video.write_bytes(b"mock")

    async def transcribe(job_id, _path, _progress):
        text = "字" * (58 if job_id.endswith(":1") else 150)
        return {"original_transcript": text, "polished_text": text}

    class Synthesis:
        async def run(self, items):
            assert [item["aweme_id"] for item in items] == ["2"]
            return {"one_sentence": "画像", "themes": [], "methods": [], "contradictions": [], "expressions": []}

    service = DistillService(db, lambda _item: asyncio.sleep(0, result=video), transcribe, lambda *_: asyncio.sleep(0, result=_valid_summary()), Synthesis(), tmp_path)
    service.retry_delays = ()
    service.schedule("low-signal")
    await service.active["low-signal"]
    run = await db.get_distill_run("low-signal")
    items = await db.get_distill_items("low-signal")
    assert items[0]["item_status"] == "low_signal"
    assert items[0]["local_path"] == str(video)
    assert "口播信息量不足" in items[0]["degrade_reason"]
    assert "001" in Path(run["output_path"]).read_text(encoding="utf-8")
    assert "口播信息量不足" in Path(run["output_path"]).read_text(encoding="utf-8")
    assert len(list((Path(run["output_path"]).parent / "口播文案").glob("*.md"))) == 2
    await db.close()


def test_synthesis_expression_validation_and_audit():
    items = [
        {"ordinal": 1, "polished_text": "先观察，再行动。", "original_transcript": ""},
        {"ordinal": 2, "polished_text": "把选择权留给自己！", "original_transcript": ""},
    ]
    data = {"themes": [], "methods": [], "contradictions": [], "one_sentence": {"text": "先观察再行动", "sources": ["001"]}, "expressions": [
        {"quote": "先观察，再行动。", "note": "动作建议", "sources": ["001"]},
        {"quote": "把选择权留给自己!", "note": "态度表达", "sources": ["002"]},
        {"quote": "原文没有的句子", "note": "不应保留", "sources": ["002"]},
        {"note": "缺少 quote", "sources": ["001"]},
        {"quote": "先观察，再行动。", "sources": ["不存在"]},
        {"quote": "……", "sources": ["001"]},
    ]}
    result = DistillSynthesis._validate(data, items)
    assert len(result["expressions"]) == 2
    assert result["_audit"]["expressions"] == {"returned": 6, "kept": 2, "discarded": {"missing_quote": 1, "sources_mismatch": 1, "not_in_source": 2}}


@pytest.mark.parametrize("value,expected", [
    ({"text": "画像一", "sources": ["001"]}, "画像一"),
    ({"summary": "画像二"}, "画像二"),
    ({"content": "画像三"}, "画像三"),
    ("纯文本画像", "纯文本画像"),
    (None, "未生成跨视频画像。"),
])
def test_one_sentence_legacy_shapes_render_cleanly(tmp_path: Path, value, expected):
    renderer = DistillMarkdown(tmp_path)
    run = {"nickname": "博主", "synthesis": {"one_sentence": value}}
    outline = renderer._outline(run, [], True)
    assert expected in outline
    assert "{'" not in outline
    assert "'}" not in outline


def test_one_sentence_unknown_source_is_audited():
    result = DistillSynthesis._validate({"themes": [], "methods": [], "contradictions": [], "expressions": [], "one_sentence": {"text": "画像", "sources": ["未知"]}}, [{"ordinal": 1}])
    assert result["one_sentence"] == {"text": "画像", "sources": []}
    assert result["_audit"]["one_sentence"]["sources_unmatched"] == 1


@pytest.mark.asyncio
async def test_consecutive_download_403_pauses_run_without_starting_remaining_items(tmp_path: Path):
    db = TaskDB(str(tmp_path / "circuit.db"))
    await db.connect()
    await db.create_distill_run("circuit", sec_user_id="sec", nickname="博主", user_url="u")
    await db.add_distill_items("circuit", [{"ordinal": index, "aweme_id": str(index), "metadata": {"kind": "video"}} for index in range(1, 7)])
    calls = []

    async def download(item):
        calls.append(item["aweme_id"])
        raise DistillError("DISTILL_DOWNLOAD_403", "HTTP 403", "重新扫码登录后继续")

    service = DistillService(db, download, lambda *_: asyncio.sleep(0), lambda *_: asyncio.sleep(0), object(), tmp_path)
    service.download_limit = asyncio.Semaphore(1)
    service.download_circuit_threshold = 3
    service.retry_delays = ()
    service.schedule("circuit")
    await service.active["circuit"]
    run = await db.get_distill_run("circuit")
    assert run["status"] == "paused"
    assert "重新扫码" in run["error"]["action"]
    assert len(calls) == 3
    assert run["failed_count"] == 3
    assert run["pending_count"] == 3
    await db.close()


@pytest.mark.asyncio
async def test_download_success_resets_403_streak_and_single_403_does_not_pause(tmp_path: Path):
    db = TaskDB(str(tmp_path / "reset.db"))
    await db.connect()
    await db.create_distill_run("reset", sec_user_id="sec", nickname="博主", user_url="u")
    await db.add_distill_items("reset", [{"ordinal": index, "aweme_id": str(index), "metadata": {"kind": "video"}} for index in range(1, 5)])
    video = tmp_path / "video.mp4"
    video.write_bytes(b"mock")

    async def download(item):
        if item["aweme_id"] != "2":
            raise DistillError("DISTILL_DOWNLOAD_403", "HTTP 403", "降低并发后继续")
        return video

    class Synthesis:
        async def run(self, _items):
            return {"one_sentence": "画像", "themes": [], "methods": [], "contradictions": [], "expressions": []}

    service = DistillService(db, download, lambda *_: asyncio.sleep(0, result={"original_transcript": "文案" * 75, "polished_text": "文案" * 75}), lambda *_: asyncio.sleep(0, result=_valid_summary()), Synthesis(), tmp_path)
    service.download_limit = asyncio.Semaphore(1)
    service.download_circuit_threshold = 3
    service.retry_delays = ()
    service.schedule("reset")
    await service.active["reset"]
    run = await db.get_distill_run("reset")
    assert run["status"] == "partial"
    assert run["failed_count"] == 3
    assert run["completed_count"] == 1
    await db.close()


def test_bad_synthesis_json_is_retryable():
    with pytest.raises(DistillError) as error:
        DistillSynthesis._parse('{"themes": [')
    assert error.value.code == "DISTILL_SYNTHESIS_BAD_JSON"
    assert error.value.retryable


@pytest.mark.asyncio
async def test_bad_synthesis_json_retries_and_failure_audits(tmp_path: Path):
    db = TaskDB(str(tmp_path / "bad-json.db"))
    await db.connect()
    await db.create_distill_run("run-bad", sec_user_id="sec", nickname="博主", user_url="u")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"mock")
    await db.add_distill_items("run-bad", [{"ordinal": 1, "aweme_id": "1", "metadata": {"kind": "video"}, "local_path": str(video), "source_size": video.stat().st_size, "source_mtime_ns": video.stat().st_mtime_ns, "polished_text": "原句", "original_transcript": "原句", "summary": {}, "item_status": "ready"}])

    class BadJsonSynthesis(DistillSynthesis):
        def __init__(self):
            super().__init__(base_url="https://example.test/v1", api_key="fake-secret")
            self.calls = 0

        async def _chat(self, _prompt):
            self.calls += 1
            return '{"themes": ['

    synthesis = BadJsonSynthesis()
    service = DistillService(db, lambda _item: asyncio.sleep(0, result=video), lambda *_args: asyncio.sleep(0, result={"polished_text": "原句"}), lambda *_args: asyncio.sleep(0, result={}), synthesis, tmp_path)
    service.retry_delays = (0,)
    service.schedule("run-bad")
    await service.active["run-bad"]
    run = await db.get_distill_run("run-bad")
    assert synthesis.calls == 2
    assert run["status"] == "failed"
    assert run["synthesis_audit"]["failure"]["code"] == "DISTILL_SYNTHESIS_BAD_JSON"
    assert "fake-secret" not in str(run["synthesis_audit"])
    assert "example.test" not in str(run["synthesis_response_meta"])
    await db.close()


def test_distill_markdown_always_writes_one_file_per_video_and_removes_topics(tmp_path: Path):
    renderer = DistillMarkdown(tmp_path, split_threshold=999999)
    run = {"run_id": "run-markdown", "nickname": "测试博主", "sec_user_id": "sec123456", "total_count": 1, "synthesis_model": "gpt-5.6-luna", "synthesis": {}}
    items = [{
        "ordinal": 1,
        "aweme_id": "123456789",
        "item_status": "ready",
        "metadata": {"title": "春天去旅行 #旅行 #日常", "kind": "video", "url": "https://www.douyin.com/video/123456789"},
        "polished_text": "这是一段文案",
        "original_transcript": "这是一段原始转写",
    }]
    artifact = renderer.render(run, items)
    output = Path(artifact["path"])
    item_files = list((output.parent / "口播文案").glob("*.md"))
    assert artifact["split"] == "1"
    assert len(item_files) == 1
    assert "#旅行" not in output.read_text(encoding="utf-8")
    assert "#日常" not in item_files[0].read_text(encoding="utf-8")
    assert "春天去旅行" in item_files[0].name
    assert "/api/distill-runs/run-markdown/items/123456789/view" in output.read_text(encoding="utf-8")


def test_distill_markdown_package_does_not_include_itself_or_previous_archives(tmp_path: Path):
    renderer = DistillMarkdown(tmp_path)
    run = {"nickname": "博主", "sec_user_id": "sec123456", "total_count": 1, "synthesis": {}}
    item = {"ordinal": 1, "aweme_id": "1", "item_status": "ready", "metadata": {"title": "视频"}}
    output = Path(renderer.render(run, [item])["path"])
    old_zip = output.parent / "旧-完整包.zip"
    old_zip.write_bytes(b"old")
    first = DistillMarkdown.package(output.parent, output)
    second = DistillMarkdown.package(output.parent, output)
    assert first == second
    with zipfile.ZipFile(second) as archive:
        names = archive.namelist()
        assert all(not name.endswith(".zip") for name in names)
        assert output.name in names
        assert "口播文案/001-视频-1.md" in names


@pytest.mark.asyncio
async def test_distill_skips_image_and_writes_partial_markdown(tmp_path: Path):
    db = TaskDB(str(tmp_path / "distill.db"))
    await db.connect()
    await db.create_distill_run("run-1", sec_user_id="sec", nickname="测试博主", user_url="https://www.douyin.com/user/sec", synthesis_model="gpt-5.6-luna")
    await db.add_distill_items("run-1", [
        {"ordinal": 1, "aweme_id": "100001", "metadata": {"kind": "image", "title": "图文"}},
        {"ordinal": 2, "aweme_id": "100002", "metadata": {"kind": "video", "title": "视频"}},
    ])
    video = tmp_path / "100002.mp4"
    video.write_bytes(b"mock video")
    await db.update_distill_item("run-1", "100002", local_path=str(video), source_size=video.stat().st_size, source_mtime_ns=video.stat().st_mtime_ns)

    async def transcribe(_job, _path, _progress):
        raise RuntimeError("mock transcript failure")

    async def summarize(_job, _path, _progress):
        return _valid_summary()

    class FakeSynthesis:
        async def run(self, items):
            assert len(items) == 1
            return {"one_sentence": "测试", "themes": [], "methods": [], "contradictions": [], "expressions": []}

    service = DistillService(db, lambda _item: asyncio.sleep(0, result=video), transcribe, summarize, FakeSynthesis(), tmp_path)
    service.retry_delays = (0, 0, 0)
    service.schedule("run-1")
    await service.active["run-1"]

    run = await db.get_distill_run("run-1")
    items = await db.get_distill_items("run-1", limit=10)
    assert run and run["status"] == "completed"
    assert run["completed_count"] == 0
    assert run["degraded_count"] == 1
    assert run["skipped_count"] == 1
    assert items[0]["item_status"] == "skipped"
    assert items[1]["item_status"] == "degraded"
    assert items[1]["summary"] == _valid_summary()
    assert Path(run["output_path"]).exists()
    await db.close()


@pytest.mark.asyncio
async def test_distill_resume_resets_failed_items(tmp_path: Path):
    db = TaskDB(str(tmp_path / "resume.db"))
    await db.connect()
    await db.create_distill_run("run-2", sec_user_id="sec", nickname="博主", user_url="u")
    await db.add_distill_items("run-2", [{"ordinal": 1, "aweme_id": "100003", "metadata": {"kind": "video"}, "item_status": "failed"}])

    async def noop(*_args):
        return {"original_transcript": "原文", "polished_text": "整理"}

    async def summary(*_args):
        return {"key_points": []}

    class FakeSynthesis:
        async def run(self, _items):
            return {"one_sentence": "", "themes": [], "methods": [], "contradictions": [], "expressions": []}

    service = DistillService(db, lambda _item: asyncio.sleep(0, result=tmp_path / "missing.mp4"), noop, summary, FakeSynthesis(), tmp_path)
    service.retry_delays = (0, 0, 0)
    await service.resume("run-2", ["100003"])
    assert service.active["run-2"]
    await service.cancel("run-2")
    item = await db.get_distill_item("run-2", "100003")
    assert item["item_status"] in {"queued", "cancelled", "failed"}
    await db.close()


@pytest.mark.asyncio
async def test_distill_synthesis_retry_is_run_level_and_does_not_require_aweme_id(tmp_path: Path):
    db = TaskDB(str(tmp_path / "synthesis-retry.db"))
    await db.connect()
    await db.create_distill_run("run-retry", sec_user_id="sec", nickname="博主", user_url="u")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"mock")
    await db.add_distill_items("run-retry", [{
        "ordinal": 1, "aweme_id": "retry-1", "metadata": {"kind": "video", "title": "视频"},
        "local_path": str(video), "source_size": video.stat().st_size, "source_mtime_ns": video.stat().st_mtime_ns,
        "original_transcript": "原文", "polished_text": "整理", "summary": {"key_points": ["要点"]},
        "item_status": "ready", "download_status": "reused", "transcript_status": "reused", "summary_status": "reused",
    }])

    class RetrySynthesis:
        def __init__(self):
            self.calls = 0

        async def run(self, _items):
            self.calls += 1
            if self.calls == 1:
                from skills.douyin_downloader.distill_service import DistillError
                raise DistillError("SYNTHESIS_429", "限流", "稍后重试", True)
            return {"one_sentence": "成功", "themes": [], "methods": [], "contradictions": [], "expressions": []}

    synthesis = RetrySynthesis()
    service = DistillService(
        db,
        lambda _item: asyncio.sleep(0, result=video),
        lambda *_args: asyncio.sleep(0, result={"original_transcript": "原文" * 75, "polished_text": "整理" * 75}),
        lambda *_args: asyncio.sleep(0, result=_valid_summary()),
        synthesis,
        tmp_path,
    )
    service.retry_delays = (0, 0, 0)
    service.schedule("run-retry")
    await service.active["run-retry"]
    run = await db.get_distill_run("run-retry")
    assert synthesis.calls == 2
    assert run and run["status"] == "completed"
    await db.close()


@pytest.mark.asyncio
async def test_distill_uses_run_save_path_for_download_and_markdown(tmp_path: Path):
    db = TaskDB(str(tmp_path / "save-path.db"))
    await db.connect()
    custom = tmp_path / "default" / "custom"
    await db.create_distill_run("run-path", sec_user_id="sec", nickname="博主", user_url="u", options={"save_path": str(custom)})

    seen = {}

    async def download(item):
        seen["root"] = item["_save_root"]
        custom.mkdir(parents=True, exist_ok=True)
        path = custom / "video.mp4"
        path.write_bytes(b"mock")
        return path

    await db.add_distill_items("run-path", [{"ordinal": 1, "aweme_id": "path-1", "metadata": {"kind": "video", "title": "视频"}}])
    service = DistillService(
        db,
        download,
        lambda *_args: asyncio.sleep(0, result={"original_transcript": "原文" * 75, "polished_text": "整理" * 75}),
        lambda *_args: asyncio.sleep(0, result=_valid_summary()),
        type("Synthesis", (), {"run": lambda _self, _items: asyncio.sleep(0, result={"one_sentence": "成功", "themes": [], "methods": [], "contradictions": [], "expressions": []})})(),
        tmp_path / "default",
    )
    service.retry_delays = (0, 0, 0)
    service.schedule("run-path")
    await service.active["run-path"]
    run = await db.get_distill_run("run-path")
    assert seen["root"] == str(custom.resolve())
    assert run and Path(run["output_path"]).parent.parent.parent == custom.resolve()
    await db.close()
