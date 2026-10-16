"""本地视频口播文案提取服务。

音频提取在本机完成，模型调用使用 OpenAI 兼容 Chat Completions 接口。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx


ProgressCallback = Callable[[str, int], Awaitable[None]]
KEYCHAIN_STATUS: Dict[str, Dict[str, Any]] = {}


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    transcribe_model: str = "gpt-4o-audio-preview"
    polish_model: str = "gpt-5.6-luna"
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    dashscope_api_key: str = ""
    timeout_seconds: float = 180.0
    request_retries: int = 3
    allow_insecure_http: bool = False

    @classmethod
    def from_environment(cls) -> "LLMConfig":
        base_url = os.getenv("DOUYIN_LLM_BASE_URL", "").strip().rstrip("/")
        api_key = os.getenv("DOUYIN_LLM_API_KEY", "").strip()
        if not api_key:
            api_key = _keychain_secret("douyin-downloader-llm")
        dashscope_api_key = os.getenv("DOUYIN_DASHSCOPE_API_KEY", "").strip()
        if not dashscope_api_key:
            dashscope_api_key = _keychain_secret("douyin-downloader-dashscope")
        return cls(
            base_url=base_url,
            api_key=api_key,
            transcribe_model=os.getenv("DOUYIN_LLM_TRANSCRIBE_MODEL", "qwen-audio-3.0-asr-flash").strip(),
            polish_model=os.getenv("DOUYIN_LLM_POLISH_MODEL", "gpt-5.6-luna").strip(),
            dashscope_base_url=os.getenv(
                "DOUYIN_DASHSCOPE_BASE_URL",
                "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
            ).strip(),
            dashscope_api_key=dashscope_api_key,
            timeout_seconds=float(os.getenv("DOUYIN_LLM_TIMEOUT_SECONDS", "180")),
            request_retries=max(0, int(os.getenv("DOUYIN_LLM_REQUEST_RETRIES", "3"))),
            allow_insecure_http=os.getenv("DOUYIN_LLM_ALLOW_INSECURE_HTTP", "0").lower() in {"1", "true", "yes"},
        )


def _keychain_secret(service: str) -> str:
    """从 macOS Keychain 读取密钥，同时保留脱敏诊断状态。"""
    if shutil.which("security") is None:
        KEYCHAIN_STATUS[service] = {"status": "unavailable", "checked_at": datetime.now().isoformat()}
        return ""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            KEYCHAIN_STATUS[service] = {"status": "ok", "checked_at": datetime.now().isoformat()}
            return result.stdout.strip()
        KEYCHAIN_STATUS[service] = {
            "status": "denied" if result.returncode == 36 else "missing",
            "checked_at": datetime.now().isoformat(),
        }
        return ""
    except subprocess.TimeoutExpired:
        KEYCHAIN_STATUS[service] = {"status": "timeout", "checked_at": datetime.now().isoformat()}
        return ""
    except (OSError, subprocess.SubprocessError):
        KEYCHAIN_STATUS[service] = {"status": "error", "checked_at": datetime.now().isoformat()}
        return ""


def _error(stage: str, code: str, reason: str, action: str) -> Dict[str, str]:
    return {"stage": stage, "code": code, "reason": reason, "action": action}


class ModelRequestError(RuntimeError):
    """An OpenAI-compatible gateway rejected a model request."""

    def __init__(self, status_code: int, detail: str, model: str):
        self.status_code = status_code
        self.detail = detail or "服务端返回错误"
        self.model = model
        self.stage = "transcription"
        super().__init__(f"模型请求失败（HTTP {status_code}）：{self.detail}")


class TranscriptionService:
    MAX_AUDIO_BYTES = 20 * 1024 * 1024
    RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig.from_environment()
        self._cancel_events: Dict[str, asyncio.Event] = {}

    def validate_config(self) -> None:
        if not self.config.base_url:
            raise RuntimeError("未配置文案整理模型服务地址")
        if not self.config.api_key:
            raise RuntimeError("未配置文案整理模型 API Key")
        if not self.config.dashscope_base_url:
            raise RuntimeError("未配置 DashScope 语音转写服务地址")
        if not self.config.dashscope_api_key:
            raise RuntimeError("未配置 DashScope API Key")
        if not self.config.allow_insecure_http and self.config.base_url.startswith("http://"):
            raise RuntimeError("文案整理模型使用明文 HTTP，需配置 HTTPS 或显式开启本地不安全 HTTP")
        if not self.config.base_url.startswith(("http://", "https://")):
            raise RuntimeError("文案整理模型服务地址必须是 http:// 或 https://")
        if not self.config.dashscope_base_url.startswith(("http://", "https://")):
            raise RuntimeError("DashScope 服务地址必须是 http:// 或 https://")

    def cancel(self, job_id: str) -> None:
        event = self._cancel_events.get(job_id)
        if event:
            event.set()

    def _check_cancelled(self, job_id: str) -> None:
        event = self._cancel_events.get(job_id)
        if event and event.is_set():
            raise asyncio.CancelledError

    async def _wait_retry(self, job_id: str, delay: float) -> None:
        event = self._cancel_events.get(job_id)
        if event:
            try:
                await asyncio.wait_for(event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        elif delay:
            await asyncio.sleep(delay)
        self._check_cancelled(job_id)

    def _retry_delay(self, response: httpx.Response, retry_index: int) -> float:
        try:
            retry_after = float(response.headers.get("retry-after", ""))
            if retry_after >= 0:
                return retry_after
        except (TypeError, ValueError):
            pass
        return float(2 ** retry_index)

    async def _post_json_with_retry(
        self,
        job_id: str,
        url: str,
        *,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        is_valid: Optional[Callable[[Dict[str, Any]], bool]] = None,
        invalid_detail: str = "模型服务返回了无效的响应结构",
    ) -> Dict[str, Any]:
        timeout = httpx.Timeout(self.config.timeout_seconds, connect=20.0)
        for attempt in range(self.config.request_retries + 1):
            self._check_cancelled(job_id)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, headers=headers, json=payload)
            except httpx.TransportError:
                if attempt >= self.config.request_retries:
                    raise
                await self._wait_retry(job_id, float(2 ** attempt))
                continue

            if response.status_code >= 400:
                try:
                    body = response.json()
                    detail = body.get("message") or body.get("error", {}).get("message") or body.get("code")
                except (AttributeError, TypeError, ValueError):
                    detail = None
                delay = self._retry_delay(response, attempt)
                if (
                    response.status_code in self.RETRYABLE_STATUS_CODES
                    and attempt < self.config.request_retries
                    and delay <= 60
                ):
                    await self._wait_retry(job_id, delay)
                    continue
                raise ModelRequestError(response.status_code, detail or "服务端返回错误", "")

            try:
                result = response.json()
            except (TypeError, ValueError):
                if attempt < self.config.request_retries:
                    await self._wait_retry(job_id, float(2 ** attempt))
                    continue
                raise ModelRequestError(502, "模型服务返回了无法解析的响应", "")
            if isinstance(result, dict) and (is_valid is None or is_valid(result)):
                return result
            if attempt < self.config.request_retries:
                await self._wait_retry(job_id, float(2 ** attempt))
                continue
            raise ModelRequestError(502, invalid_detail, "")
        raise AssertionError("模型请求重试循环意外结束")

    async def _update(self, callback: Optional[ProgressCallback], stage: str, progress: int) -> None:
        if callback:
            await callback(stage, max(0, min(100, progress)))

    async def extract_audio(self, video_path: Path, output_path: Path) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("本机未安装 ffmpeg，无法提取音频")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="ignore")[-600:]
            raise RuntimeError(f"音频提取失败：{detail or 'ffmpeg 返回非零状态'}")

    async def _chat(self, model: str, messages: List[Dict[str, Any]], job_id: str = "") -> Dict[str, Any]:
        self.validate_config()
        url = f"{self.config.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "temperature": 0}
        try:
            response = await self._post_json_with_retry(
                job_id,
                url,
                headers=headers,
                payload=payload,
                is_valid=lambda value: bool(_content_text(value)),
                invalid_detail="模型未返回有效口播文案",
            )
        except ModelRequestError as exc:
            exc.model = model
            raise
        return response

    async def transcribe_audio(self, audio_path: Path, job_id: str) -> str:
        data_uri = "data:audio/wav;base64," + base64.b64encode(audio_path.read_bytes()).decode("ascii")
        try:
            response_json = await self._post_json_with_retry(
                job_id,
                self.config.dashscope_base_url,
                headers={
                    "Authorization": f"Bearer {self.config.dashscope_api_key}",
                    "Content-Type": "application/json",
                    "X-DashScope-SSE": "disable",
                },
                payload={
                    "model": self.config.transcribe_model,
                    "input": {"messages": [{"role": "user", "content": [
                        {"type": "input_audio", "input_audio": {"data": data_uri}},
                    ]}]},
                    "parameters": {"format": "wav", "sample_rate": 16000},
                },
                is_valid=lambda value: bool(_dashscope_content_text(value)),
                invalid_detail="DashScope 未返回有效转写文本",
            )
        except ModelRequestError as exc:
            exc.stage = "transcription"
            exc.model = self.config.transcribe_model
            raise
        self._check_cancelled(job_id)
        return _dashscope_content_text(response_json)

    async def polish_transcript(self, transcript: str, job_id: str) -> str:
        try:
            response = await self._chat(
                self.config.polish_model,
                [{"role": "user", "content": (
                    "请将下面的中文口语转写整理成可直接阅读的口播文案。"
                    "只做断句、标点和少量口头语清理，不得扩写、改写事实或编造内容。"
                    "只返回整理后的文案。\n\n原始转写：\n" + transcript
                )}],
                job_id,
            )
        except ModelRequestError as exc:
            exc.stage = "polishing"
            raise
        self._check_cancelled(job_id)
        return _content_text(response)

    async def run(self, job_id: str, video_path: Path, callback: Optional[ProgressCallback] = None) -> Dict[str, Any]:
        self._cancel_events[job_id] = asyncio.Event()
        try:
            self._check_cancelled(job_id)
            await self._update(callback, "extracting_audio", 15)
            with tempfile.TemporaryDirectory(prefix="douyin-transcript-") as temp_dir:
                audio_path = Path(temp_dir) / "audio.wav"
                await self.extract_audio(video_path, audio_path)
                if audio_path.stat().st_size > self.MAX_AUDIO_BYTES:
                    raise RuntimeError("提取后的音频超过 20 MB，暂不支持该视频长度；请先裁剪视频后重试")
                self._check_cancelled(job_id)
                await self._update(callback, "transcribing", 45)
                transcript = await self.transcribe_audio(audio_path, job_id)
                if not transcript.strip():
                    raise RuntimeError("模型未返回有效转写内容")
                await self._update(callback, "polishing", 75)
                polished = await self.polish_transcript(transcript, job_id)
                await self._update(callback, "completed", 100)
                return {"original_transcript": transcript, "polished_text": polished, "segments": []}
        finally:
            self._cancel_events.pop(job_id, None)


def _content_text(response: Dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    content = (choices[0].get("message") or {}).get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict)).strip()
    return str(content).strip()


def _dashscope_content_text(response: Dict[str, Any]) -> str:
    """Parse Qwen-Audio ASR's non-OpenAI response shape."""
    output = response.get("output") or {}
    if isinstance(output, dict):
        text = output.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        nested = output.get("output") or {}
        if isinstance(nested, dict):
            sentence = nested.get("sentence") or {}
            if isinstance(sentence, dict) and isinstance(sentence.get("text"), str):
                return sentence["text"].strip()
    return _content_text(response)


