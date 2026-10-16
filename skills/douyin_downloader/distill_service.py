"""博主内容蒸馏编排、归纳与 Markdown 产物服务。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

try:
    from .db import TaskDB
    from .logger import get_logger
    from .transcription_service import ModelRequestError, _keychain_secret
    from .video_summary_service import SummaryModelError, SummaryPreparationError
except ImportError:
    from db import TaskDB
    from logger import get_logger
    from transcription_service import ModelRequestError, _keychain_secret
    from video_summary_service import SummaryModelError, SummaryPreparationError


DownloadCallback = Callable[[Dict[str, Any]], Awaitable[Path]]
TranscriptCallback = Callable[[str, Path, Callable[[str, int], Awaitable[None]]], Awaitable[Dict[str, Any]]]
SummaryCallback = Callable[[str, Path, Callable[[str, int], Awaitable[None]]], Awaitable[Dict[str, Any]]]


RUN_TERMINAL = {"completed", "partial", "preview_ready", "cancelled", "failed"}
ITEM_TERMINAL = {"ready", "written", "degraded", "transcribed", "skipped", "low_signal", "summary_invalid", "failed", "cancelled"}


class DistillError(RuntimeError):
    """蒸馏阶段错误，包含是否可重试的信息。"""

    def __init__(self, code: str, reason: str, action: str, retryable: bool = False):
        self.code = code
        self.reason = reason
        self.action = action
        self.retryable = retryable
        super().__init__(reason)


def _now() -> str:
    return datetime.now().isoformat()


def _error(stage: str, code: str, reason: str, action: str) -> Dict[str, Any]:
    return {"stage": stage, "code": code, "reason": reason, "action": action}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _safe_component(value: str, fallback: str = "博主") -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", str(value or "").strip())
    value = re.sub(r"\.{2,}", ".", value).strip(" .")
    return (value[:100] or fallback)


def _source_id(item: Dict[str, Any]) -> str:
    return f"{int(item.get('ordinal') or 0):03d}"


def _normalized_quote(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(char.casefold() for char in text if not char.isspace() and not unicodedata.category(char).startswith("P") and char not in "*_~")


def _transcript_char_count(value: Any) -> int:
    return len(_normalized_quote(value))


def _low_signal_reason(item: Dict[str, Any], minimum: int) -> str:
    text = item.get("polished_text") or item.get("original_transcript") or ""
    count = _transcript_char_count(text)
    return "口播信息量不足（疑似纯音乐/翻唱/无解说画面），仅 {0} 字，低于 {1} 字阈值".format(count, minimum) if minimum and count and count < minimum else ""


def _summary_invalid_reason(summary: Any, minimum_cjk_ratio: float = 0.15) -> str:
    if not isinstance(summary, dict):
        return "画面理解结果不是对象"
    required = ("key_points", "timeline", "visual_content", "narrative_structure", "reusable_elements")
    present = sum(bool(summary.get(key)) for key in required)
    if present < 3:
        return f"画面理解结构不足：5 项关键字段仅 {present} 项非空"
    structure = summary.get("narrative_structure")
    if not isinstance(structure, dict) or sum(bool(value) for value in structure.values()) < 3:
        return "画面理解叙事结构不足：少于 3 个有效字段"
    if minimum_cjk_ratio > 0:
        body = " ".join(str(summary.get(key) or "") for key in ("title", "one_sentence", "overview"))
        body += " " + " ".join(str(value) for key in required for value in (summary.get(key) or []) if key != "narrative_structure")
        letters = [char for char in body if char.isalpha()]
        ratio = sum("\u4e00" <= char <= "\u9fff" for char in letters) / max(len(letters), 1)
        if ratio < minimum_cjk_ratio:
            return f"画面理解中文内容占比不足：{ratio:.0%}，要求至少 {minimum_cjk_ratio:.0%}"
    return ""


def _one_sentence(value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        return {"text": value, "sources": []}
    if value is None:
        return {"text": "", "sources": []}
    if isinstance(value, dict):
        for key in ("text", "summary", "content"):
            if isinstance(value.get(key), str) and value[key].strip():
                return {"text": value[key].strip(), "sources": value.get("sources") or []}
        return {"text": _json(value), "sources": value.get("sources") or [], "shape_unrecognized": True}
    return {"text": _json(value), "sources": [], "shape_unrecognized": True}


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, DistillError):
        return exc.retryable
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    status = getattr(exc, "status_code", None)
    return status in {408, 429, 500, 502, 503, 504}


def _error_from_exception(stage: str, exc: BaseException) -> Dict[str, Any]:
    if isinstance(exc, DistillError):
        return _error(stage, exc.code, exc.reason, exc.action)
    if isinstance(exc, (ModelRequestError, SummaryModelError)):
        status = getattr(exc, "status_code", 502)
        code = f"DISTILL_{stage.upper()}_HTTP_{status}"
        action = "稍后重试，避免连续提交" if status in {408, 429, 500, 502, 503, 504} else "检查模型配置与权限后重试"
        return _error(stage, code, str(exc), action)
    if isinstance(exc, SummaryPreparationError):
        return _error(stage, exc.code, exc.detail, exc.action)
    return _error(stage, f"DISTILL_{stage.upper()}_FAILED", str(exc) or type(exc).__name__, "检查本地文件、模型配置和网络后重试")


def _source_is_current(item: Dict[str, Any], path: Path, completed_at: Optional[str]) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    expected_size = item.get("source_size")
    if expected_size is not None and int(expected_size) != stat.st_size:
        return False
    if item.get("source_mtime_ns") is not None and int(item["source_mtime_ns"]) != stat.st_mtime_ns:
        return False
    if completed_at:
        try:
            completed = datetime.fromisoformat(completed_at)
            if datetime.fromtimestamp(stat.st_mtime).replace(tzinfo=completed.tzinfo) > completed:
                return False
        except (TypeError, ValueError, OSError):
            pass
    return True


class DistillMarkdown:
    """把蒸馏快照渲染为总纲和逐条独立 Markdown 文件。"""

    def __init__(self, root: Path, split_threshold: int = 50_000):
        self.root = root.resolve()
        self.split_threshold = max(1_000, int(split_threshold))

    @staticmethod
    def _summary(item: Dict[str, Any]) -> Dict[str, Any]:
        return item.get("summary") or {}

    @staticmethod
    def _clean_title(value: Any) -> str:
        """移除标题中的话题标签，避免把 #话题 当成正文标题。"""
        title = str(value or "").replace("\r", " ").replace("\n", " ")
        # 支持 #话题、#topic 和常见的“# 话题”写法；只处理标题字段。
        title = re.sub(r"#\s*[\w\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff·-]+", " ", title)
        title = re.sub(r"\s+", " ", title).strip(" \t-—_|:：,，。.!！?？")
        return title or "未命名作品"

    @classmethod
    def _item_filename(cls, item: Dict[str, Any]) -> str:
        meta = item.get("metadata") or {}
        title = cls._clean_title(meta.get("title") or meta.get("desc"))
        safe_title = _safe_component(title, "视频")[:70]
        aweme_id = _safe_component(str(item.get("aweme_id") or "未知ID"), "未知ID")
        return f"{int(item.get('ordinal') or 0):03d}-{safe_title}-{aweme_id}.md"

    @staticmethod
    def _source_label(item: Dict[str, Any]) -> str:
        meta = item.get("metadata") or {}
        title = DistillMarkdown._clean_title(meta.get("title") or meta.get("desc"))
        return f"{int(item.get('ordinal') or 0):03d}《{title[:100]}》"

    def _appendix(self, item: Dict[str, Any], split: bool = False) -> str:
        meta = item.get("metadata") or {}
        title = self._clean_title(meta.get("title") or meta.get("desc"))
        lines = [
            f"### {int(item.get('ordinal') or 0):03d}. {title}",
            f"- 发布时间：{meta.get('published_at') or meta.get('create_time') or '未知'}",
            f"- 类型：{meta.get('kind') or 'video'}",
            f"- 时长：{meta.get('duration_seconds') or '未知'} 秒",
            f"- 原链接：{meta.get('url') or '未知'}",
            f"- 本地文件：{item.get('local_path') or '未找到'}",
            f"- 状态：{item.get('item_status') or 'unknown'}",
        ]
        summary = self._summary(item)
        if item.get("degrade_reason"):
            lines.extend(["", f"> 降级说明：{item['degrade_reason']}"])
        if summary.get("key_points"):
            lines.extend(["", "**要点**", *[f"- {value}" for value in summary["key_points"]]])
        if summary.get("visual_content"):
            lines.extend(["", "**画面信息**", *[f"- {value}" for value in summary["visual_content"]]])
        if summary.get("timeline"):
            lines.extend(["", "**时间线**", *[
                f"- {entry.get('time', '')} " + "；".join(filter(None, [
                    entry.get('content', ''),
                    f"画面：{entry['visual']}" if entry.get('visual') else '',
                    f"动作：{entry['action']}" if entry.get('action') else '',
                    f"声音：{entry['audio']}" if entry.get('audio') else '',
                    f"作用：{entry['significance']}" if entry.get('significance') else '',
                ])) if isinstance(entry, dict) else f"- {entry}"
                for entry in summary["timeline"]
            ]])
        if summary.get("narrative_structure"):
            labels = {"hook": "开场钩子", "setup": "背景铺垫", "development": "发展", "turning_point": "转折/冲突", "climax": "高潮", "ending": "结尾"}
            lines.extend(["", "**叙事结构**", *[f"- {label}：{summary['narrative_structure'].get(key) or '未明确'}" for key, label in labels.items()]])
        if summary.get("people_and_objects"):
            lines.extend(["", "**人物与物品**", *[
                f"- {'：'.join(filter(None, [entry.get('name'), entry.get('role_or_relation'), entry.get('appearance_or_action')]))}" if isinstance(entry, dict) else f"- {entry}"
                for entry in summary["people_and_objects"]
            ]])
        if summary.get("emotional_arc"):
            lines.extend(["", "**情绪变化**", *[f"- {value}" for value in summary["emotional_arc"]]])
        if summary.get("editing_and_expression"):
            lines.extend(["", "**表达与剪辑**", *[f"- {value}" for value in summary["editing_and_expression"]]])
        if summary.get("reusable_elements"):
            lines.extend(["", "**可复用要素**", *[f"- {value}" for value in summary["reusable_elements"]]])
        is_image_text = (item.get("metadata") or {}).get("kind") == "image"
        lines.extend(["", "**来源类型：图文正文（kind=image_text）**" if is_image_text else "**口播文案（完整）**", item.get("polished_text") or "（未生成整理文案）"])
        lines.extend(["", "<details>", "<summary>原始转写（完整）</summary>", "", item.get("original_transcript") or "（未生成原始转写）", "", "</details>"])
        if not split:
            return "\n".join(lines)
        return "\n".join(lines)

    def _outline(self, run: Dict[str, Any], items: List[Dict[str, Any]], split: bool) -> str:
        valid = [item for item in items if item.get("item_status") in {"ready", "written", "degraded"}]
        omitted = [item for item in items if item.get("item_status") in {"transcribed", "skipped", "low_signal", "summary_invalid", "failed", "cancelled"}]
        total_words = sum(len((item.get("polished_text") or "")) for item in items)
        portrait = _one_sentence((run.get("synthesis") or {}).get("one_sentence"))
        lines = [
            f"# {run.get('nickname') or '博主'} 内容蒸馏",
            "",
            "> 生成时间：" + _now(),
            f"> 作品总数：{run.get('total_count', len(items))}",
            f"> 成功分析数：{len(valid)}",
            f"> 本次归纳基于 {len(valid)}/{run.get('total_count', len(items))} 条作品",
            f"> 跳过/失败数：{len(omitted)}",
            f"> 归纳模型：{run.get('synthesis_model') or 'gpt-5.6-luna'}",
            f"> 文案总字数：{total_words}",
            "",
            "## 一句话画像",
            portrait["text"] or "未生成跨视频画像。",
            "（来源：" + ", ".join(portrait["sources"]) + "）" if portrait["sources"] else "（来源：未标注来源）",
            "",
            "## 可复用手法库",
        ]
        synthesis = run.get("synthesis") or {}
        for key, label in (("hooks", "开场钩子"), ("endings", "结尾句式"), ("pacing", "节奏模板")):
            lines.extend(["", f"### {label}"])
            entries = sorted((synthesis.get("techniques") or {}).get(key) or [], key=lambda entry: len(entry.get("sources") or []), reverse=True)
            if entries:
                for entry in entries:
                    lines.append(f"- {entry.get('pattern') or ''}（来源：{', '.join(entry.get('sources') or [])}）{entry.get('note') or ''}")
            else:
                lines.append("本次资料中未归纳出可复用骨架。")
        lines.extend(["", "## 主题地图"])
        for theme in synthesis.get("themes") or []:
            source = ", ".join(theme.get("sources") or [])
            lines.append(f"- **{theme.get('name') or '未命名主题'}**：{theme.get('summary') or ''}（来源：{source or '未在原片中找到依据'}）")
        if not synthesis.get("themes"):
            lines.append("暂无足够材料形成主题地图。")
        lines.extend(["", "## 观点与方法论"])
        for claim in synthesis.get("methods") or []:
            source = ", ".join(claim.get("sources") or [])
            lines.append(f"- {claim.get('claim') or ''}（来源：{source or '未在原片中找到依据'}）")
        if not synthesis.get("methods"):
            lines.append("暂无可验证的方法论结论。")
        lines.extend(["", "## 演变与矛盾"])
        for claim in synthesis.get("contradictions") or []:
            source = ", ".join(claim.get("sources") or [])
            lines.append(f"- {claim.get('claim') or ''}（来源：{source or '未在原片中找到依据'}）")
        if not synthesis.get("contradictions"):
            lines.append("未发现有足够来源支持的明显演变或矛盾。")
        lines.extend(["", "## 值得复用的表达"])
        for quote in synthesis.get("expressions") or []:
            source = ", ".join(quote.get("sources") or [])
            lines.append(f"- “{quote.get('quote') or ''}”（来源：{source or '未在原片中找到依据'}）")
        if not synthesis.get("expressions"):
            lines.append("暂无经过原文校验的表达。")
        lines.extend(["", "## 逐条视频文件"])
        run_id = str(run.get("run_id") or "")
        for item in items:
            appendix_name = f"口播文案/{self._item_filename(item)}"
            item_href = f"/api/distill-runs/{run_id}/items/{item.get('aweme_id')}/view" if run_id else appendix_name
            meta = item.get("metadata") or {}
            title = self._clean_title(meta.get("title") or meta.get("desc"))
            excerpt = re.sub(r"\s+", " ", item.get("polished_text") or "")[:300]
            lines.extend(["", f"### {int(item.get('ordinal') or 0):03d}. {title}", f"- 状态：{item.get('item_status') or 'unknown'}", f"- 独立文件：[{appendix_name}]({item_href})", f"- 文案摘要：{excerpt or '暂无整理文案'}"])
        lines.extend(["", "## 本次未纳入"])
        for item in omitted:
            lines.append(f"- {self._source_label(item)}：{item.get('degrade_reason') or ((item.get('error') or {}).get('reason')) or '未完成分析'}")
        if not omitted:
            lines.append("无。")
        return "\n".join(lines) + "\n"

    def render(self, run: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, str]:
        # 每条视频始终独立落盘；split_threshold 仅保留为兼容旧调用方。
        split = True
        directory = self.root / "distill" / f"{_safe_component(run.get('nickname'), '博主')}-{str(run.get('sec_user_id') or '')[:8]}"
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M")
        output = directory / f"{timestamp}-蒸馏总纲.md"
        run_for_render = dict(run)
        run_for_render["synthesis"] = run.get("synthesis") or {}
        outline = self._outline(run_for_render, items, split)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp") as handle:
            handle.write(outline)
            temp_name = Path(handle.name)
        temp_name.replace(output)
        if split:
            appendix_dir = directory / "口播文案"
            appendix_dir.mkdir(exist_ok=True)
            for item in items:
                appendix = appendix_dir / self._item_filename(item)
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=appendix_dir, delete=False, suffix=".tmp") as handle:
                    handle.write(f"# {run.get('nickname') or '博主'} · {self._source_label(item)}\n\n{self._appendix(item, split=True)}\n")
                    temp_item = Path(handle.name)
                temp_item.replace(appendix)
        return {"path": str(output), "directory": str(directory), "split": "1", "per_item_files": "1"}

    @staticmethod
    def package(directory: Path, output_path: Path) -> Path:
        """Package an outline and its per-item files without recursively packaging ZIPs.

        The temporary archive lives outside the output directory so an interrupted
        or repeated download can never include itself (or a previous package).
        """
        package_path = output_path.parent / f"{output_path.stem}-完整包.zip"
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as handle:
            temp_zip = Path(handle.name)
        with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in directory.rglob("*"):
                if path.is_file() and path != output_path and path.suffix.lower() != ".zip":
                    archive.write(path, path.relative_to(directory))
            archive.write(output_path, output_path.relative_to(directory))
        temp_zip.replace(package_path)
        return package_path


