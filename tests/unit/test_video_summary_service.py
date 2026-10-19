import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skills.douyin_downloader.db import TaskDB
from skills.douyin_downloader.video_summary_service import (
    SummaryConfig,
    SummaryModelError,
    SummaryResponseError,
    SummaryTransportError,
    VideoPreparation,
    VideoProbe,
    VideoSummaryService,
    _parse_summary_response,
    normalize_comment_ideas,
    serialize_summary_error,
)


def test_summary_config_requires_https_and_key():
    with pytest.raises(RuntimeError, match="API Key"):
        VideoSummaryService(SummaryConfig(api_key="")).validate_config()
    with pytest.raises(RuntimeError, match="HTTPS"):
        VideoSummaryService(SummaryConfig(base_url="http://api.text168.com/v1beta", api_key="key")).validate_config()
    VideoSummaryService(SummaryConfig(api_key="key")).validate_config()


@pytest.mark.asyncio
async def test_summary_service_runs_five_in_parallel_without_download_cooldown(tmp_path, monkeypatch):
    service = VideoSummaryService(SummaryConfig(api_key="test"))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"mock-video")
    five_started, release = asyncio.Event(), asyncio.Event()
    started, active, peak = [], 0, 0

    async def prepare(job_id, path, directory, callback):
        return VideoPreparation("direct", path, [path], 10, 10)

    async def generate(job_id, path, prompt):
        nonlocal active, peak
        started.append(job_id)
        active += 1
        peak = max(peak, active)
        if active == 5:
            five_started.set()
        try:
            await release.wait()
            return {"title": "测试总结"}
        finally:
            active -= 1

    monkeypatch.setattr(service, "_prepare_video", prepare)
    monkeypatch.setattr(service, "_generate_parsed", generate)
    jobs = [asyncio.create_task(service.summarize_video(f"job-{index}", video)) for index in range(6)]
    try:
        await asyncio.wait_for(five_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert len(started) == 5 and peak == 5
        release.set()
        results = await asyncio.gather(*jobs)
        assert len(results) == 6 and peak == 5
    finally:
        release.set()
        await asyncio.gather(*jobs, return_exceptions=True)


def test_fallback_model_defaults_to_gemini_25_flash(monkeypatch):
    monkeypatch.delenv("DOUYIN_VIDEO_SUMMARY_FALLBACK_MODEL", raising=False)
    monkeypatch.setenv("DOUYIN_VIDEO_SUMMARY_API_KEY", "test")
    monkeypatch.setenv("DOUYIN_VIDEO_SUMMARY_FALLBACK_API_KEY", "test")
    assert SummaryConfig(api_key="test").fallback_model == "gemini-2.5-flash"
    assert SummaryConfig.from_environment().fallback_model == "gemini-2.5-flash"


def test_gateway_precedes_existing_channels():
    service = VideoSummaryService(SummaryConfig(
        gateway_api_key="gateway-test", api_key="primary-test", fallback_api_key="fallback-test",
    ))
    service.validate_config()
    assert [(provider["name"], provider["model"]) for provider in service._providers()] == [
        ("gateway", "gemini-3.8-flash"),
        ("primary", "gemini-3.7-flash"),
        ("fallback", "gemini-2.5-flash"),
    ]


@pytest.mark.asyncio
async def test_gateway_failover_reaches_25_fallback(monkeypatch):
    service = VideoSummaryService(SummaryConfig(
        gateway_api_key="gateway-test", api_key="primary-test", fallback_api_key="fallback-test",
    ))
    attempts = []

    async def request(_job_id, provider, _payload, _timeout):
        attempts.append(provider["name"])
        if provider["name"] != "fallback":
            raise SummaryModelError(503, "通道不可用", provider["model"], provider=provider["name"])
        return {"candidates": [{"content": {"parts": [{"text": '{"title":"保底成功"}'}]}}]}

    monkeypatch.setattr(service, "_request_provider", request)
    response = await service._request_generate("job-three-channels", None, "测试")
    assert attempts == ["gateway", "primary", "fallback"]
    assert response["candidates"][0]["content"]["parts"][0]["text"] == '{"title":"保底成功"}'
    assert service._job_provider_indexes["job-three-channels"] == 2


def test_summary_response_parser_rejects_non_json_and_model_announcement():
    response = {"candidates": [{"content": {"parts": [{"text": "```json\n{\"title\":\"测试\",\"overview\":\"概览\"}\n```"}]}}]}
    parsed = _parse_summary_response(response)
    assert parsed["title"] == "测试"
    assert parsed["key_points"] == []

    with pytest.raises(SummaryResponseError) as bad_json:
        _parse_summary_response({"candidates": [{"content": {"parts": [{"text": "普通总结文本"}]}}]})
    assert bad_json.value.code == "SUMMARY_MODEL_BAD_JSON"

    notice = "Gemini 3.5 Flash is no longer available. Please switch to Gemini 3.7 Flash in the latest version of Antigravity."
    with pytest.raises(SummaryResponseError) as deprecated:
        _parse_summary_response({"candidates": [{"content": {"parts": [{"text": notice}]}}]})
    assert serialize_summary_error(deprecated.value)["code"] == "SUMMARY_MODEL_DEPRECATED"
    with pytest.raises(SummaryResponseError) as secret_notice:
        _parse_summary_response({"candidates": [{"content": {"parts": [{"text": notice + " API key sk-testsecret123456"}]}}]})
    assert "sk-testsecret123456" not in secret_notice.value.detail


@pytest.mark.asyncio
async def test_bad_json_retries_then_succeeds(monkeypatch):
    service = VideoSummaryService(SummaryConfig(api_key="key", request_retries=2, retry_backoff_seconds=0))
    responses = iter(["无效文本", '{"title":"恢复正常"}'])

    async def request(*_args):
        return {"candidates": [{"content": {"parts": [{"text": next(responses)}]}}]}

    monkeypatch.setattr(service, "_request_generate", request)
    result = await service._generate_parsed("job", None, "prompt")
    assert result["title"] == "恢复正常"


def test_summary_prompt_requests_detailed_research_fields():
    prompt = VideoSummaryService(SummaryConfig(api_key="key"))._summary_prompt()
    assert "不少于 180 个中文字符" in prompt
    assert "narrative_structure" in prompt
    assert "editing_and_expression" in prompt
    assert "reusable_elements" in prompt


def test_summary_prompt_separates_original_comment_ideas_from_facts():
    prompt = VideoSummaryService(SummaryConfig(api_key="key"))._summary_prompt()
    example = json.loads(prompt.split("JSON 形状示例", 1)[1].split("：", 1)[1].split("}。", 1)[0] + "}")
    assert {entry["style"] for entry in example["comment_ideas"]} == {"搞笑有梗", "抒情共鸣", "金句"}
    assert all(isinstance(entry["text"], str) for entry in example["comment_ideas"])
    assert "通常给 6 条，每类 2 条" in prompt
    assert "贴合原片具体细节" in prompt
    assert "不是真实评论或原话" in prompt
    assert "不虚构人物经历，不保证高赞" in prompt
    assert "除 comment_ideas 创作区外，只陈述视频中看见或听见的内容" in prompt


@pytest.mark.parametrize("value", [None, "一句评论", {"style": "金句", "text": "一句评论"}, 1, True])
def test_comment_ideas_reject_unrecognized_collection_shapes(value):
    assert normalize_comment_ideas(value) == []


def test_comment_ideas_normalize_valid_rows_without_stringifying_objects():
    value = [
        {"style": " 搞笑有梗 ", "text": " 这转场，比我下班还利索。 ", "extra": "忽略"},
        {"style": "金句", "text": "这转场，比我下班还利索。"},
        {"style": "抒情共鸣", "text": "原来那一盏灯，一直在等你。"},
        {"style": "金句", "text": " "},
        {"style": "其他", "text": "不支持的类型"},
        {"style": ["金句"], "text": "类型不是字符串"},
        {"style": "金句", "text": {"content": "正文不是字符串"}},
        {"style": "金句", "text": 42},
        {"style": "金句"},
        "不是对象",
        None,
    ]
    assert normalize_comment_ideas(value) == [
        {"style": "搞笑有梗", "text": "这转场，比我下班还利索。"},
        {"style": "抒情共鸣", "text": "原来那一盏灯，一直在等你。"},
    ]
    assert value[0]["style"] == " 搞笑有梗 "


def test_comment_ideas_keep_at_most_six_distinct_texts():
    values = [{"style": "金句", "text": f"原创评论 {index}"} for index in range(8)]
    assert normalize_comment_ideas(values) == values[:6]


def test_summary_response_normalizes_comment_ideas_and_accepts_legacy():
    def parse(payload):
        return _parse_summary_response({"candidates": [{"content": {"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}}]})

    parsed = parse({"title": "视频", "comment_ideas": [
        {"style": " 金句 ", "text": " 灯亮起来，家就有了方向。 "},
        {"style": "金句", "text": {"text": "不应显示的对象"}},
    ]})
    assert parsed["comment_ideas"] == [{"style": "金句", "text": "灯亮起来，家就有了方向。"}]
    assert parse({"title": "旧结果"})["comment_ideas"] == []
    assert parse({"title": "异常字段", "comment_ideas": {"text": "不是数组"}})["comment_ideas"] == []


@pytest.mark.asyncio
async def test_segment_merge_regenerates_comment_ideas_without_extra_requests(tmp_path, monkeypatch):
    service = VideoSummaryService(SummaryConfig(api_key="key"))
    paths = [tmp_path / "part-1.mp4", tmp_path / "part-2.mp4"]
    requests = []
    final_ideas = [
        {"style": " 搞笑有梗 ", "text": " 最后这一下，连反转都没反应过来。 "},
        {"style": "金句", "text": "最后这一下，连反转都没反应过来。"},
        {"style": "抒情共鸣", "text": "所有的铺垫，都是为了这一刻的相遇。"},
    ]

    async def request(job_id, video_path, prompt):
        requests.append((video_path, prompt))
        payload = {
            "title": "完整分析" if video_path is None else "片段分析",
            "comment_ideas": final_ideas if video_path is None else [{"style": "金句", "text": f"片段评论 {len(requests)}"}],
        }
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}}]}

    monkeypatch.setattr(service, "_request_generate", request)
    monkeypatch.setattr(service, "_probe_video", lambda _path: _resolved(VideoProbe(30, 640, 360, True)))
    result = await service._summarize_segments("job", VideoPreparation("chunked", tmp_path / "video.mp4", paths, 20, 10), None)
    assert len(requests) == 3
    assert [item[0] for item in requests] == [*paths, None]
    combine_prompt = requests[-1][1]
    assert "围绕完整视频重新整理、去重" in combine_prompt
    assert "不是真实评论或原话" in combine_prompt
    assert "style" in combine_prompt and "text" in combine_prompt
    assert "片段评论 1" in combine_prompt and "片段评论 2" in combine_prompt
    assert result["comment_ideas"] == [
        {"style": "搞笑有梗", "text": "最后这一下，连反转都没反应过来。"},
        {"style": "抒情共鸣", "text": "所有的铺垫，都是为了这一刻的相遇。"},
    ]


def test_summary_response_parser_preserves_detailed_fields_and_defaults_legacy():
    detailed = _parse_summary_response({"candidates": [{"content": {"parts": [{"text": json.dumps({
        "title": "测试", "narrative_structure": {"hook": "开场"},
        "audience_and_intent": {"tone": "温暖"}, "emotional_arc": ["平静→惊喜"],
        "editing_and_expression": ["慢推镜头"], "reusable_elements": ["家庭惊喜"],
    }, ensure_ascii=False)}]}}]})
    assert detailed["narrative_structure"]["hook"] == "开场"
    assert detailed["emotional_arc"] == ["平静→惊喜"]

    legacy = _parse_summary_response({"candidates": [{"content": {"parts": [{"text": '{"title":"旧结果"}'}]}}]})
    assert legacy["narrative_structure"] == {}
    assert legacy["audience_and_intent"] == {}
    assert legacy["editing_and_expression"] == []


def test_summary_error_is_actionable():
    error = serialize_summary_error(SummaryModelError(503, "No available channel", "gemini-3.7-flash", provider="primary", host="api.text168.com"))
    assert error["code"] == "SUMMARY_MODEL_UNAVAILABLE"
    assert "api.text168.com" in error["reason"]
    assert "HTTP 503" in error["reason"]
    assert "No available channel" in error["reason"]

    rate_limited = serialize_summary_error(SummaryModelError(429, "quota", "gemini-3.7-flash", 30))
    assert "30 秒" in rate_limited["action"]
    assert "配额" in rate_limited["reason"]


def test_summary_transport_error_is_actionable():
    error = serialize_summary_error(SummaryTransportError("[SSL] record layer failure", "gemini-3.7-flash"))
    assert error["code"] == "SUMMARY_MODEL_TLS_FAILED"
    assert "自动重试" in error["action"]


def test_summary_config_uses_safe_segment_default():
    assert SummaryConfig(api_key="key").segment_bytes <= 8 * 1024 * 1024


def test_summary_auth_error_identifies_fallback_and_missing_fallback():
    fallback = serialize_summary_error(SummaryModelError(401, "Invalid API key", "gemini-3.7-flash", provider="fallback", host="ai.image.mohuajz.eu.org"))
    assert "fallback" in fallback["reason"]
    assert "HTTP 401" in fallback["reason"]
    assert "Invalid API key" in fallback["reason"]
    primary = serialize_summary_error(SummaryModelError(502, "upstream unavailable", "gemini-3.7-flash", provider="primary", host="api.text168.com"))
    assert "备用接口未启用" in primary["action"]


@pytest.mark.asyncio
async def test_sticky_fallback_failure_does_not_claim_fallback_disabled(monkeypatch: pytest.MonkeyPatch):
    service = VideoSummaryService(SummaryConfig(api_key="primary-key", fallback_api_key="fallback-key"))
    service._job_provider_indexes["job"] = 1

    async def fail(_job_id, provider, _payload, _timeout):
        status = 401 if provider["name"] == "fallback" else 502
        raise SummaryModelError(status, "通道不可用", provider["model"], provider=provider["name"])

    monkeypatch.setattr(service, "_request_provider", fail)
    with pytest.raises(SummaryModelError) as failure:
        await service._request_generate("job", None, "测试")
    action = serialize_summary_error(failure.value)["action"]
    assert "备用接口已尝试且失败" in action
    assert "备用接口未启用" not in action
    assert [item["provider"] for item in failure.value.attempts] == ["fallback", "primary"]


@pytest.mark.asyncio
async def test_summary_failure_persists_preparation_before_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from skills.douyin_downloader import web

    db = TaskDB(str(tmp_path / "jobs.db"))
    await db.connect()
    task_id = await db.create_task("single")
    await db.create_video_summary_job("job", task_id, "gemini-3.7-flash", 12)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"source-video")
    prepared = tmp_path / "prepared.mp4"
    prepared.write_bytes(b"prepared")
    service = VideoSummaryService(SummaryConfig(api_key="key"))

    async def prepare(_job_id, _path, _temp_dir, _callback):
        return VideoPreparation("transcoded", video, [prepared], video.stat().st_size, prepared.stat().st_size)

    async def fail(_job_id, provider, _payload, _timeout):
        row = await db.get_video_summary_job(job_id="job")
        assert (row["prepared_bytes"], row["processing_mode"], row["segment_count"]) == (len(b"prepared"), "transcoded", 1)
        raise SummaryModelError(502, "上游不可用", provider["model"], provider=provider["name"])

    monkeypatch.setattr(service, "_prepare_video", prepare)
    monkeypatch.setattr(service, "_request_provider", fail)
    monkeypatch.setattr(web, "video_summary_service", service)
    monkeypatch.setattr(web, "task_manager", SimpleNamespace(db=db))
    await web._run_video_summary_job("job", task_id, video)
    row = await db.get_video_summary_job(job_id="job")
    assert row["status"] == "failed"
    assert (row["prepared_bytes"], row["processing_mode"], row["segment_count"]) == (len(b"prepared"), "transcoded", 1)
    await db.close()


@pytest.mark.asyncio
async def test_segment_failure_persists_count_and_failed_segment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from skills.douyin_downloader import web

    db = TaskDB(str(tmp_path / "segments.db"))
    await db.connect()
    task_id = await db.create_task("single")
    await db.create_video_summary_job("job-segments", task_id, "gemini-3.7-flash", 12)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"source-video")
    paths = [tmp_path / f"segment-{index}.mp4" for index in range(2)]
    for path in paths:
        path.write_bytes(b"part")
    service = VideoSummaryService(SummaryConfig(api_key="key"))

    async def prepare(_job_id, _path, _temp_dir, _callback):
        return VideoPreparation("chunked", video, paths, video.stat().st_size, 8)

    async def fail(_job_id, provider, _payload, _timeout):
        row = await db.get_video_summary_job(job_id="job-segments")
        assert (row["processing_mode"], row["segment_count"]) == ("chunked", 2)
        raise SummaryModelError(502, "上游不可用", provider["model"], provider=provider["name"])

    monkeypatch.setattr(service, "_prepare_video", prepare)
    monkeypatch.setattr(service, "_request_provider", fail)
    monkeypatch.setattr(web, "video_summary_service", service)
    monkeypatch.setattr(web, "task_manager", SimpleNamespace(db=db))
    await web._run_video_summary_job("job-segments", task_id, video)
    row = await db.get_video_summary_job(job_id="job-segments")
    assert row["status"] == "failed"
    assert (row["prepared_bytes"], row["processing_mode"], row["segment_count"]) == (8, "chunked", 2)
    assert row["provider_attempts"][0]["segment_index"] == 1
    await db.close()


