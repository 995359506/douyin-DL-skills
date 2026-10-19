"""主页批量链接入队与进度接口，不在提交阶段请求抖音。"""

import re
from typing import Any, Callable, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator


class DownloadBatchRequest(BaseModel):
    """仅接收公开作品编号，间隔固定由后端控制。"""

    model_config = ConfigDict(extra="forbid")
    video_ids: list[str] = Field(min_length=1, max_length=20000)
    author: str = Field(default="", max_length=80)
    profile_id: str = Field(default="", max_length=256, pattern=r"^[\w-]*$")
    client_request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    auto_summary: bool = True

    @field_validator("video_ids")
    @classmethod
    def validate_video_ids(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[0-9]{5,32}", value) for value in values):
            raise ValueError("作品编号必须是 5–32 位数字，不接受链接或其他内容")
        return list(dict.fromkeys(values))


def create_download_queue_router(
    get_queue: Callable[[], Any], check_config: Callable[[bool], None]
) -> APIRouter:
    """将队列接口与现有服务配置连接，避免主 Web 模块增加新端点。"""
    router = APIRouter(prefix="/api/download-batches", tags=["后台下载队列"])

    def ready_queue() -> Any:
        queue = get_queue()
        if queue is None:
            raise HTTPException(status_code=503, detail={"code": "DOWNLOAD_QUEUE_NOT_READY", "message": "后台下载队列尚未初始化", "action": "稍后刷新看板再试"})
        return queue

    async def required_batch(batch_id: str) -> dict:
        try:
            batch = await ready_queue().get(batch_id)
        except KeyError:
            batch = None
        if batch is None:
            raise HTTPException(status_code=404, detail={"code": "DOWNLOAD_BATCH_NOT_FOUND", "message": "找不到该后台下载队列", "action": "刷新队列列表后重试"})
        return batch

    @router.post("")
    async def create_batch(request: DownloadBatchRequest) -> dict:
        queue = ready_queue()
        check_config(request.auto_summary)
        try:
            batch = await queue.create(
                request.video_ids, author=request.author, profile_id=request.profile_id,
                client_request_id=request.client_request_id, auto_summary=request.auto_summary,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail={"code": "DOWNLOAD_BATCH_REQUEST_CONFLICT", "message": "同一提交编号已用于其他内容，请重新收集并确认", "action": "不要修改待重试批次的链接或提交编号"}) from exc
        return {"success": True, "data": batch, "message": "全部链接已保存至本地后台队列；可关闭网页，下载后逐条总结"}

    @router.get("")
    async def list_batches() -> dict:
        return {"success": True, "data": {"batches": await ready_queue().list_batches()}, "message": "后台下载队列已更新"}

    @router.get("/{batch_id}")
    async def get_batch(batch_id: str) -> dict:
        return {"success": True, "data": await required_batch(batch_id), "message": "后台下载进度已更新"}

    @router.post("/{batch_id}/{action}")
    async def control_batch(batch_id: str, action: Literal["pause", "resume", "stop"]) -> dict:
        batch = await required_batch(batch_id)
        if action == "resume":
            check_config(bool(batch.get("auto_summary", True)))
        batch = await ready_queue().control(batch_id, action)
        return {"success": True, "data": batch, "message": {"pause": "后续下载已暂停，当前项不受影响", "resume": f"队列已恢复，实际下载结束后间隔 {batch['interval_seconds']} 秒", "stop": "后续下载已停止，当前项与已提交总结继续"}[action]}

    return router
