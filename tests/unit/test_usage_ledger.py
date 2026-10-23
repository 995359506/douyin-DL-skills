import asyncio
import json
from pathlib import Path

import httpx
import pytest

from skills.douyin_downloader.usage_ledger import (
    BatchRequestLimitReached,
    UsageLedger,
    content_hash,
    extract_usage,
    model_request_context,
    recorded_post,
)
from skills.douyin_downloader.transcription_service import LLMConfig, TranscriptionService
from skills.douyin_downloader.db import TaskDB
from skills.douyin_downloader.distill_service import DistillService, DistillSynthesis
from skills.douyin_downloader.video_summary_service import SummaryConfig, VideoPreparation, VideoSummaryService


@pytest.mark.asyncio
async def test_request_limit_is_atomic_across_five_workers_and_connections(tmp_path):
    first, second = UsageLedger(tmp_path / "ledger.db"), UsageLedger(tmp_path / "ledger.db")
    await first.set_limit("download", "batch", 3)
    await second.initialize()

    async def reserve(index):
        with model_request_context(batch_kind="download", batch_id="batch", execution_id=f"video-{index}"):
            try:
                return await (first if index % 2 else second).reserve(execution_id="fallback", stage="summary", provider="gateway", model="test")
            except BatchRequestLimitReached:
                return None

    rows = await asyncio.gather(*(reserve(index) for index in range(5)))
    assert sum(value is not None for value in rows) == 3
    stats = await first.stats("download", "batch")
    assert stats["request_count"] == stats["in_flight_count"] == 3
    assert stats["paused"] is True
    assert stats["cost_status"] == "unknown"
    await first.set_limit("download", "batch", 4)
    await first.resume("download", "batch")
    with model_request_context(batch_kind="download", batch_id="batch", execution_id="next"):
        assert await first.reserve(execution_id="next", stage="summary", provider="gateway", model="test")
    assert (await first.stats("download", "batch"))["request_count"] == 4


@pytest.mark.asyncio
async def test_every_real_post_is_recorded_without_persisting_body_and_unknown_is_not_zero(tmp_path):
    ledger = UsageLedger(tmp_path / "ledger.db")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"secret_body": "never persist", "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "totalTokenCount": 120}}))) as client:
        with model_request_context(batch_kind="topic", batch_id="topic", execution_id="version"):
            await recorded_post(client, "https://test.invalid/model", ledger=ledger, stage="topic", provider="gateway", model="test", json={"private_source": "never persist"})
    stats = await ledger.stats("topic", "topic")
    assert stats["input_tokens"] == 100 and stats["output_tokens"] == 20
    assert stats["cost_status"] == "unknown" and stats["estimated_costs"] == {}
    row = (await ledger.attempts("topic", "topic"))[0]
    assert "never persist" not in json.dumps(row)
    assert row["status"] == "succeeded"
    assert extract_usage({}) == {"usage_complete": False}
    assert extract_usage({"usage": {"prompt_tokens": True}}) == {"usage_complete": False}