@pytest.mark.asyncio
async def test_direct_transport_fallback_updates_segment_preparation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from skills.douyin_downloader import web

    db = TaskDB(str(tmp_path / "fallback.db"))
    await db.connect()
    task_id = await db.create_task("single")
    await db.create_video_summary_job("job-fallback-segments", task_id, "gemini-3.7-flash", 12)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"source-video")
    direct = tmp_path / "direct.mp4"
    direct.write_bytes(b"direct")
    paths = [tmp_path / f"segment-{index}.mp4" for index in range(2)]
    for path in paths:
        path.write_bytes(b"part")
    service = VideoSummaryService(SummaryConfig(api_key="key"))

    async def prepare(_job_id, _path, _temp_dir, _callback):
        return VideoPreparation("direct", video, [direct], video.stat().st_size, direct.stat().st_size)

    async def prepare_segments(_job_id, _path, _probe, _temp_dir, _callback):
        return VideoPreparation("chunked", video, paths, video.stat().st_size, 8)

    async def request(_job_id, provider, _payload, _timeout):
        row = await db.get_video_summary_job(job_id="job-fallback-segments")
        if row["processing_mode"] == "direct":
            raise SummaryTransportError("连接中断", provider["model"], provider=provider["name"])
        assert (row["prepared_bytes"], row["segment_count"]) == (8, 2)
        raise SummaryModelError(502, "上游不可用", provider["model"], provider=provider["name"])

    monkeypatch.setattr(service, "_prepare_video", prepare)
    monkeypatch.setattr(service, "_prepare_segments", prepare_segments)
    monkeypatch.setattr(service, "_probe_video", lambda _path: _resolved(VideoProbe(2.0, 640, 360, True)))
    monkeypatch.setattr(service, "_request_provider", request)
    monkeypatch.setattr(web, "video_summary_service", service)
    monkeypatch.setattr(web, "task_manager", SimpleNamespace(db=db))
    await web._run_video_summary_job("job-fallback-segments", task_id, video)
    row = await db.get_video_summary_job(job_id="job-fallback-segments")
    assert row["status"] == "failed"
    assert (row["prepared_bytes"], row["processing_mode"], row["segment_count"]) == (8, "chunked", 2)
    assert row["provider_attempts"][-1]["segment_index"] == 1
    await db.close()


