"""批次结果、真实请求额度与恢复接口。"""

from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field


class RequestLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_limit: Optional[int] = Field(default=None, ge=0, le=1000000)


class RetryAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=200)


def create_batch_results_router(get_results: Callable[[], Any], resume_batch: Callable, retry_analysis: Optional[Callable] = None) -> APIRouter:
    router = APIRouter(prefix="/api/batch-results", tags=["批次结果与调用额度"])

    def ready():
        value = get_results()
        if value is None:
            raise HTTPException(503, detail="批次结果服务尚未就绪")
        return value

    @router.get("")
    async def list_batches():
        return {"success": True, "data": {"batches": await ready().list_batches()}}

    @router.get("/{kind}/{batch_id}")
    async def result(kind: Literal["download", "summary", "transcript", "distill", "single", "topic"], batch_id: str,
                     status: str = "", author: str = "", page: int = Query(1, ge=1),
                     page_size: int = Query(30, ge=1, le=100)):
        try:
            data = await ready().report(kind, batch_id, status=status, author=author, page=page, page_size=page_size)
        except KeyError:
            raise HTTPException(404, detail="批次不存在")
        return {"success": True, "data": data}

    @router.post("/{kind}/{batch_id}/limit")
    async def limit(kind: Literal["download", "summary", "transcript", "distill", "single", "topic"], batch_id: str, request: RequestLimit):
        results = ready()
        await results.ledger.set_limit(kind, batch_id, request.request_limit)
        return {"success": True, "data": await results.ledger.stats(kind, batch_id),
                "message": "调用次数上限已保存；达到额度后不会启动新请求，在途请求仍可能完成并计费"}

    @router.post("/{kind}/{batch_id}/resume")
    async def resume(kind: Literal["download", "summary", "transcript", "distill", "single", "topic"], batch_id: str):
        return {"success": True, "data": await resume_batch(kind, batch_id), "message": "已恢复未完成分析，已完成结果会复用"}

    @router.post("/{kind}/{batch_id}/retry")
    async def retry(kind: Literal["download", "summary", "transcript"], batch_id: str, request: RetryAnalysis):
        if retry_analysis is None:
            raise HTTPException(503, detail="重试服务尚未就绪")
        return {"success": True, "data": await retry_analysis(kind, batch_id, request.task_id)}

    return router
