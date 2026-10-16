from pathlib import Path

import pytest

from skills.douyin_downloader.transcription_service import (
    LLMConfig,
    ModelRequestError,
    TranscriptionService,
    _content_text,
    serialize_error,
)


def test_content_text_supports_string_and_parts():
    assert _content_text({"choices": [{"message": {"content": " 文案 "}}]}) == "文案"
    assert _content_text({"choices": [{"message": {"content": [{"text": "第一句"}, {"text": "第二句"}]}}]}) == "第一句第二句"


def test_config_requires_api_key_and_secure_transport():
    with pytest.raises(RuntimeError, match="API Key"):
        TranscriptionService(LLMConfig(base_url="https://example.test/v1", api_key="")).validate_config()
    with pytest.raises(RuntimeError, match="明文 HTTP"):
        TranscriptionService(LLMConfig(base_url="http://example.test/v1", api_key="key", dashscope_api_key="key")).validate_config()
    TranscriptionService(LLMConfig(base_url="http://127.0.0.1:8080/v1", api_key="key", dashscope_api_key="key", allow_insecure_http=True)).validate_config()


def test_dashscope_response_text_parser():
    from skills.douyin_downloader.transcription_service import _dashscope_content_text

    assert _dashscope_content_text({"output": {"text": "  你好  "}}) == "你好"
    assert _dashscope_content_text({"output": {"output": {"sentence": {"text": "第二句"}}}}) == "第二句"


def test_model_errors_are_actionable():
    rate_limited = serialize_error(ModelRequestError(429, "All available accounts are currently rate-limited.", "gpt-4o-audio-preview"))
    assert rate_limited["code"] == "MODEL_RATE_LIMITED"
    assert "限流" in rate_limited["reason"]

    upstream = ModelRequestError(502, "Upstream request failed", "gpt-4o-audio-preview")
    upstream.stage = "transcription"
    parsed = serialize_error(upstream)
    assert parsed["code"] == "MODEL_UPSTREAM_UNAVAILABLE"
    assert "502" in parsed["reason"]


@pytest.mark.asyncio
async def test_transcription_retries_transient_http_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = TranscriptionService(LLMConfig(
        base_url="https://example.test/v1",
        api_key="key",
        dashscope_api_key="key",
        request_retries=3,
    ))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"mock-wav")
    attempts = 0
    delays = []

    class FakeResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.headers = {}

        def json(self):
            if self.status_code >= 400:
                return {"message": "temporary upstream error"}
            return {"output": {"text": "识别成功"}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            return FakeResponse(503 if attempts == 1 else 200)

    import skills.douyin_downloader.transcription_service as module

    async def no_wait(_job_id: str, delay: float):
        delays.append(delay)

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(service, "_wait_retry", no_wait)
    assert await service.transcribe_audio(audio, "job-1") == "识别成功"
    assert attempts == 2
    assert delays == [1.0]


@pytest.mark.asyncio
async def test_transcription_does_not_retry_non_retryable_http_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = TranscriptionService(LLMConfig(
        base_url="https://example.test/v1", api_key="key", dashscope_api_key="key", request_retries=3
    ))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"mock-wav")
    attempts = 0

    class FakeResponse:
        status_code = 400
        headers = {}

        def json(self):
            return {"message": "invalid audio request"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            return FakeResponse()

    import skills.douyin_downloader.transcription_service as module

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    with pytest.raises(ModelRequestError) as caught:
        await service.transcribe_audio(audio, "job-1")
    assert caught.value.status_code == 400
    assert attempts == 1


@pytest.mark.asyncio
async def test_run_uses_mock_audio_and_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = TranscriptionService(LLMConfig(base_url="https://example.test/v1", api_key="key", dashscope_api_key="key"))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"mock-video")

    async def fake_extract(_video: Path, output: Path) -> None:
        output.write_bytes(b"mock-wav")

    async def fake_transcribe(_audio: Path, _job_id: str) -> str:
        return "你好，这是原始转写。"

    async def fake_polish(_transcript: str, _job_id: str) -> str:
        return "你好，这是整理后的口播文案。"

    monkeypatch.setattr(service, "extract_audio", fake_extract)
    monkeypatch.setattr(service, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(service, "polish_transcript", fake_polish)
    result = await service.run("job-1", video)
    assert result["original_transcript"] == "你好，这是原始转写。"
    assert result["polished_text"] == "你好，这是整理后的口播文案。"