@pytest.mark.asyncio
async def test_provider_attempts_record_failover_without_secrets(monkeypatch: pytest.MonkeyPatch):
    service = VideoSummaryService(SummaryConfig(api_key="primary-secret", fallback_api_key="fallback-secret"))

    async def request(_job_id, provider, _payload, _timeout):
        if provider["name"] == "primary":
            raise SummaryModelError(503, "上游不可用", provider["model"], provider="primary", host="api.text168.com")
        return {"candidates": []}

    monkeypatch.setattr(service, "_request_provider", request)
    await service._request_generate("test-job", None, "测试")
    attempts = service._job_attempts["test-job"]
    assert [(item["provider"], item["status"]) for item in attempts] == [("primary", 503), ("fallback", 200)]
    assert "secret" not in json.dumps(attempts)


@pytest.mark.asyncio
async def test_run_uses_mock_full_video_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = VideoSummaryService(SummaryConfig(api_key="key"))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"mock-video")
    monkeypatch.setattr(
        service,
        "_probe_video",
        lambda _path: _resolved(VideoProbe(2.0, 640, 360, True)),
    )

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"title": "测试视频", "overview": "测试概览"}, ensure_ascii=False)}]}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    import skills.douyin_downloader.video_summary_service as module

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    stages = []

    async def progress(stage: str, percentage: int):
        stages.append((stage, percentage))

    result = await service.summarize_video("job-1", video, progress)
    assert result["title"] == "测试视频"
    assert result["_processing"]["mode"] == "direct"
    assert result["_processing"]["original_bytes"] == len(b"mock-video")
    assert stages[-1] == ("completed", 100)