class DistillSynthesis:
    """使用现有 OpenAI 兼容文本通道完成 map-reduce 归纳。"""

    def __init__(self, model: str = "gpt-5.6-luna", base_url: str = "", api_key: str = "", timeout: float = 300.0):
        self.model = model
        self.base_url = (base_url or os.getenv("DOUYIN_LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("DOUYIN_LLM_API_KEY", "") or _keychain_secret("douyin-downloader-llm")
        self.timeout = timeout
        self.max_chars = max(10_000, int(os.getenv("DOUYIN_DISTILL_MAP_MAX_CHARS", "60000")))
        self.response_meta: List[Dict[str, Any]] = []

    def validate(self) -> None:
        if not self.base_url or not self.api_key:
            raise DistillError("DISTILL_SYNTHESIS_CONFIG", "未配置博主归纳模型服务或 API Key", "检查 douyin-downloader-llm Keychain 配置")

    async def _chat(self, prompt: str) -> str:
        self.validate()
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=20.0)) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": 0},
            )
        if response.status_code >= 400:
            raise DistillError(f"DISTILL_SYNTHESIS_HTTP_{response.status_code}", f"博主归纳模型请求失败（HTTP {response.status_code}）", "稍后重试归纳", response.status_code in {408, 429, 500, 502, 503, 504})
        body = response.json()
        choices = body.get("choices") or []
        content = (choices[0].get("message") or {}).get("content", "") if choices else ""
        if isinstance(content, list):
            content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
        if not str(content).strip():
            raise DistillError("DISTILL_SYNTHESIS_EMPTY", "博主归纳模型未返回有效内容", "重试归纳")
        usage = body.get("usage") or {}
        safe_usage = {key: value for key, value in usage.items() if key in {"prompt_tokens", "completion_tokens", "total_tokens"} and isinstance(value, int)} if isinstance(usage, dict) else {}
        self.response_meta.append({"length": len(str(content)), "finish_reason": choices[0].get("finish_reason") if choices else None, "usage": safe_usage})
        get_logger().info("蒸馏归纳响应：序号=%s 长度=%s", len(self.response_meta), len(str(content)))
        return str(content).strip()

    @staticmethod
    def _parse(text: str) -> Dict[str, Any]:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            raise DistillError("DISTILL_SYNTHESIS_BAD_JSON", "博主归纳模型返回的 JSON 无法解析", "系统将按重试预算重新归纳", True)
        if not isinstance(data, dict):
            raise DistillError("DISTILL_SYNTHESIS_BAD_JSON", "博主归纳模型返回的不是 JSON 对象", "系统将按重试预算重新归纳", True)
        for key in ("themes", "methods", "contradictions", "expressions"):
            if not isinstance(data.get(key), list):
                data[key] = []
        if not isinstance(data.get("techniques"), dict):
            data["techniques"] = {}
        data.setdefault("one_sentence", "")
        return data

    @staticmethod
    def _validate(data: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, Any]:
        labels = {_source_id(item) for item in items}
        text_by_label = {_source_id(item): _normalized_quote(f"{item.get('polished_text') or ''}\n{item.get('original_transcript') or ''}") for item in items}
        audit = {"expressions": {"returned": len(data.get("expressions") or []), "kept": 0, "discarded": {"missing_quote": 0, "sources_mismatch": 0, "not_in_source": 0}}, "one_sentence": {}}
        audit["techniques"] = {"returned": 0, "kept": 0, "discarded": {"missing_pattern": 0, "sources_mismatch": 0}}
        techniques = {}
        for group in ("hooks", "endings", "pacing"):
            merged = {}
            for entry in (data.get("techniques") or {}).get(group) or []:
                audit["techniques"]["returned"] += 1
                pattern = str(entry.get("pattern") or "").strip() if isinstance(entry, dict) else ""
                if not pattern:
                    audit["techniques"]["discarded"]["missing_pattern"] += 1
                    continue
                sources = [source for source in entry.get("sources") or [] if source in labels]
                if not sources:
                    audit["techniques"]["discarded"]["sources_mismatch"] += 1
                    continue
                if pattern in merged:
                    merged[pattern]["sources"] = sorted(set(merged[pattern]["sources"] + sources))
                else:
                    merged[pattern] = {"pattern": pattern, "note": str(entry.get("note") or ""), "sources": sorted(set(sources))}
            techniques[group] = list(merged.values())
            audit["techniques"]["kept"] += len(techniques[group])
        data["techniques"] = techniques
        for key in ("themes", "methods", "contradictions", "expressions"):
            valid = []
            for entry in data.get(key) or []:
                if not isinstance(entry, dict):
                    if key == "expressions":
                        audit["expressions"]["discarded"]["missing_quote"] += 1
                    continue
                sources = [source for source in (entry.get("sources") or []) if source in labels]
                if key == "expressions":
                    quote = str(entry.get("quote") or "").strip()
                    if not quote:
                        audit["expressions"]["discarded"]["missing_quote"] += 1
                        continue
                    if not sources:
                        audit["expressions"]["discarded"]["sources_mismatch"] += 1
                        continue
                    normalized = _normalized_quote(quote)
                    if not normalized or not any(normalized in text_by_label[source] for source in sources):
                        audit["expressions"]["discarded"]["not_in_source"] += 1
                        continue
                entry["sources"] = sources
                if key in {"methods", "contradictions"} and not sources:
                    continue
                valid.append(entry)
            data[key] = valid
        audit["expressions"]["kept"] = len(data["expressions"])
        portrait = _one_sentence(data.get("one_sentence"))
        if portrait.pop("shape_unrecognized", False):
            audit["one_sentence"]["shape_unrecognized"] = 1
        portrait["sources"] = [source for source in portrait["sources"] if source in labels]
        if not portrait["sources"]:
            audit["one_sentence"]["sources_unmatched"] = 1
        data["one_sentence"] = portrait
        data["_audit"] = audit
        return data

    async def run(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        self.response_meta = []
        usable = [item for item in items if item.get("item_status") in {"ready", "written", "degraded"}]
        groups: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        current_chars = 0
        for item in usable:
            metadata = item.get("metadata") or {}
            payload = {"source": _source_id(item), "kind": "image_text" if metadata.get("kind") == "image" else "video", "title": metadata.get("title"), "summary": item.get("summary") or {}, "transcript": item.get("polished_text") or ""}
            size = len(_json(payload))
            if current and current_chars + size > self.max_chars:
                groups.append(current)
                current, current_chars = [], 0
            current.append(payload)
            current_chars += size
        if current:
            groups.append(current)
        mapped = []
        for group in groups:
            prompt = """你是内容研究员。只根据给定视频资料归纳，禁止补充外部知识。严格返回 JSON。结构示例：{"themes":[],"methods":[],"contradictions":[],"expressions":[{"quote":"原文连续短句","note":"可复用原因","sources":["003"]}],"techniques":{"hooks":[{"pattern":"把原片中的【具体困境】化成【具体提问】","note":"原片使用具体困境引出提问","sources":["003"]}],"endings":[],"pacing":[]},"one_sentence":{"text":"一句话画像","sources":["003"]}}。techniques 只能从 narrative_structure.hook、ending、reusable_elements、editing_and_expression 归纳；反例：用提问开场（无原片具体元素，不要输出）。sources 必须使用输入中的三位 source 编号；quote 必须是所引作品中连续出现的原句，不得改写标点或添加省略号；one_sentence 不超过 120 字，必须由资料支撑。资料：\n""" + _json(group)
            mapped.append(self._parse(await self._chat(prompt)))
        if not mapped:
            return {"one_sentence": "暂无足够的有效分析结果。", "themes": [], "methods": [], "contradictions": [], "expressions": [], "techniques": {}}
        if len(mapped) == 1:
            return self._validate(mapped[0], items)
        reduced = self._parse(await self._chat("请合并以下分组归纳，按 pattern 去重并累计 sources，不得添加输入之外的事实。严格返回 JSON。techniques 必须是 {hooks:[{pattern:带【】占位的原片具体骨架,note:原因,sources:[三位来源编号]}],endings:[],pacing:[]}；只能依据输入的叙事结构、可复用要素与剪辑表达，不能输出‘用提问开场’等空泛套路。expressions 元素必须是 {quote:原文连续短句,note:复用原因,sources:[三位来源编号]}，quote 不得改写标点或加省略号；one_sentence 必须是 {text:120字内一句话,sources:[三位来源编号]}。\n" + _json(mapped)))
        return self._validate(reduced, items)


class DistillService:
    """蒸馏运行状态机。"""

    def __init__(self, db: TaskDB, download_item: DownloadCallback, transcribe_item: TranscriptCallback,
                 summarize_item: SummaryCallback, synthesis: DistillSynthesis, output_root: Path):
        self.db = db
        self.download_item = download_item
        self.transcribe_item = transcribe_item
        self.summarize_item = summarize_item
        self.synthesis = synthesis
        self.output_root = output_root.resolve()
        self.download_limit = asyncio.Semaphore(2)
        self.transcript_limit = asyncio.Semaphore(1)
        self.summary_limit = asyncio.Semaphore(2)
        self.active: Dict[str, asyncio.Task[Any]] = {}
        self.cancel_events: Dict[str, asyncio.Event] = {}
        self.download_403_streak: Dict[str, int] = {}
        self.download_circuit_errors: Dict[str, Dict[str, Any]] = {}
        self.download_state_locks: Dict[str, asyncio.Lock] = {}
        self.download_circuit_threshold = max(1, int(os.getenv("DOUYIN_DISTILL_DOWNLOAD_CIRCUIT_BREAKER", "5")))
        self.min_transcript_chars = max(0, int(os.getenv("DOUYIN_DISTILL_MIN_TRANSCRIPT_CHARS", "150")))
        self.min_summary_cjk_ratio = max(0.0, float(os.getenv("DOUYIN_DISTILL_MIN_SUMMARY_CJK_RATIO", "0.15")))
        self.retry_delays = (5, 20, 60)

    async def _record_download_result(self, run_id: str, error: Optional[BaseException] = None) -> None:
        async with self.download_state_locks[run_id]:
            if self.download_circuit_errors.get(run_id):
                return
            if isinstance(error, DistillError) and error.code == "DISTILL_DOWNLOAD_403":
                streak = self.download_403_streak.get(run_id, 0) + 1
                self.download_403_streak[run_id] = streak
                if streak >= self.download_circuit_threshold:
                    self.download_circuit_errors[run_id] = _error("download", "DISTILL_DOWNLOAD_CIRCUIT_OPEN", f"连续 {streak} 条作品下载返回 HTTP 403，已暂停本轮蒸馏", error.action)
            else:
                self.download_403_streak[run_id] = 0

    async def _update_counts(self, run_id: str) -> None:
        counts = await self.db.count_distill_items(run_id)
        await self.db.update_distill_run(run_id, completed_count=counts["completed"], degraded_count=counts["degraded"], skipped_count=counts["skipped"], failed_count=counts["failed"])

    async def _stage(self, run_id: str, item: Optional[Dict[str, Any]], stage: str, callback: Callable[[], Awaitable[Any]], retry_callback: Optional[Callable[[int], Awaitable[None]]] = None) -> Any:
        for attempt in range(len(self.retry_delays) + 1):
            try:
                return await callback()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not _retryable(exc) or attempt >= len(self.retry_delays):
                    raise DistillError(exc.code if isinstance(exc, DistillError) else f"DISTILL_{stage.upper()}_FAILED", _error_from_exception(stage, exc)["reason"], _error_from_exception(stage, exc)["action"], False) from exc
                retry_count = int(item.get("retry_count") or 0) + 1 if item else attempt + 1
                if item and item.get("aweme_id"):
                    await self.db.update_distill_item(run_id, item["aweme_id"], retry_count=retry_count)
                    item["retry_count"] = retry_count
                if retry_callback:
                    await retry_callback(retry_count)
                await asyncio.sleep(self.retry_delays[attempt])
        raise AssertionError("unreachable")

    async def _progress(self, run_id: str, item: Dict[str, Any], stage: str, percentage: int) -> None:
        await self.db.update_distill_item(run_id, item["aweme_id"], **{f"{stage}_status": "running"})

    async def _process_item(self, run_id: str, item: Dict[str, Any]) -> None:
        aweme_id = item["aweme_id"]
        meta = item.get("metadata") or {}
        if meta.get("kind") == "image":
            body = str(meta.get("body") or meta.get("desc") or "").strip()
            if body:
                await self.db.update_distill_item(run_id, aweme_id, item_status="ready", download_status="skipped", transcript_status="completed", summary_status="skipped", original_transcript=body, polished_text=body, degrade_reason=None)
            else:
                await self.db.update_distill_item(run_id, aweme_id, item_status="skipped", download_status="skipped", transcript_status="skipped", summary_status="skipped", degrade_reason="图文正文为空，未纳入归纳")
            return
        event = self.cancel_events[run_id]
        if event.is_set():
            raise asyncio.CancelledError
        path = Path(item["local_path"]) if item.get("local_path") else None
        if path is None or not path.exists():
            await self.db.update_distill_item(run_id, aweme_id, item_status="downloading", download_status="running")
            async with self.download_limit:
                if self.download_circuit_errors.get(run_id):
                    raise DistillError("DISTILL_CIRCUIT_OPEN", "本轮下载已暂停", "修复登录状态或等待限速解除后继续")
                try:
                    path = await self._stage(run_id, item, "download", lambda: self.download_item(item))
                except Exception as exc:
                    await self._record_download_result(run_id, exc)
                    raise
                await self._record_download_result(run_id)
            stat = path.stat()
            await self.db.update_distill_item(run_id, aweme_id, task_id=item.get("task_id"), local_path=str(path), source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns, download_status="completed")
            item["local_path"] = str(path)
        else:
            await self.db.update_distill_item(run_id, aweme_id, download_status="reused")
        if not path or not path.exists():
            raise DistillError("DISTILL_VIDEO_NOT_FOUND", "下载后仍找不到本地视频文件", "检查下载目录后重试")
        duration_seconds = meta.get("duration_seconds")
        too_long_for_transcript = False
        try:
            too_long_for_transcript = float(duration_seconds) > 655 if duration_seconds is not None else False
        except (TypeError, ValueError):
            too_long_for_transcript = False
        if too_long_for_transcript:
            await self.db.update_distill_item(run_id, aweme_id, transcript_status="skipped", degrade_reason="音频预计超过 655 秒，跳过口播提取")
        elif not item.get("original_transcript") and item.get("transcript_status") not in {"skipped", "completed", "reused"}:
            await self.db.update_distill_item(run_id, aweme_id, item_status="transcribing", transcript_status="running")
            try:
                async with self.transcript_limit:
                    result = await self._stage(run_id, item, "transcribing", lambda: self.transcribe_item(f"{run_id}:{aweme_id}", path, lambda stage, progress: self._progress(run_id, item, "transcript", progress)))
                await self.db.update_distill_item(run_id, aweme_id, transcript_status="completed", original_transcript=result.get("original_transcript", ""), polished_text=result.get("polished_text", ""))
                item.update(result)
            except Exception as exc:
                error = _error_from_exception("transcribing", exc)
                await self.db.update_distill_item(run_id, aweme_id, transcript_status="failed", error_json=_json(error), degrade_reason=error["reason"])
        elif item.get("transcript_status") == "pending":
            await self.db.update_distill_item(run_id, aweme_id, transcript_status="skipped", degrade_reason="复用结果不可用，未生成口播文案")
        run = await self.db.get_distill_run(run_id)
        if (run.get("options") or {}).get("stage_policy") == "preview":
            latest = await self.db.get_distill_item(run_id, aweme_id) or item
            has_text = bool(latest.get("polished_text") or latest.get("original_transcript"))
            status = "low_signal" if _low_signal_reason(latest, self.min_transcript_chars) else "transcribed" if has_text else "failed"
            await self.db.update_distill_item(run_id, aweme_id, item_status=status, summary_status="skipped", degrade_reason=_low_signal_reason(latest, self.min_transcript_chars) or latest.get("degrade_reason"))
            return
        if (item.get("item_status") == "summary_invalid" or not item.get("summary")) and item.get("summary_status") not in {"skipped", "completed", "reused"}:
            await self.db.update_distill_item(run_id, aweme_id, item_status="summarizing", summary_status="running")
            try:
                async with self.summary_limit:
                    result = await self._stage(run_id, item, "summarizing", lambda: self.summarize_item(f"{run_id}:{aweme_id}", path, lambda stage, progress: self._progress(run_id, item, "summary", progress)))
                await self.db.update_distill_item(run_id, aweme_id, summary_status="completed", summary_json=_json(result))
                item["summary"] = result
            except Exception as exc:
                error = _error_from_exception("summarizing", exc)
                await self.db.update_distill_item(run_id, aweme_id, summary_status="failed", error_json=_json(error), degrade_reason=error["reason"])
        latest = await self.db.get_distill_item(run_id, aweme_id) or item
        has_transcript = bool(latest.get("polished_text") or latest.get("original_transcript"))
        has_summary = bool(latest.get("summary"))
        low_signal = _low_signal_reason(latest, self.min_transcript_chars)
        invalid_summary = _summary_invalid_reason(latest.get("summary"), self.min_summary_cjk_ratio) if has_summary else ""
        if low_signal:
            status = "low_signal"
            await self.db.update_distill_item(run_id, aweme_id, degrade_reason=low_signal)
        elif invalid_summary:
            status = "summary_invalid"
            await self.db.update_distill_item(run_id, aweme_id, degrade_reason=invalid_summary)
        elif has_transcript and has_summary:
            status = "ready"
        elif has_transcript or has_summary:
            status = "degraded"
            reason = latest.get("degrade_reason") or ("缺少画面理解结果" if has_transcript else "缺少口播文案结果")
            await self.db.update_distill_item(run_id, aweme_id, degrade_reason=reason)
        else:
            status = "failed"
        await self.db.update_distill_item(run_id, aweme_id, item_status=status)

    async def _close_inflight_items(self, run_id: str, *, cancelled: bool) -> None:
        """收口取消/重启时遗留的中间态，避免界面显示永久运行。"""
        active_statuses = {"downloading", "transcribing", "summarizing", "running"}
        for item in await self.db.get_distill_items(run_id, limit=10_000):
            if item.get("item_status") not in active_statuses:
                continue
            if cancelled:
                await self.db.update_distill_item(
                    run_id,
                    item["aweme_id"],
                    item_status="cancelled",
                    download_status="cancelled" if item.get("download_status") == "running" else item.get("download_status"),
                    transcript_status="cancelled" if item.get("transcript_status") == "running" else item.get("transcript_status"),
                    summary_status="cancelled" if item.get("summary_status") == "running" else item.get("summary_status"),
                    degrade_reason="用户取消了本次蒸馏，可点击继续后重试",
                )
            else:
                await self.db.update_distill_item(
                    run_id,
                    item["aweme_id"],
                    item_status="queued",
                    download_status="pending" if item.get("download_status") == "running" else item.get("download_status"),
                    transcript_status="pending" if item.get("transcript_status") == "running" else item.get("transcript_status"),
                    summary_status="pending" if item.get("summary_status") == "running" else item.get("summary_status"),
                    error_json=_json(_error("restart", "DISTILL_STAGE_INTERRUPTED", "服务重启，中断的阶段未自动重试", "点击继续蒸馏")),
                )

    def _run_output_root(self, run: Dict[str, Any]) -> Path:
        requested = (run.get("options") or {}).get("save_path")
        root = Path(requested).expanduser().resolve() if requested else self.output_root
        try:
            root.relative_to(self.output_root)
        except ValueError as exc:
            raise DistillError("DISTILL_SAVE_PATH_OUTSIDE_ROOT", "蒸馏保存位置不在下载目录内", "修改保存位置后重试") from exc
        root.mkdir(parents=True, exist_ok=True)
        return root

    async def run(self, run_id: str) -> None:
        self.cancel_events[run_id] = asyncio.Event()
        self.download_403_streak[run_id] = 0
        self.download_state_locks[run_id] = asyncio.Lock()
        self.download_circuit_errors.pop(run_id, None)
        try:
            run = await self.db.get_distill_run(run_id)
            if not run:
                return
            await self.db.update_distill_run(run_id, status="running", started_at=run.get("started_at") or _now())
            items = await self.db.get_distill_items(run_id, limit=10_000)
            output_root = self._run_output_root(run)
            for item in items:
                item["_save_root"] = str(output_root)

            async def process_and_count(item: Dict[str, Any]) -> None:
                deep_selected = (run.get("options") or {}).get("deep_selected_aweme_ids")
                if deep_selected is not None and item["aweme_id"] not in deep_selected:
                    return
                if item.get("item_status") in ITEM_TERMINAL and item.get("item_status") != "failed":
                    return
                try:
                    await self._process_item(run_id, item)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if isinstance(exc, DistillError) and exc.code == "DISTILL_CIRCUIT_OPEN":
                        await self.db.update_distill_item(run_id, item["aweme_id"], item_status="queued", download_status="pending")
                        await self._update_counts(run_id)
                        return
                    error = _error_from_exception("item", exc)
                    await self.db.update_distill_item(run_id, item["aweme_id"], item_status="failed", error_json=_json(error), degrade_reason=error["reason"])
                await self._update_counts(run_id)
            await asyncio.gather(*(process_and_count(item) for item in items))
            await self._update_counts(run_id)
            if self.download_circuit_errors.get(run_id):
                await self.db.update_distill_run(run_id, status="paused", error_json=_json(self.download_circuit_errors[run_id]))
                return
            if (run.get("options") or {}).get("stage_policy") == "preview":
                await self.db.update_distill_run(run_id, status="preview_ready", completed_at=_now())
                return
            items = await self.db.get_distill_items(run_id, limit=10_000)
            usable = [item for item in items if item.get("item_status") in {"ready", "degraded"}]
            if not usable:
                await self.db.update_distill_run(run_id, status="failed", error_json=_json(_error("synthesis", "DISTILL_NO_USABLE_ITEMS", "没有任何作品完成口播或画面分析", "修复失败条目后重试")), completed_at=_now())
                return
            await self.db.update_distill_run(run_id, status="synthesizing")
            synthesis = await self._stage(run_id, None, "synthesis", lambda: self.synthesis.run(usable))
            audit = synthesis.pop("_audit", {})
            if audit:
                expressions = audit.get("expressions") or {}
                get_logger().info("蒸馏归纳表达校验：run_id=%s 返回=%s 保留=%s 丢弃=%s", run_id, expressions.get("returned", 0), expressions.get("kept", 0), expressions.get("discarded", {}))
            run["synthesis"] = synthesis
            await self.db.update_distill_run(run_id, status="writing", synthesis_json=_json(synthesis), synthesis_audit=_json(audit), synthesis_response_meta=_json(getattr(self.synthesis, "response_meta", [])))
            renderer = DistillMarkdown(output_root, int(os.getenv("DOUYIN_DISTILL_SPLIT_THRESHOLD", "50000")))
            artifact = renderer.render(run, items)
            for item in items:
                if item.get("item_status") == "ready":
                    await self.db.update_distill_item(run_id, item["aweme_id"], item_status="written")
            await self._update_counts(run_id)
            counts = await self.db.count_distill_items(run_id)
            status = "completed" if counts["failed"] == 0 and counts["pending"] == 0 and counts["cancelled"] == 0 else "partial"
            await self.db.update_distill_run(run_id, status=status, output_path=artifact["path"], completed_at=_now())
        except asyncio.CancelledError:
            await self._close_inflight_items(run_id, cancelled=True)
            await self.db.update_distill_run(run_id, status="cancelled", completed_at=_now())
            raise
        except Exception as exc:
            error = _error_from_exception("run", exc)
            current = await self.db.get_distill_run(run_id)
            if current and current.get("synthesis") and current.get("output_path") and Path(current["output_path"]).is_file():
                await self.db.update_distill_run(run_id, error_json=_json(error), completed_at=_now())
            else:
                await self.db.update_distill_run(run_id, status="failed", error_json=_json(error), synthesis_audit=_json({"failure": {"code": error["code"], "reason": error["reason"]}}), synthesis_response_meta=_json(getattr(self.synthesis, "response_meta", [])), completed_at=_now())
        finally:
            self.cancel_events.pop(run_id, None)
            self.download_403_streak.pop(run_id, None)
            self.download_circuit_errors.pop(run_id, None)
            self.download_state_locks.pop(run_id, None)
            self.active.pop(run_id, None)

    def schedule(self, run_id: str) -> asyncio.Task[Any]:
        existing = self.active.get(run_id)
        if existing and not existing.done():
            return existing
        task = asyncio.create_task(self.run(run_id))
        self.active[run_id] = task
        return task

    async def resume(self, run_id: str, aweme_ids: Optional[List[str]] = None, stage_policy: Optional[str] = None) -> None:
        """清除指定失败/取消项的检查点并恢复运行。"""
        run = await self.db.get_distill_run(run_id)
        if not run:
            raise DistillError("DISTILL_RUN_NOT_FOUND", "蒸馏运行不存在", "刷新历史记录后重试")
        if stage_policy == "full" and (run.get("options") or {}).get("stage_policy") == "preview":
            if not aweme_ids:
                raise DistillError("DISTILL_SELECTION_EMPTY", "请先选择需要深看的作品", "在预筛结果中勾选作品")
        selected = [str(value) for value in (aweme_ids or []) if str(value).strip()]
        items = await self.db.get_distill_items(run_id, item_ids=selected or None, limit=10_000)
        if selected and len(items) != len(set(selected)):
            raise DistillError("DISTILL_ITEM_NOT_FOUND", "指定的作品不属于本次蒸馏", "刷新运行详情后重试")
        if stage_policy == "full" and (run.get("options") or {}).get("stage_policy") == "preview":
            options = {**run["options"], "stage_policy": "full", "deep_selected_aweme_ids": selected}
            await self.db.update_distill_run(run_id, options_json=_json(options))
        for item in items:
            if item.get("item_status") not in ({"failed", "cancelled", "summary_invalid"} if not selected else {"failed", "cancelled", "summary_invalid", "transcribed", "degraded", "ready", "written"}):
                continue
            await self.db.update_distill_item(
                run_id,
                item["aweme_id"],
                item_status="queued",
                download_status="pending" if not item.get("local_path") else item.get("download_status") or "pending",
                transcript_status="pending" if not item.get("original_transcript") else item.get("transcript_status") or "reused",
                summary_status="pending" if item.get("item_status") in {"summary_invalid", "transcribed"} or not item.get("summary") else item.get("summary_status") or "reused",
                summary_json=None if item.get("item_status") == "summary_invalid" else _json(item.get("summary")) if item.get("summary") else None,
                error_json=None,
                degrade_reason=None,
            )
        await self.db.update_distill_run(run_id, status="queued", error_json=None, completed_at=None)
        self.schedule(run_id)

    async def cancel(self, run_id: str) -> None:
        event = self.cancel_events.get(run_id)
        if event:
            event.set()
        task = self.active.get(run_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            await self._close_inflight_items(run_id, cancelled=True)

    async def recover_after_restart(self) -> int:
        rows = await self.db.fetch_all("SELECT run_id FROM distill_runs WHERE status IN ('running','synthesizing','writing','cancelling')")
        for row in rows:
            await self._close_inflight_items(row[0], cancelled=False)
            await self.db.update_distill_run(row[0], status="paused", error_json=_json(_error("restart", "DISTILL_PAUSED_AFTER_RESTART", "服务重启，中断的阶段未自动重试", "点击继续蒸馏")))
        return len(rows)