@pytest.mark.asyncio
async def test_configured_price_is_estimate_and_unknown_usage_remains_incomplete(tmp_path):
    ledger = UsageLedger(tmp_path / "ledger.db")
    await ledger.set_price("gateway", "test", version="v1", currency="CNY", input_per_million=10, output_per_million=20)
    with model_request_context(batch_kind="download", batch_id="b", execution_id="job"):
        first = await ledger.reserve(execution_id="job", stage="summary", provider="gateway", model="test")
        await ledger.finish(first, elapsed_seconds=2, status="succeeded", response={"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}})
        second = await ledger.reserve(execution_id="job", stage="summary", provider="gateway", model="test", retry_index=1, is_fallback=True)
        await ledger.finish(second, elapsed_seconds=1, status="unknown")
    stats = await ledger.stats("download", "b")
    assert stats["estimated_costs"] == {"CNY": 20}
    assert stats["cost_status"] == "incomplete"
    assert stats["input_tokens"] is None
    assert stats["request_count"] == 2 and stats["logical_execution_count"] == 1
    assert stats["retry_request_count"] == stats["fallback_request_count"] == 1
    assert stats["network_duration_seconds_range"] is None
    # 更新单价不能篡改已完成请求的价格快照。
    await ledger.set_price("gateway", "test", version="v2", currency="CNY", per_request=999)
    assert (await ledger.stats("download", "b"))["estimated_costs"] == {"CNY": 20}


@pytest.mark.asyncio
async def test_transport_timeout_keeps_quota_and_no_free_resend(tmp_path):
    ledger = UsageLedger(tmp_path / "ledger.db")
    await ledger.set_limit("download", "b", 1)
    calls = []
    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with model_request_context(batch_kind="download", batch_id="b", execution_id="job"):
            with pytest.raises(httpx.ReadTimeout):
                await recorded_post(client, "https://test.invalid", ledger=ledger, execution_id="job", stage="summary", provider="gateway", model="test")
            with pytest.raises(BatchRequestLimitReached):
                await recorded_post(client, "https://test.invalid", ledger=ledger, execution_id="job", stage="summary", provider="gateway", model="test")
    assert len(calls) == 1
    assert (await ledger.attempts("download", "b"))[0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_transcript_checkpoint_survives_polish_budget_pause_without_repeating_asr(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path / "ledger.db")
    await ledger.set_limit("download", "b", 1)
    config = LLMConfig(base_url="https://text.invalid", api_key="fake", dashscope_api_key="fake", request_retries=0)
    service = TranscriptionService(config, ledger=ledger)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"synthetic video")
    calls = []
    partial = []

    async def save_partial(text):
        partial.append(text)

    async def extract(_video, audio):
        audio.write_bytes(b"synthetic audio")

    def request(request):
        calls.append(str(request.url))
        if "dashscope" in str(request.url):
            return httpx.Response(200, json={"output": {"text": "原始转写快照"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "整理后的快照。"}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr("skills.douyin_downloader.transcription_service.httpx.AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(request), **kwargs))
    monkeypatch.setattr(service, "extract_audio", extract)
    with model_request_context(batch_kind="download", batch_id="b", execution_id="execution"):
        with pytest.raises(BatchRequestLimitReached):
            await service.run("job", video, partial_callback=save_partial)
    assert len(calls) == 1
    assert partial == ["原始转写快照"]
    await ledger.set_limit("download", "b", 2)
    await ledger.resume("download", "b")
    with model_request_context(batch_kind="download", batch_id="b", execution_id="execution"):
        result = await service.run("job", video)
        again = await service.run("job", video)
    assert result == again == {"original_transcript": "原始转写快照", "polished_text": "整理后的快照。", "segments": []}
    assert len(calls) == 2
    assert sum("dashscope" in url for url in calls) == 1


@pytest.mark.asyncio
async def test_summary_parse_retry_and_fallback_cannot_bypass_request_limit(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path / "ledger.db")
    await ledger.set_limit("download", "b", 1)
    service = VideoSummaryService(SummaryConfig(gateway_api_key="fake", api_key="fake", request_retries=3, retry_backoff_seconds=0), ledger=ledger)
    calls = []

    def invalid(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "不是JSON"}]}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr("skills.douyin_downloader.video_summary_service.httpx.AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(invalid), **kwargs))
    with model_request_context(batch_kind="download", batch_id="b", execution_id="job"):
        with pytest.raises(BatchRequestLimitReached):
            await service._generate_parsed("job", None, "synthetic input")
    assert len(calls) == 1
    assert (await ledger.stats("download", "b"))["request_count"] == 1


@pytest.mark.asyncio
async def test_segment_time_mapping_uses_original_boundaries_and_never_model_merge(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path / "ledger.db")
    service = VideoSummaryService(SummaryConfig(api_key="fake"), ledger=ledger)
    video = tmp_path / "video.mp4"
    parts = [tmp_path / "part-0.mp4", tmp_path / "part-1.mp4"]
    for path in [video, *parts]:
        path.write_bytes(b"synthetic")
    requests = []

    async def generate(job_id, path, prompt):
        requests.append(path)
        if path is None:
            return {"title": "合并", "timeline": [{"time": 99999, "visual": "不能采用模型重写的时间"}]}
        return {"title": "片段", "timeline": [{"time": "00:05", "visual": "画面"}, {"time": 1000, "visual": "超出片长"}, {"time": 0, "visual": "起点"}]}

    monkeypatch.setattr(service, "_generate_parsed", generate)
    prep = VideoPreparation("chunked", video, parts, 10, 8, (0.0, 60.0), (60.0, 60.0))
    with model_request_context(batch_kind="download", batch_id="b", execution_id="job"):
        result = await service._summarize_segments("job", prep, None)
        again = await service._summarize_segments("job", prep, None)
    assert result == again and len(requests) == 3
    valid = [item["time_seconds"] for item in result["timeline"] if item["time_seconds"] is not None]
    assert valid == [0, 5, 60, 65]
    assert all(item["time_basis"] == "mapped_estimate" for item in result["timeline"] if item["time_seconds"] is not None)
    assert sum(item["time_seconds"] is None for item in result["timeline"]) == 2
    assert all(item["time"] != 99999 for item in result["timeline"])


@pytest.mark.asyncio
async def test_checkpoint_never_reuses_different_input_or_execution(tmp_path):
    ledger = UsageLedger(tmp_path / "ledger.db")
    with model_request_context(execution_id="first"):
        await ledger.save_checkpoint("ignored", "summary", content_hash("source1"), {"saved": "text"})
        assert await ledger.get_checkpoint("ignored", "summary", content_hash("source2")) is None
    with model_request_context(execution_id="forced-new"):
        assert await ledger.get_checkpoint("ignored", "summary", content_hash("source1")) is None


@pytest.mark.asyncio
async def test_missing_output_price_and_unaccounted_usage_are_unknown(tmp_path):
    ledger = UsageLedger(tmp_path / "ledger.db")
    await ledger.set_price("gateway", "test", version="v1", currency="CNY", input_per_million=1)
    with model_request_context(batch_kind="download", batch_id="b", execution_id="job"):
        request = await ledger.reserve(execution_id="job", stage="summary", provider="gateway", model="test")
        await ledger.finish(request, elapsed_seconds=1, status="succeeded", response={"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20, "totalTokenCount": 30}})
    assert (await ledger.stats("download", "b"))["cost_status"] == "unknown"
    usage = extract_usage({"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20, "thoughtsTokenCount": 5, "totalTokenCount": 35}})
    assert usage["output_tokens"] == 25 and usage["usage_complete"] is True
    assert extract_usage({"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20, "totalTokenCount": 35}})["usage_complete"] is False


@pytest.mark.asyncio
async def test_restart_preserves_unknown_request_and_does_not_release_its_quota(tmp_path):
    ledger = UsageLedger(tmp_path / "ledger.db")
    await ledger.set_limit("download", "b", 1)
    with model_request_context(batch_kind="download", batch_id="b", execution_id="job"):
        await ledger.reserve(execution_id="job", stage="summary", provider="gateway", model="test")
    assert await ledger.recover_after_restart() == 1
    assert await ledger.recover_after_restart() == 0
    stats = await ledger.stats("download", "b")
    assert stats["request_count"] == 1 and stats["in_flight_count"] == 0
    assert (await ledger.attempts("download", "b"))[0]["status"] == "unknown"
    with model_request_context(batch_kind="download", batch_id="b", execution_id="job"):
        with pytest.raises(BatchRequestLimitReached):
            await ledger.reserve(execution_id="job", stage="summary", provider="gateway", model="test")


@pytest.mark.asyncio
async def test_summary_http_retries_and_provider_fallback_are_individually_charged(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path / "ledger.db")
    service = VideoSummaryService(SummaryConfig(gateway_api_key="fake", api_key="fake", request_retries=1, retry_backoff_seconds=0), ledger=ledger)
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if "aigateway" in str(request.url):
            return httpx.Response(503, json={"error": {"message": "synthetic unavailable"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"title":"有效结果"}'}]}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr("skills.douyin_downloader.video_summary_service.httpx.AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    with model_request_context(batch_kind="download", batch_id="b", execution_id="job"):
        result = await service._generate_parsed("job", None, "synthetic input")
    assert result["title"] == "有效结果" and len(calls) == 3
    rows = await ledger.attempts("download", "b")
    assert [row["http_status"] for row in rows] == [503, 503, 200]
    assert [row["retry_index"] for row in rows] == [0, 1, 0]
    assert [row["is_fallback"] for row in rows] == [0, 0, 1]


@pytest.mark.asyncio
async def test_distill_budget_pause_is_not_swallowed_or_degraded(tmp_path):
    db = TaskDB(str(tmp_path / "distill.db"))
    await db.connect()
    await db.create_distill_run("run", sec_user_id="sec", nickname="博主", user_url="u")
    await db.add_distill_items("run", [{"ordinal": 1, "aweme_id": "1", "metadata": {"kind": "video", "title": "视频"}}])
    video = tmp_path / "video.mp4"
    video.write_bytes(b"synthetic")
    calls = []

    async def transcribe(*args):
        calls.append("transcript")
        raise BatchRequestLimitReached("distill", "run", 0)

    async def summarize(*args):
        calls.append("summary")
        return {}

    class Synthesis:
        async def run(self, items):
            calls.append("synthesis")
            return {}

    service = DistillService(db, lambda _item: asyncio.sleep(0, result=video), transcribe, summarize, Synthesis(), tmp_path)
    try:
        await service.run("run")
        run = await db.get_distill_run("run")
        item = await db.get_distill_item("run", "1")
        assert run["status"] == "paused"
        assert item["item_status"] == "queued" and item["transcript_status"] == "pending"
        assert item["download_status"] == "completed"
        assert calls == ["transcript"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_distill_map_checkpoints_keep_completed_groups_across_pause(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path / "ledger.db")
    synthesis = DistillSynthesis(base_url="https://text.invalid", api_key="fake", ledger=ledger)
    synthesis.max_chars = 150
    items = [{"ordinal": index, "aweme_id": str(index), "item_status": "ready", "polished_text": "来源资料" * 50, "metadata": {"title": f"视频{index}"}} for index in (1, 2)]
    calls, paused = [], True

    async def chat(prompt):
        nonlocal paused
        calls.append(prompt)
        if len(calls) == 2 and paused:
            paused = False
            raise BatchRequestLimitReached("distill", "run", 1)
        return json.dumps({"themes": [], "methods": [], "contradictions": [], "expressions": [], "one_sentence": {"text": "归纳", "sources": ["001"]}}, ensure_ascii=False)

    monkeypatch.setattr(synthesis, "_chat", chat)
    with model_request_context(batch_kind="distill", batch_id="run", execution_id="run:synthesis"):
        with pytest.raises(BatchRequestLimitReached):
            await synthesis.run(items)
        result = await synthesis.run(items)
        again = await synthesis.run(items)
    assert result == again
    assert len(calls) == 4  # 组一成功、组二暂停、组二恢复、reduce；成功组与reduce不重复。
    assert calls[0] not in calls[1:]


@pytest.mark.asyncio
async def test_task_cleanup_covers_forced_versions_and_preserves_shared_execution(tmp_path):
    ledger = UsageLedger(tmp_path / "ledger.db")
    for execution in ("first-version", "forced-second-version", "shared-version"):
        await ledger.bind_execution(execution, "task")
        await ledger.save_checkpoint(execution, "summary", "input", {"saved": execution})
    await ledger.bind_execution("shared-version", "another-task")
    assert await ledger.delete_task_checkpoints("task") == 2
    assert await ledger.get_checkpoint("first-version", "summary", "input") is None
    assert await ledger.get_checkpoint("forced-second-version", "summary", "input") is None
    assert await ledger.get_checkpoint("shared-version", "summary", "input") == {"saved": "shared-version"}
    assert await ledger.delete_task_checkpoints("another-task") == 1
    assert await ledger.get_checkpoint("shared-version", "summary", "input") is None


@pytest.mark.asyncio
async def test_explicit_distill_retry_clears_invalid_summary_checkpoint_only(tmp_path, monkeypatch):
    db = TaskDB(str(tmp_path / "distill.db"))
    await db.connect()
    await db.create_distill_run("run", sec_user_id="sec", nickname="博主", user_url="u")
    await db.add_distill_items("run", [{"ordinal": 1, "aweme_id": "1", "metadata": {"kind": "video", "title": "视频"}}])
    await db.update_distill_item("run", "1", item_status="summary_invalid", transcript_status="completed", original_transcript="已保留的原始转写", polished_text="已保留的整理稿", summary_status="completed", summary_json='{"overview":"无效结构"}')
    ledger = UsageLedger(tmp_path / "distill.db")
    await ledger.save_checkpoint("run:1", "summary_completed", "input", {"overview": "无效结构"})
    await ledger.save_checkpoint("another-execution", "summary_completed", "input", {"overview": "独立保留"})

    class Synthesis:
        async def run(self, items):
            return {}

    synthesis = Synthesis()
    synthesis.ledger = ledger
    service = DistillService(db, None, None, None, synthesis, tmp_path)
    monkeypatch.setattr(service, "schedule", lambda run_id: None)
    try:
        await service.resume("run", ["1"])
        item = await db.get_distill_item("run", "1")
        assert item["original_transcript"] == "已保留的原始转写"
        assert item["polished_text"] == "已保留的整理稿"
        assert item["summary_status"] == "pending" and item["summary"] is None
        assert await ledger.get_checkpoint("run:1", "summary_completed", "input") is None
        assert await ledger.get_checkpoint("another-execution", "summary_completed", "input") == {"overview": "独立保留"}
    finally:
        await db.close()