@pytest.mark.asyncio
async def test_request_retries_transport_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = VideoSummaryService(SummaryConfig(api_key="key", request_retries=2, retry_backoff_seconds=0))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"mock-video")
    attempts = 0

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": '{"title":"重试成功"}'}]}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise __import__("httpx").ConnectError("[SSL] record layer failure")
            return FakeResponse()

    import skills.douyin_downloader.video_summary_service as module

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    response = await service._request_generate("job-1", video, "请总结")
    assert attempts == 2
    assert response["candidates"][0]["content"]["parts"][0]["text"] == '{"title":"重试成功"}'


@pytest.mark.asyncio
async def test_request_retries_transient_http_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = VideoSummaryService(SummaryConfig(api_key="key", request_retries=3, retry_backoff_seconds=0))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"mock-video")
    attempts = 0

    class FakeResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.headers = {}

        def json(self):
            if self.status_code >= 400:
                return {"error": {"message": "temporary upstream error"}}
            return {"candidates": [{"content": {"parts": [{"text": '{"title":"重试成功"}'}]}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            return FakeResponse(503 if attempts < 3 else 200)

    import skills.douyin_downloader.video_summary_service as module

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    response = await service._request_generate("retry-status", video, "请总结")
    assert attempts == 3
    assert response["candidates"][0]["content"]["parts"][0]["text"] == '{"title":"重试成功"}'


@pytest.mark.asyncio
async def test_large_video_uses_temporary_transcode_and_preserves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = VideoSummaryService(SummaryConfig(api_key="key", max_video_bytes=1024, segment_bytes=900, min_video_bitrate=1))
    video = tmp_path / "video.mp4"
    original = b"original-video" * 200
    video.write_bytes(original)
    monkeypatch.setattr(service, "_probe_video", lambda _path: _resolved(VideoProbe(120.0, 1920, 1080, True)))

    async def fake_transcode(_job_id, _input, probe, temp_dir, target_bytes, _callback, purpose="full-video"):
        assert probe.duration_seconds == 120.0
        assert target_bytes == 1024
        output = temp_dir / "prepared.mp4"
        output.write_bytes(b"prepared")
        return output

    monkeypatch.setattr(service, "_transcode_to_target", fake_transcode)
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    prepared = await service._prepare_video("job-1", video, temp_dir, None)
    assert prepared.mode == "transcoded"
    assert prepared.prepared_bytes == len(b"prepared")
    assert video.read_bytes() == original


@pytest.mark.asyncio
async def test_low_bitrate_video_falls_back_to_segments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = VideoSummaryService(SummaryConfig(api_key="key", max_video_bytes=1024, segment_bytes=900, min_video_bitrate=999_999))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * 3000)
    monkeypatch.setattr(service, "_probe_video", lambda _path: _resolved(VideoProbe(600.0, 1920, 1080, True)))

    async def fake_segments(_job_id, input_path, _probe, temp_dir, _callback):
        output = temp_dir / "segment.mp4"
        output.write_bytes(b"segment")
        return VideoPreparation("chunked", input_path, [output], input_path.stat().st_size, output.stat().st_size)

    monkeypatch.setattr(service, "_prepare_segments", fake_segments)
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    prepared = await service._prepare_video("job-2", video, temp_dir, None)
    assert prepared.mode == "chunked"
    assert prepared.segment_count == 1


async def _resolved(value):
    return value


def test_summary_config_allows_trusted_local_gateway_and_rejects_untrusted_host():
    from skills.douyin_downloader.video_summary_service import SummaryConfig

    VideoSummaryService(SummaryConfig(base_url="http://127.0.0.1:9000/v1beta", api_key="key")).validate_config()
    VideoSummaryService(
        SummaryConfig(
            api_key="key",
            fallback_base_url="https://ai.image.mohuajz.eu.org/v1beta",
            fallback_api_key="fallback-key",
            fallback_model="gemini-3.8-flash",
        )
    ).validate_config()
    with pytest.raises(RuntimeError, match="可信白名单"):
        VideoSummaryService(SummaryConfig(base_url="https://api.text168.com.evil.test/v1beta", api_key="key")).validate_config()


@pytest.mark.asyncio
async def test_request_uses_fallback_provider_after_primary_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = VideoSummaryService(
        SummaryConfig(
            api_key="primary-key",
            fallback_base_url="https://ai.image.mohuajz.eu.org/v1beta",
            fallback_api_key="fallback-key",
            fallback_model="gemini-3.8-flash",
            request_retries=1,
        )
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"mock-video")
    requests: list[str] = []

    class FakeResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.headers = {}

        def json(self):
            if self.status_code >= 400:
                return {"error": {"message": "primary unavailable"}}
            return {"candidates": [{"content": {"parts": [{"text": '{"title":"备用成功"}'}]}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **_kwargs):
            requests.append(url)
            return FakeResponse(503 if "api.text168.com" in url else 200)

    import skills.douyin_downloader.video_summary_service as module

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    response = await service._request_generate("job-fallback", video, "请总结")

    assert response["candidates"][0]["content"]["parts"][0]["text"] == '{"title":"备用成功"}'
    assert requests == [
        "https://api.text168.com/v1beta/models/gemini-3.7-flash:generateContent",
        "https://api.text168.com/v1beta/models/gemini-3.7-flash:generateContent",
        "https://ai.image.mohuajz.eu.org/v1beta/models/gemini-3.8-flash:generateContent",
    ]