def new_job_id() -> str:
    return str(uuid.uuid4())


def serialize_error(exc: BaseException) -> Dict[str, str]:
    if isinstance(exc, asyncio.CancelledError):
        return _error("cancelled", "TRANSCRIPT_CANCELLED", "文案提取已取消", "可重新点击提取口播")
    if isinstance(exc, ModelRequestError):
        if exc.status_code == 429:
            return _error(
                exc.stage,
                "MODEL_RATE_LIMITED",
                f"模型当前限流（HTTP 429）：{exc.detail}",
                "请等待几分钟后重试；不要连续重复提交",
            )
        if exc.status_code in {502, 503, 504}:
            return _error(
                exc.stage,
                "MODEL_UPSTREAM_UNAVAILABLE",
                f"模型网关暂时不可用（HTTP {exc.status_code}）：{exc.detail}",
                "稍后重试；若持续失败，请检查模型服务上游状态",
            )
        if exc.status_code in {401, 403}:
            return _error(
                exc.stage,
                "MODEL_AUTH_FAILED",
                f"模型鉴权失败（HTTP {exc.status_code}）",
                "检查 API Key 是否有效后重试",
            )
        if exc.status_code == 400:
            return _error(
                exc.stage,
                "MODEL_REQUEST_INVALID",
                f"模型请求格式被拒绝（HTTP 400）：{exc.detail}",
                "检查转写模型是否支持音频输入",
            )
        return _error(
            exc.stage,
            "MODEL_HTTP_ERROR",
            f"模型请求失败（HTTP {exc.status_code}）：{exc.detail}",
            "检查模型配置和网络后重试",
        )
    if isinstance(exc, httpx.TransportError):
        return _error(
            "transcription",
            "MODEL_TRANSPORT_ERROR",
            "模型服务连接中断，已达到自动重试上限",
            "检查网络或模型服务状态后重试",
        )
    return _error("transcription", "TRANSCRIPT_FAILED", str(exc) or type(exc).__name__, "检查 ffmpeg、模型配置和网络后重试")
