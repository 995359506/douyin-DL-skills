"""Direct full-video summarization through a Gemini-compatible generateContent API."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import uuid
import shutil
import tempfile
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx

try:
    from .transcription_service import _keychain_secret
except ImportError:
    from transcription_service import _keychain_secret


ProgressCallback = Callable[[str, int], Awaitable[None]]


_COMMENT_IDEA_STYLES = frozenset({"搞笑有梗", "抒情共鸣", "金句"})
_COMMENT_IDEAS_GUIDANCE = (
    "comment_ideas 是独立的神评论灵感创作区，元素必须是 {\"style\":\"搞笑有梗|抒情共鸣|金句\",\"text\":\"可直接发布的短评论\"}，"
    "style 只能选择其中一种。通常给 6 条，每类 2 条；某类不适合原片时可以少给，最多 6 条。"
    "评论必须贴合原片具体细节，语气自然：搞笑有梗、抒情共鸣或精练金句，避免套话和重复。"
    "优先一句话、10-45 个中文字，像观众随手留下的评论；搞笑类抓反差、巧妙接梗，抒情类抓细节和共鸣，金句类要精练有记忆点，避免复述摘要、万能夸赞和强行玩梗。"
    "这是基于原片的 AI 原创候选，不是真实评论或原话，不虚构人物经历，不保证高赞；必须与事实分析区分。"
)


def normalize_comment_ideas(value: Any) -> list[dict[str, str]]:
    """保留可直接展示的原创评论候选，兼容没有该字段的旧总结。"""
    if not isinstance(value, list):
        return []
    ideas: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        style, text = entry.get("style"), entry.get("text")
        if not isinstance(style, str) or not isinstance(text, str):
            continue
        style, text = style.strip(), text.strip()
        if style not in _COMMENT_IDEA_STYLES or not text or text in seen:
            continue
        ideas.append({"style": style, "text": text})
        seen.add(text)
        if len(ideas) == 6:
            break
    return ideas


@dataclass(frozen=True)
class SummaryConfig:
    gateway_base_url: str = "https://api.aigateway.online/v1beta"
    gateway_api_key: str = ""
    gateway_model: str = "gemini-3.8-flash"
    base_url: str = "https://api.text168.com/v1beta"
    api_key: str = ""
    model: str = "gemini-3.7-flash"
    fallback_base_url: str = "https://ai.image.mohuajz.eu.org/v1beta"
    fallback_api_key: str = ""
    fallback_model: str = "gemini-2.5-flash"
    timeout_seconds: float = 600.0
    request_retries: int = 3
    retry_backoff_seconds: float = 1.0
    # Raw video target. Base64 expands the request, so leave room below the
    # provider's total inline request limit.
    max_video_bytes: int = 8 * 1024 * 1024
    inline_request_bytes: int = 20 * 1024 * 1024
    # Keep inline base64 requests comfortably below common gateway body limits.
    segment_bytes: int = 8 * 1024 * 1024
    max_segments: int = 12
    min_video_bitrate: int = 220_000
    max_dimension: int = 854
    target_fps: int = 15
    audio_bitrate: int = 48_000

    @classmethod
    def from_environment(cls) -> "SummaryConfig":
        gateway_api_key = os.getenv("DOUYIN_VIDEO_SUMMARY_GATEWAY_API_KEY", "").strip()
        if not gateway_api_key:
            gateway_api_key = _keychain_secret("douyin-downloader-summary-aigateway")
        api_key = os.getenv("DOUYIN_VIDEO_SUMMARY_API_KEY", "").strip()
        if not api_key:
            api_key = _keychain_secret("douyin-downloader-text168")
        fallback_api_key = os.getenv("DOUYIN_VIDEO_SUMMARY_FALLBACK_API_KEY", "").strip()
        if not fallback_api_key:
            fallback_api_key = _keychain_secret("douyin-downloader-summary-fallback")
        return cls(
            gateway_base_url=os.getenv("DOUYIN_VIDEO_SUMMARY_GATEWAY_BASE_URL", "https://api.aigateway.online/v1beta").strip().rstrip("/"),
            gateway_api_key=gateway_api_key,
            gateway_model=os.getenv("DOUYIN_VIDEO_SUMMARY_GATEWAY_MODEL", "gemini-3.8-flash").strip(),
            base_url=os.getenv("DOUYIN_VIDEO_SUMMARY_BASE_URL", "https://api.text168.com/v1beta").strip().rstrip("/"),
            api_key=api_key,
            model=os.getenv("DOUYIN_VIDEO_SUMMARY_MODEL", "gemini-3.7-flash").strip(),
            fallback_base_url=os.getenv("DOUYIN_VIDEO_SUMMARY_FALLBACK_BASE_URL", "https://ai.image.mohuajz.eu.org/v1beta").strip().rstrip("/"),
            fallback_api_key=fallback_api_key,
            fallback_model=os.getenv("DOUYIN_VIDEO_SUMMARY_FALLBACK_MODEL", "gemini-2.5-flash").strip(),
            timeout_seconds=float(os.getenv("DOUYIN_VIDEO_SUMMARY_TIMEOUT_SECONDS", "600")),
            request_retries=max(0, int(os.getenv("DOUYIN_VIDEO_SUMMARY_REQUEST_RETRIES", "3"))),
            retry_backoff_seconds=max(0.0, float(os.getenv("DOUYIN_VIDEO_SUMMARY_RETRY_BACKOFF_SECONDS", "1"))),
            max_video_bytes=int(os.getenv("DOUYIN_VIDEO_SUMMARY_MAX_BYTES", str(8 * 1024 * 1024))),
            inline_request_bytes=int(os.getenv("DOUYIN_VIDEO_SUMMARY_INLINE_MAX_BYTES", str(20 * 1024 * 1024))),
            segment_bytes=int(os.getenv("DOUYIN_VIDEO_SUMMARY_SEGMENT_BYTES", str(8 * 1024 * 1024))),
            max_segments=int(os.getenv("DOUYIN_VIDEO_SUMMARY_MAX_SEGMENTS", "12")),
            min_video_bitrate=int(os.getenv("DOUYIN_VIDEO_SUMMARY_MIN_VIDEO_BITRATE", "220000")),
            max_dimension=int(os.getenv("DOUYIN_VIDEO_SUMMARY_MAX_DIMENSION", "854")),
            target_fps=int(os.getenv("DOUYIN_VIDEO_SUMMARY_TARGET_FPS", "15")),
            audio_bitrate=int(os.getenv("DOUYIN_VIDEO_SUMMARY_AUDIO_BITRATE", "48000")),
        )


@dataclass(frozen=True)
class VideoProbe:
    duration_seconds: float
    width: int
    height: int
    has_audio: bool


@dataclass(frozen=True)
class VideoPreparation:
    mode: str
    original_path: Path
    prepared_paths: list[Path]
    original_bytes: int
    prepared_bytes: int

    @property
    def segment_count(self) -> int:
        return len(self.prepared_paths)


class SummaryModelError(RuntimeError):
    def __init__(self, status_code: int, detail: str, model: str, retry_after_seconds: Optional[int] = None, provider: str = "", host: str = ""):
        self.status_code = status_code
        self.detail = detail or "模型服务返回错误"
        self.model = model
        self.retry_after_seconds = retry_after_seconds
        self.provider = provider
        self.host = host
        self.fallback_configured = False
        self.attempts: list[Dict[str, Any]] = []
        super().__init__(f"视频总结模型请求失败（HTTP {status_code}）：{self.detail}")


class SummaryPreparationError(RuntimeError):
    def __init__(self, code: str, detail: str, action: str):
        self.code = code
        self.detail = detail
        self.action = action
        super().__init__(detail)


class SummaryTransportError(RuntimeError):
    """The provider connection was interrupted before an HTTP response arrived."""

    def __init__(self, detail: str, model: str, provider: str = "", host: str = ""):
        self.detail = detail or "模型服务连接中断"
        self.model = model
        self.provider = provider
        self.host = host
        super().__init__(self.detail)


class SummaryResponseError(RuntimeError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _error(stage: str, code: str, reason: str, action: str) -> Dict[str, str]:
    return {"stage": stage, "code": code, "reason": reason, "action": action}


class VideoSummaryService:
    VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
    TRUSTED_HOSTS = {"api.aigateway.online", "ai.image.mohuajz.eu.org", "api.text168.com", "localhost", "127.0.0.1"}

    def __init__(self, config: Optional[SummaryConfig] = None):
        self.config = config or SummaryConfig.from_environment()
        self._cancel_events: Dict[str, asyncio.Event] = {}
        self._job_provider_indexes: Dict[str, int] = {}
        self._job_attempts: Dict[str, list[Dict[str, Any]]] = {}
        self._job_segment_indexes: Dict[str, int] = {}
        self._concurrency = asyncio.Semaphore(5)

    def validate_config(self) -> None:
        providers = self._providers()
        if not providers:
            raise RuntimeError("未配置视频总结模型服务地址和 API Key")
        for provider in providers:
            self._validate_provider(provider)
        if self.config.max_video_bytes <= 0 or self.config.max_video_bytes >= self.config.inline_request_bytes:
            raise RuntimeError("视频总结大小阈值配置无效")
        if self.config.segment_bytes <= 0 or self.config.segment_bytes > self.config.max_video_bytes:
            raise RuntimeError("视频总结分段大小配置无效")

    def _providers(self) -> list[Dict[str, str]]:
        providers: list[Dict[str, str]] = []
        if self.config.gateway_base_url and self.config.gateway_api_key:
            providers.append({"name": "gateway", "base_url": self.config.gateway_base_url, "api_key": self.config.gateway_api_key, "model": self.config.gateway_model})
        if self.config.base_url and self.config.api_key:
            providers.append({"name": "primary", "base_url": self.config.base_url, "api_key": self.config.api_key, "model": self.config.model})
        if self.config.fallback_base_url and self.config.fallback_api_key:
            providers.append({"name": "fallback", "base_url": self.config.fallback_base_url, "api_key": self.config.fallback_api_key, "model": self.config.fallback_model})
        return providers

    def _validate_provider(self, provider: Dict[str, str]) -> None:
        parsed = urlparse(provider["base_url"])
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or host not in self.TRUSTED_HOSTS:
            raise RuntimeError("视频总结服务地址不在可信白名单；允许配置的 Gemini 兼容网关或本机网关")
        if parsed.scheme != "https" and host not in {"localhost", "127.0.0.1"}:
            raise RuntimeError("公网视频总结服务必须使用 HTTPS；仅允许本机网关使用 HTTP")
        if not provider["model"]:
            raise RuntimeError("未配置视频总结模型名称")

    def cancel(self, job_id: str) -> None:
        event = self._cancel_events.get(job_id)
        if event:
            event.set()

    def _check_cancelled(self, job_id: str) -> None:
        event = self._cancel_events.get(job_id)
        if event and event.is_set():
            raise asyncio.CancelledError

    async def _update(self, callback: Optional[ProgressCallback], stage: str, progress: int) -> None:
        if callback:
            await callback(stage, max(0, min(100, progress)))

    def _validate_video(self, video_path: Path) -> None:
        if not video_path.exists() or not video_path.is_file():
            raise FileNotFoundError("找不到任务对应的本地视频文件")
        if video_path.suffix.lower() not in self.VIDEO_SUFFIXES:
            raise SummaryPreparationError(
                "SUMMARY_UNSUPPORTED_VIDEO",
                "任务文件不是受支持的视频格式",
                "确认下载结果为 MP4、MOV、MKV、AVI 或 WebM 后重试",
            )
        size = video_path.stat().st_size
        if size <= 0:
            raise SummaryPreparationError("SUMMARY_EMPTY_VIDEO", "视频文件为空", "确认视频已完整下载后重试")

    @staticmethod
    def _mime_type(video_path: Path) -> str:
        return {
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".mkv": "video/x-matroska",
            ".avi": "video/x-msvideo",
            ".webm": "video/webm",
        }.get(video_path.suffix.lower(), "video/mp4")

    async def _probe_video(self, video_path: Path) -> VideoProbe:
        if shutil.which("ffprobe") is None:
            raise SummaryPreparationError("SUMMARY_FFPROBE_MISSING", "本机未安装 ffprobe，无法检查视频", "安装 ffmpeg 后重试")
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-show_entries", "stream=codec_type,width,height", "-of", "json", str(video_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="ignore")[-500:]
            raise SummaryPreparationError(
                "SUMMARY_VIDEO_PROBE_FAILED",
                f"视频预检失败：{detail or 'ffprobe 返回非零状态'}",
                "确认视频文件可播放后重试",
            )
        try:
            payload = json.loads(stdout.decode("utf-8", errors="ignore"))
            duration = float((payload.get("format") or {}).get("duration") or 0)
            streams = payload.get("streams") or []
            video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
            if not video_stream or duration <= 0:
                raise ValueError
            return VideoProbe(
                duration_seconds=duration,
                width=int(video_stream.get("width") or 0),
                height=int(video_stream.get("height") or 0),
                has_audio=any(item.get("codec_type") == "audio" for item in streams),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SummaryPreparationError(
                "SUMMARY_VIDEO_PROBE_INVALID", "无法读取视频时长或视频流信息", "确认视频文件可播放后重试"
            ) from exc

    async def _run_ffmpeg(self, job_id: str, args: list[str]) -> None:
        if shutil.which("ffmpeg") is None:
            raise SummaryPreparationError("SUMMARY_FFMPEG_MISSING", "本机未安装 ffmpeg，无法压缩视频", "安装 ffmpeg 后重试")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stderr_task = asyncio.create_task(proc.stderr.read())
        wait_task = asyncio.create_task(proc.wait())
        try:
            while not wait_task.done():
                self._check_cancelled(job_id)
                try:
                    await asyncio.wait_for(asyncio.shield(wait_task), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
            return_code = await wait_task
            stderr = await stderr_task
        except BaseException:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
            await stderr_task
            raise
        if return_code != 0:
            detail = stderr.decode("utf-8", errors="ignore")[-700:]
            raise SummaryPreparationError(
                "SUMMARY_TRANSCODE_FAILED",
                f"视频压缩失败：{detail or 'ffmpeg 返回非零状态'}",
                "检查视频编码格式和本机磁盘空间后重试",
            )

    def _scale_filter(self, max_dimension: int, fps: int) -> str:
        return f"scale={max_dimension}:{max_dimension}:force_original_aspect_ratio=decrease:force_divisible_by=2,fps={fps}"

    def _encode_args(
        self,
        input_path: Path,
        output_path: Path,
        video_bitrate: int,
        max_dimension: int,
        fps: int,
        pass_number: int,
        passlog: Path,
        start: float | None = None,
        duration: float | None = None,
    ) -> list[str]:
        args: list[str] = ["-y"]
        if start is not None:
            args.extend(["-ss", f"{start:.3f}"])
        args.extend(["-i", str(input_path)])
        if duration is not None:
            args.extend(["-t", f"{duration:.3f}"])
        args.extend([
            "-map", "0:v:0", "-map", "0:a:0?", "-vf", self._scale_filter(max_dimension, fps),
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(video_bitrate),
            "-maxrate", str(video_bitrate), "-bufsize", str(video_bitrate * 2),
            "-pix_fmt", "yuv420p", "-r", str(fps), "-pass", str(pass_number),
            "-passlogfile", str(passlog),
        ])
        if pass_number == 1:
            args.extend(["-an", "-f", "null", "/dev/null"])
        else:
            args.extend([
                "-c:a", "aac", "-b:a", str(self.config.audio_bitrate), "-ac", "1", "-ar", "16000",
                "-movflags", "+faststart", str(output_path),
            ])
        return args

    def _target_bitrate(self, duration: float, target_bytes: int, audio_enabled: bool, factor: float = 1.0) -> int:
        total_bps = (target_bytes * 8 * 0.93) / max(duration, 1.0)
        audio_bps = self.config.audio_bitrate if audio_enabled else 0
        return max(40_000, int((total_bps - audio_bps) * factor))

    async def _transcode_once(
        self, job_id: str, input_path: Path, output_path: Path, probe: VideoProbe,
        target_bytes: int, max_dimension: int, fps: int, factor: float, passlog: Path,
        start: float | None = None, duration: float | None = None,
    ) -> None:
        effective_duration = duration if duration is not None else probe.duration_seconds
        bitrate = self._target_bitrate(effective_duration, target_bytes, probe.has_audio, factor)
        await self._run_ffmpeg(job_id, self._encode_args(input_path, output_path, bitrate, max_dimension, fps, 1, passlog, start, duration))
        await self._run_ffmpeg(job_id, self._encode_args(input_path, output_path, bitrate, max_dimension, fps, 2, passlog, start, duration))

    async def _transcode_to_target(
        self, job_id: str, input_path: Path, probe: VideoProbe, temp_dir: Path,
        target_bytes: int, callback: Optional[ProgressCallback], purpose: str = "full-video",
    ) -> Optional[Path]:
        profiles = [
            (self.config.max_dimension, self.config.target_fps, 1.0),
            (self.config.max_dimension, max(10, self.config.target_fps - 3), 0.82),
            (min(640, self.config.max_dimension), 12, 0.68),
        ]
        for index, (dimension, fps, factor) in enumerate(profiles):
            self._check_cancelled(job_id)
            output_path = temp_dir / f"{purpose}-{index}.mp4"
            passlog = temp_dir / f"{purpose}-pass-{index}"
            await self._update(callback, "compressing_video", 12 + index * 8)
            await self._transcode_once(job_id, input_path, output_path, probe, target_bytes, dimension, fps, factor, passlog)
            if output_path.exists() and output_path.stat().st_size <= target_bytes:
                return output_path
            output_path.unlink(missing_ok=True)
        return None

    async def _prepare_segments(
        self, job_id: str, input_path: Path, probe: VideoProbe, temp_dir: Path,
        callback: Optional[ProgressCallback],
    ) -> VideoPreparation:
        count = max(2, min(self.config.max_segments, math.ceil(input_path.stat().st_size / self.config.segment_bytes)))
        segment_duration = probe.duration_seconds / count
        paths: list[Path] = []
        for index in range(count):
            self._check_cancelled(job_id)
            start = index * segment_duration
            duration = probe.duration_seconds - start if index == count - 1 else segment_duration
            output = temp_dir / f"segment-{index:02d}.mp4"
            passlog = temp_dir / f"segment-pass-{index:02d}"
            for dimension, fps, factor in [
                (self.config.max_dimension, self.config.target_fps, 1.0),
                (min(640, self.config.max_dimension), 12, 0.68),
            ]:
                await self._run_ffmpeg(job_id, self._encode_args(input_path, output, self._target_bitrate(duration, self.config.segment_bytes, probe.has_audio, factor), dimension, fps, 1, passlog, start, duration))
                await self._run_ffmpeg(job_id, self._encode_args(input_path, output, self._target_bitrate(duration, self.config.segment_bytes, probe.has_audio, factor), dimension, fps, 2, passlog, start, duration))
                if output.exists() and output.stat().st_size <= self.config.segment_bytes:
                    break
                output.unlink(missing_ok=True)
            if not output.exists():
                raise SummaryPreparationError("SUMMARY_SEGMENT_TOO_LARGE", f"第 {index + 1} 段视频无法压缩到安全大小", "缩短视频或降低原视频清晰度后重试")
            paths.append(output)
            await self._update(callback, "compressing_segments", 15 + int((index + 1) / count * 25))
        return VideoPreparation("chunked", input_path, paths, input_path.stat().st_size, sum(item.stat().st_size for item in paths))

    async def _prepare_video(self, job_id: str, video_path: Path, temp_dir: Path, callback: Optional[ProgressCallback]) -> VideoPreparation:
        self._validate_video(video_path)
        original_bytes = video_path.stat().st_size
        await self._update(callback, "checking_video", 5)
        probe = await self._probe_video(video_path)
        if original_bytes <= self.config.max_video_bytes:
            await self._update(callback, "ready_to_upload", 30)
            return VideoPreparation("direct", video_path, [video_path], original_bytes, original_bytes)
        estimated_bitrate = self._target_bitrate(
            probe.duration_seconds,
            self.config.max_video_bytes,
            probe.has_audio,
        )
        if estimated_bitrate < self.config.min_video_bitrate:
            return await self._prepare_segments(job_id, video_path, probe, temp_dir, callback)
        compressed = await self._transcode_to_target(job_id, video_path, probe, temp_dir, self.config.max_video_bytes, callback)
        if compressed:
            await self._update(callback, "ready_to_upload", 40)
            return VideoPreparation("transcoded", video_path, [compressed], original_bytes, compressed.stat().st_size)
        return await self._prepare_segments(job_id, video_path, probe, temp_dir, callback)

    def _summary_prompt(self, segment_offset: float = 0.0, segment_count: int = 1) -> str:
        prefix = "这是完整视频。" if segment_count == 1 else f"这是完整视频从第 {segment_offset:.1f} 秒开始的一段连续片段，共 {segment_count} 段之一。请把时间写成原视频绝对秒数。"
        return (
            prefix + "请做一份有细节、可复核、适合内容研究的中文视频分析，不要只写泛泛的摘要。"
            "按视频实际顺序覆盖开场、发展、转折/冲突、高潮、结尾；说明人物做了什么、画面如何变化、"
            "听到了哪些口播/对白、剪辑和表达如何服务主题。除 comment_ideas 创作区外，只陈述视频中看见或听见的内容，无法确认的地方明确写入 uncertainties，绝不补写常识或猜测。"
            "请严格只返回 JSON，不要 Markdown 代码块。字段必须包含："
            "title（准确标题）、one_sentence（一句话概括）、overview（至少 3 段；在信息足够时不少于 180 个中文字符，"
            "按起因-经过-结果完整叙述）、"
            "key_points（6-10 条具体事实或观点，每条说明主体和动作/结果）、"
            "timeline（6-12 条；每条包含原视频绝对秒数 time、画面 visual、动作 action、口播/声音 audio、意义 significance；"
            "若片段信息不足则按实际数量返回）、"
            "visual_content（6-12 条具体画面变化，包含景别/人物/物品/环境/动作等可观察细节）、"
            "spoken_content（按顺序记录听到的完整口播、对白、字幕或声音；听不清则标注）、"
            "people_and_objects（数组；每项包含 name、role_or_relation、appearance_or_action）、"
            "narrative_structure（对象，包含 hook、setup、development、turning_point、climax、ending）、"
            "emotional_arc（按顺序描述情绪变化及其画面/声音依据）、"
            "editing_and_expression（镜头、节奏、转场、字幕、音乐、叙事视角等具体手法数组）、"
            "audience_and_intent（对象，包含 likely_audience、communication_intent、tone；仅在视频有依据时填写）、"
            "reusable_elements（可复用的选题、开场、表达或拍摄要素；每项必须能对应原片事实）、"
            "keywords（关键词数组）、uncertainties（无法确认的信息数组）。"
            "JSON 形状示例（内容仅作字段示意，必须用真实视频事实替换）："
            '{"key_points":["具体观点"],"timeline":[{"time":1,"visual":"画面","action":"动作","audio":"声音","significance":"作用"}],'
            '"visual_content":["可见变化"],"spoken_content":["听到的内容"],'
            '"people_and_objects":[{"name":"人物","role_or_relation":"关系","appearance_or_action":"动作"}],'
            '"narrative_structure":{"hook":"开场","setup":"背景","development":"发展","turning_point":"转折","climax":"高潮","ending":"结尾"},'
            '"emotional_arc":["平静到紧张，依据是画面与声音"],"editing_and_expression":["具体剪辑手法"],'
            '"audience_and_intent":{"likely_audience":"受众","communication_intent":"意图","tone":"语气"},'
            '"reusable_elements":["有原片依据的要素"],"keywords":["关键词"],"uncertainties":["无法确认事项"],'
            '"comment_ideas":[{"style":"搞笑有梗","text":"贴合具体画面的原创短评论"},'
            '{"style":"抒情共鸣","text":"由原片细节引发的原创感受"},{"style":"金句","text":"精练的原创表达"}]}。'
            + _COMMENT_IDEAS_GUIDANCE
        )

    async def _request_generate(self, job_id: str, video_path: Optional[Path], prompt: str) -> Dict[str, Any]:
        self._check_cancelled(job_id)
        parts: List[Dict[str, Any]] = []
        if video_path is not None:
            encoded = base64.b64encode(video_path.read_bytes()).decode("ascii")
            parts.append({"inlineData": {"mimeType": self._mime_type(video_path), "data": encoded}})
        parts.append({"text": prompt})
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }
        timeout = httpx.Timeout(self.config.timeout_seconds, connect=20.0)
        providers = self._providers()
        if not providers:  # validate_config normally catches this before a request.
            raise RuntimeError("未配置视频总结模型服务")
        preferred = self._job_provider_indexes.get(job_id, 0)
        ordered = providers[preferred:] + providers[:preferred]
        last_error: Optional[BaseException] = None
        for provider_index, provider in enumerate(ordered):
            attempt_info = {"provider": provider["name"], "model": provider["model"], "host": urlparse(provider["base_url"]).hostname}
            if job_id in self._job_segment_indexes:
                attempt_info["segment_index"] = self._job_segment_indexes[job_id]
            try:
                response = await self._request_provider(
                    job_id, provider, payload, timeout,
                )
                self._job_attempts.setdefault(job_id, []).append({**attempt_info, "status": 200})
                actual_index = (preferred + provider_index) % len(providers)
                self._job_provider_indexes[job_id] = actual_index
                return response
            except (SummaryModelError, SummaryTransportError) as exc:
                attempts = self._job_attempts.setdefault(job_id, [])
                attempts.append({**attempt_info, "status": getattr(exc, "status_code", None)})
                if isinstance(exc, SummaryModelError):
                    exc.fallback_configured = any(item["name"] == "fallback" for item in providers)
                    exc.attempts = list(attempts)
                last_error = exc
                if provider_index + 1 < len(ordered):
                    continue
                raise
        raise last_error or RuntimeError("视频总结模型请求失败")

    async def _request_provider(
        self,
        job_id: str,
        provider: Dict[str, str],
        payload: Dict[str, Any],
        timeout: httpx.Timeout,
    ) -> Dict[str, Any]:
        request_url = f"{provider['base_url']}/models/{provider['model']}:generateContent"
        last_error: Optional[BaseException] = None
        for attempt in range(self.config.request_retries + 1):
            self._check_cancelled(job_id)
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    response = await client.post(
                        request_url,
                        headers={"x-goog-api-key": provider["api_key"], "Content-Type": "application/json"},
                        json=payload,
                    )
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self.config.request_retries:
                    detail = str(exc) or type(exc).__name__
                    raise SummaryTransportError(detail, provider["model"], provider["name"], urlparse(provider["base_url"]).hostname or "") from exc
                await asyncio.sleep(self.config.retry_backoff_seconds * (2**attempt))
                continue
            if response.status_code < 400:
                break
            try:
                body = response.json()
                detail = body.get("message") or body.get("error", {}).get("message") or body.get("code")
            except (AttributeError, TypeError, ValueError):
                detail = None
            retry_after = None
            if response.status_code == 429:
                try:
                    retry_after = max(0, int(response.headers.get("retry-after", "0"))) or None
                except (AttributeError, TypeError, ValueError):
                    retry_after = None
            if (
                response.status_code in {408, 425, 429, 500, 502, 503, 504}
                and attempt < self.config.request_retries
                and (retry_after is None or retry_after <= 60)
            ):
                delay = retry_after if retry_after is not None else self.config.retry_backoff_seconds * (2**attempt)
                await asyncio.sleep(delay)
                continue
            raise SummaryModelError(response.status_code, detail or "模型服务返回错误", provider["model"], retry_after, provider["name"], urlparse(provider["base_url"]).hostname or "")
        else:  # pragma: no cover - loop always either breaks or raises
            raise SummaryTransportError(str(last_error or "模型服务连接中断"), provider["model"], provider["name"], urlparse(provider["base_url"]).hostname or "")
        self._check_cancelled(job_id)
        return response.json()

    async def _summarize_segments(self, job_id: str, preparation: VideoPreparation, callback: Optional[ProgressCallback]) -> Dict[str, Any]:
        summaries: List[Dict[str, Any]] = []
        offset = 0.0
        for index, path in enumerate(preparation.prepared_paths):
            self._check_cancelled(job_id)
            await self._update(callback, "analyzing_segments", 45 + int(index / max(preparation.segment_count, 1) * 35))
            self._job_segment_indexes[job_id] = index + 1
            try:
                summary = await self._generate_parsed(job_id, path, self._summary_prompt(offset, preparation.segment_count))
            finally:
                self._job_segment_indexes.pop(job_id, None)
            summaries.append(summary)
            try:
                offset += (await self._probe_video(path)).duration_seconds
            except SummaryPreparationError:
                pass
        combine_prompt = (
            "请把下面多个连续视频片段的 JSON 分析合并成一份详细、去重、按原视频时间排序的中文视频总结。"
            "保留并补全完整视频的起因-经过-结果、转折、情绪变化、镜头与声音表达；overview 写成连贯的多段文字，"
            "timeline 合并为具体时间线，事实分析的所有结论只能来自片段内容。只返回与片段相同字段的 JSON，不要添加 Markdown。"
            "comment_ideas 必须围绕完整视频重新整理、去重，不要直接堆叠各片段的评论。"
            + _COMMENT_IDEAS_GUIDANCE +
            "片段分析如下：\n" + json.dumps(summaries, ensure_ascii=False)
        )
        await self._update(callback, "combining_segments", 85)
        return await self._generate_parsed(job_id, None, combine_prompt)

    async def _generate_parsed(self, job_id: str, video_path: Optional[Path], prompt: str) -> Dict[str, Any]:
        for attempt in range(self.config.request_retries + 1):
            response = await self._request_generate(job_id, video_path, prompt)
            try:
                return _parse_summary_response(response, self.config.model)
            except SummaryResponseError as exc:
                if exc.code == "SUMMARY_MODEL_DEPRECATED" or attempt >= self.config.request_retries:
                    raise
                await asyncio.sleep(self.config.retry_backoff_seconds * (2**attempt))
        raise AssertionError("总结解析重试意外结束")

    async def summarize_video(self, job_id: str, video_path: Path, callback: Optional[ProgressCallback] = None, preparation_callback: Optional[Callable[[VideoPreparation], Awaitable[None]]] = None) -> Dict[str, Any]:
        self.validate_config()
        self._cancel_events[job_id] = asyncio.Event()
        try:
            async with self._concurrency:
                self._check_cancelled(job_id)
                with tempfile.TemporaryDirectory(prefix="douyin-summary-") as temp_dir_name:
                    preparation = await self._prepare_video(job_id, video_path, Path(temp_dir_name), callback)
                    if preparation_callback:
                        await preparation_callback(preparation)
                    if preparation.mode == "chunked":
                        result = await self._summarize_segments(job_id, preparation, callback)
                    else:
                        await self._update(callback, "analyzing_video", 55)
                        try:
                            result = await self._generate_parsed(job_id, preparation.prepared_paths[0], self._summary_prompt())
                        except SummaryTransportError:
                            # Large inline uploads can be dropped by a gateway during TLS.
                            # Retry with smaller sequential segments before surfacing the error.
                            await self._update(callback, "retrying_as_segments", 42)
                            segmented = await self._prepare_segments(job_id, video_path, await self._probe_video(video_path), Path(temp_dir_name), callback)
                            if preparation_callback:
                                await preparation_callback(segmented)
                            result = await self._summarize_segments(job_id, segmented, callback)
                    active_provider = self._providers()[self._job_provider_indexes.get(job_id, 0)]
                    result["_processing"] = {
                        "mode": preparation.mode,
                        "original_bytes": preparation.original_bytes,
                        "prepared_bytes": preparation.prepared_bytes,
                        "segment_count": preparation.segment_count,
                        "provider": active_provider["name"],
                        "model": active_provider["model"],
                    }
                    await self._update(callback, "completed", 100)
                    return result
        finally:
            self._cancel_events.pop(job_id, None)
            self._job_provider_indexes.pop(job_id, None)
            self._job_segment_indexes.pop(job_id, None)


def _response_text(response: Dict[str, Any]) -> str:
    chunks = []
    for candidate in response.get("candidates") or []:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def _parse_summary_response(response: Dict[str, Any], model: str = "gemini-3.7-flash") -> Dict[str, Any]:
    text = _response_text(response)
    if not text:
        raise SummaryModelError(502, "模型未返回总结文本", model)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        is_announcement = (
            len(re.findall(r"[\u4e00-\u9fff]", cleaned)) < 3
            and bool(re.search(r"no longer available|not supported|switch to|api key", cleaned, re.IGNORECASE))
        )
        if is_announcement:
            safe_notice = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[已隐藏密钥]", cleaned[:300])
            raise SummaryResponseError("SUMMARY_MODEL_DEPRECATED", safe_notice)
        raise SummaryResponseError("SUMMARY_MODEL_BAD_JSON", "模型返回的总结不是有效 JSON")
    if not isinstance(parsed, dict):
        raise SummaryResponseError("SUMMARY_MODEL_BAD_JSON", "模型返回的总结不是 JSON 对象")
    parsed.setdefault("title", "视频总结")
    parsed.setdefault("one_sentence", parsed.get("overview", ""))
    parsed.setdefault("overview", "")
    parsed["comment_ideas"] = normalize_comment_ideas(parsed.get("comment_ideas"))
    shape_issues = {}
    for field in ("key_points", "timeline", "visual_content", "spoken_content", "people_and_objects", "emotional_arc", "editing_and_expression", "reusable_elements", "keywords", "uncertainties"):
        if not isinstance(parsed.get(field), list):
            if field in parsed:
                shape_issues[field] = str(parsed[field])[:200]
            parsed[field] = []
    if not isinstance(parsed.get("narrative_structure"), dict):
        if "narrative_structure" in parsed:
            shape_issues["narrative_structure"] = str(parsed["narrative_structure"])[:200]
        parsed["narrative_structure"] = {}
    if not isinstance(parsed.get("audience_and_intent"), dict):
        if "audience_and_intent" in parsed:
            shape_issues["audience_and_intent"] = str(parsed["audience_and_intent"])[:200]
        parsed["audience_and_intent"] = {}
    if shape_issues:
        parsed["summary_shape_issues"] = shape_issues
    return parsed


def serialize_summary_error(exc: BaseException) -> Dict[str, str]:
    if isinstance(exc, asyncio.CancelledError):
        return _error("cancelled", "SUMMARY_CANCELLED", "视频总结已取消", "可重新点击视频总结")
    if isinstance(exc, FileNotFoundError):
        return _error("preparing_video", "SUMMARY_VIDEO_NOT_FOUND", str(exc), "确认视频仍在下载目录后重试")
    if isinstance(exc, SummaryPreparationError):
        return _error("preparing_video", exc.code, exc.detail, exc.action)
    if isinstance(exc, SummaryResponseError):
        action = "把 DOUYIN_VIDEO_SUMMARY_MODEL 或 _FALLBACK_MODEL 换成可用模型后重试" if exc.code == "SUMMARY_MODEL_DEPRECATED" else "稍后重试；持续失败时检查模型是否返回 JSON"
        return _error("analyzing_video", exc.code, exc.detail, action)
    if isinstance(exc, SummaryModelError):
        provider_label = f"{exc.provider or '模型通道'}（{exc.host or '未记录域名'}）HTTP {exc.status_code}"
        if not exc.fallback_configured:
            fallback_hint = "；备用接口未启用：请检查 Keychain 中 douyin-downloader-summary-fallback"
        elif any(item.get("provider") == "fallback" and item.get("status") != 200 for item in exc.attempts):
            fallback_hint = "；备用接口已尝试且失败"
        else:
            fallback_hint = "；备用接口已启用"
        if exc.status_code in {401, 403}:
            return _error("analyzing_video", "SUMMARY_MODEL_AUTH_FAILED", f"{provider_label} 鉴权失败：{exc.detail}", f"检查该通道的 API Key 和权限后重试{fallback_hint}")
        if exc.status_code == 413:
            return _error("analyzing_video", "SUMMARY_MODEL_PAYLOAD_TOO_LARGE", "模型服务拒绝了视频请求体大小", "降低视频清晰度或使用分段总结后重试")
        if exc.status_code == 429:
            wait_hint = f"建议等待约 {exc.retry_after_seconds} 秒后再试；" if exc.retry_after_seconds else "当前没有返回明确等待时间；"
            return _error(
                "analyzing_video",
                "SUMMARY_MODEL_RATE_LIMITED",
                "视频总结模型当前限流或配额不足",
                f"{wait_hint}请检查视频总结接口用量/限流状态，或更换可用的 API Key/模型通道，避免连续提交",
            )
        if exc.status_code in {502, 503, 504}:
            return _error("analyzing_video", "SUMMARY_MODEL_UNAVAILABLE", f"{provider_label} 上游不可用：{exc.detail}", f"检查该通道后重试{fallback_hint}")
        return _error("analyzing_video", "SUMMARY_MODEL_REQUEST_FAILED", f"视频总结模型请求失败（HTTP {exc.status_code}）：{exc.detail}", "检查视频格式和模型配置后重试")
    if isinstance(exc, SummaryTransportError):
        return _error(
            "analyzing_video",
            "SUMMARY_MODEL_TLS_FAILED",
            f"视频总结模型连接中断：{exc.detail}",
            "模型通道在上传视频时中断，系统已自动重试并切换备用接口/分段；请稍后重试或检查模型通道",
        )
    return _error("video_summary", "SUMMARY_FAILED", str(exc) or type(exc).__name__, "检查视频文件、模型配置和网络后重试")


def new_summary_job_id() -> str:
    return str(uuid.uuid4())
