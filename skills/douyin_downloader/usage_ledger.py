"""模型请求台账、原子次数上限与成功阶段检查点。

台账只保存调用元数据和上游用量；检查点是用户主动分析产生的文本结果，
不保存请求体、密钥、音视频或完整上游响应。历史缺少的数据不会补成零。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import aiosqlite
import httpx


class ModelControlError(RuntimeError):
    """控制信号不能按网络错误重试、切换备用通道或降级吞掉。"""

    retryable = False


class BatchRequestLimitReached(ModelControlError):
    code = "MODEL_REQUEST_LIMIT_REACHED"

    def __init__(self, batch_kind: str, batch_id: str, limit: int):
        self.batch_kind, self.batch_id, self.limit = batch_kind, batch_id, limit
        super().__init__(f"本批次已达到 {limit} 次模型请求上限，已暂停后续调用；在途请求仍可能完成并计费")


class UsageLedgerUnavailable(ModelControlError):
    code = "MODEL_LEDGER_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("调用台账暂不可用，已停止启动模型请求；恢复台账后可继续")


@dataclass(frozen=True)
class RequestContext:
    batch_kind: str = "single"
    batch_id: str = ""
    execution_id: str = ""
    stage: str = ""
    retry_index: int = 0


_context: ContextVar[RequestContext] = ContextVar("douyin_model_context", default=RequestContext())
_default_ledger: Optional["UsageLedger"] = None


def configure_default_ledger(ledger: Optional["UsageLedger"]) -> None:
    """由应用启动显式配置；独立服务与历史单测可不配置。"""
    global _default_ledger
    _default_ledger = ledger


def get_default_ledger() -> Optional["UsageLedger"]:
    return _default_ledger


def current_request_context() -> RequestContext:
    return _context.get()


@contextmanager
def model_request_context(**values: Any) -> Iterator[RequestContext]:
    context = replace(_context.get(), **{key: max(0, int(value)) if key == "retry_index" else str(value or "") for key, value in values.items() if key in RequestContext.__dataclass_fields__})
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


def content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


async def file_content_hash(path: Path) -> str:
    def calculate() -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    return await asyncio.to_thread(calculate)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_usage(body: Any) -> Dict[str, Any]:
    """仅提取可核实的数值，兼容 Gemini、OpenAI 与 DashScope。"""
    if not isinstance(body, dict):
        return {"usage_complete": False}
    usage = body.get("usageMetadata") or body.get("usage") or {}
    if not isinstance(usage, dict):
        return {"usage_complete": False}
    fields = {
        "input_tokens": ("promptTokenCount", "prompt_tokens", "input_tokens"),
        "output_tokens": ("candidatesTokenCount", "completion_tokens", "output_tokens"),
        "total_tokens": ("totalTokenCount", "total_tokens"),
        "audio_seconds": ("audio_seconds", "duration"),
    }
    result: Dict[str, Any] = {}
    for target, names in fields.items():
        for name in names:
            value = usage.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 9_223_372_036_854_775_807 and math.isfinite(value):
                result[target] = value if target == "audio_seconds" else int(value)
                break
    result["usage_complete"] = "input_tokens" in result and "output_tokens" in result
    # Gemini 的思考 token 与候选输出分开报告，二者都可能按输出计费。
    thoughts = usage.get("thoughtsTokenCount")
    if "candidatesTokenCount" in usage and isinstance(thoughts, int) and not isinstance(thoughts, bool) and thoughts >= 0 and "output_tokens" in result and result["output_tokens"] + thoughts <= 9_223_372_036_854_775_807:
        result["output_tokens"] += thoughts
    if "total_tokens" in result and result["usage_complete"] and result["total_tokens"] != result["input_tokens"] + result["output_tokens"]:
        result["usage_complete"] = False
    return result


class UsageLedger:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._ready = False
        self._init_lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._init_lock:
            if self._ready:
                return
            try:
                async with aiosqlite.connect(self.db_path, timeout=30) as db:
                    await db.executescript("""
                        CREATE TABLE IF NOT EXISTS execution_attempts (
                            request_id TEXT PRIMARY KEY, batch_kind TEXT NOT NULL, batch_id TEXT NOT NULL,
                            execution_id TEXT NOT NULL, stage TEXT NOT NULL, segment_index INTEGER,
                            provider TEXT NOT NULL, model TEXT NOT NULL, retry_index INTEGER NOT NULL DEFAULT 0,
                            is_fallback INTEGER NOT NULL DEFAULT 0,
                            started_at TEXT NOT NULL, completed_at TEXT, elapsed_seconds REAL,
                            status TEXT NOT NULL, http_status INTEGER, usage_complete INTEGER NOT NULL DEFAULT 0,
                            input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, audio_seconds REAL,
                            estimated_cost REAL, currency TEXT, rate_version TEXT,
                            input_rate REAL, output_rate REAL, request_rate REAL, audio_rate REAL
                        );
                        CREATE INDEX IF NOT EXISTS idx_execution_attempts_batch ON execution_attempts(batch_kind,batch_id);
                        CREATE INDEX IF NOT EXISTS idx_execution_attempts_execution ON execution_attempts(execution_id,stage);
                        CREATE TABLE IF NOT EXISTS model_batch_limits (
                            batch_kind TEXT NOT NULL, batch_id TEXT NOT NULL, request_limit INTEGER,
                            paused INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                            PRIMARY KEY(batch_kind,batch_id)
                        );
                        CREATE TABLE IF NOT EXISTS model_price_rates (
                            provider TEXT NOT NULL, model TEXT NOT NULL, version TEXT NOT NULL,
                            currency TEXT NOT NULL, input_per_million REAL, output_per_million REAL,
                            per_request REAL, per_audio_second REAL, updated_at TEXT NOT NULL,
                            PRIMARY KEY(provider,model)
                        );
                        CREATE TABLE IF NOT EXISTS model_stage_checkpoints (
                            execution_id TEXT NOT NULL, stage TEXT NOT NULL, input_hash TEXT NOT NULL,
                            result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                            PRIMARY KEY(execution_id,stage,input_hash)
                        );
                        CREATE TABLE IF NOT EXISTS model_execution_tasks (
                            execution_id TEXT NOT NULL, task_id TEXT NOT NULL,
                            PRIMARY KEY(execution_id,task_id)
                        );
                        CREATE INDEX IF NOT EXISTS idx_model_execution_tasks_task ON model_execution_tasks(task_id);
                    """)
                    await db.commit()
                self._ready = True
            except (aiosqlite.Error, OSError) as exc:
                raise UsageLedgerUnavailable() from exc

    async def _rows(self, query: str, args: tuple[Any, ...] = ()) -> list[Dict[str, Any]]:
        await self.initialize()
        try:
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(query, args)
                return [dict(row) for row in await cursor.fetchall()]
        except (aiosqlite.Error, OSError) as exc:
            raise UsageLedgerUnavailable() from exc

    async def set_limit(self, batch_kind: str, batch_id: str, request_limit: Optional[int]) -> Dict[str, Any]:
        if request_limit is not None and (isinstance(request_limit, bool) or not isinstance(request_limit, int) or request_limit < 0):
            raise ValueError("模型请求次数上限必须为非负整数或不设上限")
        await self.initialize()
        try:
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                await db.execute("INSERT INTO model_batch_limits(batch_kind,batch_id,request_limit,updated_at) VALUES(?,?,?,?) ON CONFLICT(batch_kind,batch_id) DO UPDATE SET request_limit=excluded.request_limit,updated_at=excluded.updated_at", (batch_kind, batch_id, request_limit, _now()))
                await db.commit()
        except (aiosqlite.Error, OSError) as exc:
            raise UsageLedgerUnavailable() from exc
        return await self.stats(batch_kind, batch_id)

    async def resume(self, batch_kind: str, batch_id: str) -> Dict[str, Any]:
        await self.initialize()
        try:
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                await db.execute("UPDATE model_batch_limits SET paused=0,updated_at=? WHERE batch_kind=? AND batch_id=?", (_now(), batch_kind, batch_id))
                await db.commit()
        except (aiosqlite.Error, OSError) as exc:
            raise UsageLedgerUnavailable() from exc
        return await self.stats(batch_kind, batch_id)

    async def recover_after_restart(self) -> int:
        """仅由应用单实例启动调用；遗留请求结果未知，保留已占次数。"""
        await self.initialize()
        try:
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                cursor = await db.execute("UPDATE execution_attempts SET status='unknown',completed_at=? WHERE completed_at IS NULL", (_now(),))
                await db.commit()
                return cursor.rowcount
        except (aiosqlite.Error, OSError) as exc:
            raise UsageLedgerUnavailable() from exc

    async def set_price(self, provider: str, model: str, *, version: str, currency: str, input_per_million: Optional[float] = None, output_per_million: Optional[float] = None, per_request: Optional[float] = None, per_audio_second: Optional[float] = None) -> None:
        values = (input_per_million, output_per_million, per_request, per_audio_second)
        if not provider or not model or not version or not currency or any(value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0) for value in values):
            raise ValueError("模型价格配置无效")
        await self.initialize()
        try:
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                await db.execute("INSERT INTO model_price_rates VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,model) DO UPDATE SET version=excluded.version,currency=excluded.currency,input_per_million=excluded.input_per_million,output_per_million=excluded.output_per_million,per_request=excluded.per_request,per_audio_second=excluded.per_audio_second,updated_at=excluded.updated_at", (provider, model, version, currency, *values, _now()))
                await db.commit()
        except (aiosqlite.Error, OSError) as exc:
            raise UsageLedgerUnavailable() from exc

    async def prices(self) -> list[Dict[str, Any]]:
        """返回显式配置的单价；没有配置的通道保持未知。"""
        return await self._rows("SELECT provider,model,version,currency,input_per_million,output_per_million,per_request,per_audio_second,updated_at FROM model_price_rates ORDER BY provider,model")

    async def reserve(self, *, execution_id: str, stage: str, provider: str, model: str, segment_index: Optional[int] = None, retry_index: int = 0, is_fallback: bool = False) -> str:
        await self.initialize()
        context = current_request_context()
        request_id = str(uuid.uuid4())
        execution_id = context.execution_id or execution_id
        batch_id = context.batch_id or execution_id
        try:
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await (await db.execute("SELECT request_limit,paused FROM model_batch_limits WHERE batch_kind=? AND batch_id=?", (context.batch_kind, batch_id))).fetchone()
                if row:
                    count = (await (await db.execute("SELECT COUNT(*) FROM execution_attempts WHERE batch_kind=? AND batch_id=?", (context.batch_kind, batch_id))).fetchone())[0]
                    if row[1] or (row[0] is not None and count >= row[0]):
                        await db.execute("UPDATE model_batch_limits SET paused=1,updated_at=? WHERE batch_kind=? AND batch_id=?", (_now(), context.batch_kind, batch_id))
                        await db.commit()
                        raise BatchRequestLimitReached(context.batch_kind, batch_id, int(row[0] if row[0] is not None else count))
                rate = await (await db.execute("SELECT currency,version,input_per_million,output_per_million,per_request,per_audio_second FROM model_price_rates WHERE provider=? AND model=?", (provider, model))).fetchone()
                prices = tuple(rate) if rate else (None,) * 6
                await db.execute("INSERT INTO execution_attempts(request_id,batch_kind,batch_id,execution_id,stage,segment_index,provider,model,retry_index,is_fallback,started_at,status,currency,rate_version,input_rate,output_rate,request_rate,audio_rate) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (request_id, context.batch_kind, batch_id, execution_id, stage or context.stage, segment_index, provider, model, retry_index + context.retry_index, int(is_fallback), _now(), "in_flight", *prices))
                await db.commit()
            return request_id
        except (aiosqlite.Error, OSError) as exc:
            raise UsageLedgerUnavailable() from exc

    async def finish(self, request_id: str, *, elapsed_seconds: float, status: str, http_status: Optional[int] = None, response: Any = None) -> None:
        usage = extract_usage(response)
        try:
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                row = await (await db.execute("SELECT input_rate,output_rate,request_rate,audio_rate,currency FROM execution_attempts WHERE request_id=? AND completed_at IS NULL", (request_id,))).fetchone()
                if row is None:
                    return
                cost, known_price = 0.0, False
                complete_price = row[4] is not None
                # 配了 token 价格时必须同时明确输入与输出单价，缺价不能当作零。
                if row[0] is not None or row[1] is not None:
                    complete_price = complete_price and row[0] is not None and row[1] is not None and usage["usage_complete"]
                for rate, unit, denominator in ((row[0], "input_tokens", 1_000_000), (row[1], "output_tokens", 1_000_000), (row[3], "audio_seconds", 1)):
                    if rate is not None:
                        known_price = True
                        if unit not in usage:
                            complete_price = False
                        else:
                            cost += rate * usage[unit] / denominator
                if row[2] is not None:
                    known_price = True
                    cost += row[2]
                estimated_cost = cost if known_price and complete_price and math.isfinite(cost) else None
                await db.execute("UPDATE execution_attempts SET completed_at=?,elapsed_seconds=?,status=?,http_status=?,usage_complete=?,input_tokens=?,output_tokens=?,total_tokens=?,audio_seconds=?,estimated_cost=? WHERE request_id=? AND completed_at IS NULL", (_now(), elapsed_seconds, status, http_status, int(usage["usage_complete"]), usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens"), usage.get("audio_seconds"), estimated_cost, request_id))
                await db.commit()
        except (aiosqlite.Error, OSError) as exc:
            raise UsageLedgerUnavailable() from exc

    async def attempts(self, batch_kind: str, batch_id: str, limit: int = 200, offset: int = 0) -> list[Dict[str, Any]]:
        return await self._rows("SELECT * FROM execution_attempts WHERE batch_kind=? AND batch_id=? ORDER BY started_at,request_id LIMIT ? OFFSET ?", (batch_kind, batch_id, min(max(limit, 1), 1000), max(offset, 0)))

    async def stats(self, batch_kind: str, batch_id: str) -> Dict[str, Any]:
        rows = await self._rows("SELECT * FROM execution_attempts WHERE batch_kind=? AND batch_id=? ORDER BY started_at", (batch_kind, batch_id))
        limits = await self._rows("SELECT request_limit,paused FROM model_batch_limits WHERE batch_kind=? AND batch_id=?", (batch_kind, batch_id))
        costs: Dict[str, float] = {}
        for row in rows:
            if row["estimated_cost"] is not None and row["currency"]:
                costs[row["currency"]] = costs.get(row["currency"], 0) + row["estimated_cost"]
        durations = sorted(row["elapsed_seconds"] for row in rows if row["status"] == "succeeded" and row["elapsed_seconds"] is not None)
        sample_kinds = {(row["stage"], row["provider"], row["model"]) for row in rows if row["status"] == "succeeded" and row["elapsed_seconds"] is not None}
        # 兼容旧界面的单一区间仅在样本同质时返回，避免混算ASR、总结和归纳。
        duration_range = [durations[int((len(durations) - 1) * .1)], durations[int((len(durations) - 1) * .9)]] if len(durations) >= 10 and len(sample_kinds) == 1 else None
        unknown_cost = sum(row["estimated_cost"] is None for row in rows)
        grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
        phase_samples: Dict[tuple[str, str, str], list[float]] = {}
        for row in rows:
            key = (row["provider"], row["model"])
            group = grouped.setdefault(key, {"provider": key[0], "model": key[1], "request_count": 0, "succeeded_count": 0, "retry_request_count": 0, "fallback_request_count": 0, "usage_known_count": 0, "usage_unknown_count": 0, "estimated_costs": {}, "unknown_cost_request_count": 0})
            group["request_count"] += 1
            group["succeeded_count"] += row["status"] == "succeeded"
            group["retry_request_count"] += row["retry_index"] > 0
            group["fallback_request_count"] += bool(row["is_fallback"])
            group["usage_known_count"] += bool(row["usage_complete"])
            group["usage_unknown_count"] += not row["usage_complete"]
            if row["estimated_cost"] is not None and row["currency"]:
                group["estimated_costs"][row["currency"]] = group["estimated_costs"].get(row["currency"], 0) + row["estimated_cost"]
            else:
                group["unknown_cost_request_count"] += 1
            if row["status"] == "succeeded" and row["elapsed_seconds"] is not None:
                phase_samples.setdefault((row["stage"], row["provider"], row["model"]), []).append(row["elapsed_seconds"])
        timing_groups = []
        for (stage, provider, model), samples in sorted(phase_samples.items()):
            samples.sort()
            timing_groups.append({"stage": stage, "provider": provider, "model": model, "sample_count": len(samples), "network_seconds_range": [samples[int((len(samples) - 1) * .1)], samples[int((len(samples) - 1) * .9)]] if len(samples) >= 10 else None, "percentiles": [10, 90] if len(samples) >= 10 else None})
        return {
            "batch_kind": batch_kind, "batch_id": batch_id, "request_count": len(rows),
            "logical_execution_count": len({row["execution_id"] for row in rows}),
            "in_flight_count": sum(row["completed_at"] is None for row in rows),
            "retry_request_count": sum(row["retry_index"] > 0 for row in rows),
            "fallback_request_count": sum(bool(row["is_fallback"]) for row in rows),
            "usage_known_count": sum(bool(row["usage_complete"]) for row in rows),
            "usage_unknown_count": sum(not row["usage_complete"] for row in rows),
            "input_tokens": sum(row["input_tokens"] or 0 for row in rows) if rows and all(row["input_tokens"] is not None for row in rows) else None,
            "output_tokens": sum(row["output_tokens"] or 0 for row in rows) if rows and all(row["output_tokens"] is not None for row in rows) else None,
            "estimated_costs": costs, "cost_status": "unknown" if not costs else "incomplete" if unknown_cost else "estimated",
            "unknown_cost_request_count": unknown_cost, "historical_coverage": "仅启用台账后的实际请求；更早记录未知",
            "cost_scope": "统计归属本批次的全部实际模型请求：新增分析、分段、重试、备用通道、片段合并、map/reduce归纳；复用与共享引用不增加调用；下载与本地处理不计模型费用",
            "cost_method": "费用为请求时配置单价乘以上游实际用量的估算；非上游账单。缺少价格或必要用量保持未知，币种分别累计。按次价格按每次实际发送计算。",
            "provider_models": list(grouped.values()), "network_timing_by_stage": timing_groups,
            "request_limit": limits[0]["request_limit"] if limits else None,
            "paused": bool(limits and limits[0]["paused"]),
            "network_duration_sample_count": len(durations), "network_duration_seconds_range": duration_range,
            "estimate_note": "仅网络请求耗时样本；不包含排队、预处理、退避、下载冷却和用户暂停；不足10次成功样本不估算",
        }

    async def get_checkpoint(self, execution_id: str, stage: str, input_hash: str) -> Any:
        execution_id = current_request_context().execution_id or execution_id
        rows = await self._rows("SELECT result_json FROM model_stage_checkpoints WHERE execution_id=? AND stage=? AND input_hash=?", (execution_id, stage, input_hash))
        return json.loads(rows[0]["result_json"]) if rows else None

    async def save_checkpoint(self, execution_id: str, stage: str, input_hash: str, result: Any) -> None:
        await self.initialize()
        execution_id = current_request_context().execution_id or execution_id
        try:
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                await db.execute("INSERT OR IGNORE INTO model_stage_checkpoints VALUES(?,?,?,?,?)", (execution_id, stage, input_hash, json.dumps(result, ensure_ascii=False), _now()))
                await db.commit()
        except (aiosqlite.Error, OSError) as exc:
            raise UsageLedgerUnavailable() from exc

    async def delete_checkpoints(self, execution_id: str) -> int:
        """删除分析记录时清理恢复用文本；纯调用元数据仍保留。"""
        await self.initialize()
        try:
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                cursor = await db.execute("DELETE FROM model_stage_checkpoints WHERE execution_id=?", (execution_id,))
                await db.commit()
                return cursor.rowcount
        except (aiosqlite.Error, OSError) as exc:
            raise UsageLedgerUnavailable() from exc

    async def bind_execution(self, execution_id: str, task_id: str) -> None:
        """保留历次强制重做的归属，防止引用覆盖后残留分析正文。"""
        if not execution_id or not task_id:
            return
        await self.initialize()
        try:
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                await db.execute("INSERT OR IGNORE INTO model_execution_tasks VALUES(?,?)", (execution_id, task_id))
                await db.commit()
        except (aiosqlite.Error, OSError) as exc:
            raise UsageLedgerUnavailable() from exc

    async def delete_task_checkpoints(self, task_id: str) -> int:
        """清理任务的所有恢复文本；仍被其他任务共享的执行继续保留。"""
        await self.initialize()
        try:
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                await db.execute("BEGIN IMMEDIATE")
                rows = await (await db.execute("SELECT execution_id FROM model_execution_tasks WHERE task_id=?", (task_id,))).fetchall()
                await db.execute("DELETE FROM model_execution_tasks WHERE task_id=?", (task_id,))
                removed = 0
                for (execution_id,) in rows:
                    remaining = await (await db.execute("SELECT 1 FROM model_execution_tasks WHERE execution_id=? LIMIT 1", (execution_id,))).fetchone()
                    if not remaining:
                        cursor = await db.execute("DELETE FROM model_stage_checkpoints WHERE execution_id=?", (execution_id,))
                        removed += cursor.rowcount
                await db.commit()
                return removed
        except (aiosqlite.Error, OSError) as exc:
            raise UsageLedgerUnavailable() from exc


async def recorded_post(client: httpx.AsyncClient, url: str, *, ledger: Optional[UsageLedger] = None, execution_id: str = "", stage: str = "", provider: str = "", model: str = "", segment_index: Optional[int] = None, retry_index: int = 0, is_fallback: bool = False, **kwargs: Any) -> httpx.Response:
    """唯一发包边界；先持久化占额，异常和超时也保留一次请求。"""
    ledger = ledger or get_default_ledger()
    if ledger is None:
        return await client.post(url, **kwargs)
    request_id = await ledger.reserve(execution_id=execution_id, stage=stage, provider=provider, model=model, segment_index=segment_index, retry_index=retry_index, is_fallback=is_fallback)
    started = time.monotonic()
    try:
        response = await client.post(url, **kwargs)
    except BaseException:
        await asyncio.shield(ledger.finish(request_id, elapsed_seconds=time.monotonic() - started, status="unknown"))
        raise
    try:
        body = response.json()
    except (ValueError, TypeError):
        body = None
    await ledger.finish(request_id, elapsed_seconds=time.monotonic() - started, status="succeeded" if 200 <= response.status_code < 300 else "http_error", http_status=response.status_code, response=body)
    return response
