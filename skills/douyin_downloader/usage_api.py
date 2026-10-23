"""模型用量与费率独立路由，仅暴露调用元数据。"""

from __future__ import annotations

import re
from typing import Annotated, Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, field_validator, model_validator

try:
    from .usage_ledger import UsageLedger, UsageLedgerUnavailable
except ImportError:
    from usage_ledger import UsageLedger, UsageLedgerUnavailable


Rate = Annotated[StrictFloat, Field(ge=0, allow_inf_nan=False)]


class ModelPriceConfig(BaseModel):
    """单价是用户提供的估算配置，不代表上游账单。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    model: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_.:/-]+$")
    version: str = Field(min_length=1, max_length=80)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    input_per_million: Optional[Rate] = None
    output_per_million: Optional[Rate] = None
    per_request: Optional[Rate] = None
    per_audio_second: Optional[Rate] = None

    @field_validator("provider", "model", "version")
    @classmethod
    def reject_credentials(cls, value: str) -> str:
        if re.search(r"(?i)(?:sk-[a-z0-9_-]{12,}|bearer\s|https?://|[\x00-\x1f])", value):
            raise ValueError("费率标识只能填写通道、模型或版本名称，不能包含密钥、地址或控制字符")
        return value

    @model_validator(mode="after")
    def require_rate(self) -> "ModelPriceConfig":
        if all(getattr(self, field) is None for field in ("input_per_million", "output_per_million", "per_request", "per_audio_second")):
            raise ValueError("请至少填写一项明确的计费单价；未配置的价格保持未知")
        return self


def create_usage_router(get_ledger: Callable[[], Optional[UsageLedger]]) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["模型调用与成本"])

    def ledger() -> UsageLedger:
        instance = get_ledger()
        if instance is None:
            raise HTTPException(503, "模型调用台账正在初始化")
        return instance

    async def invoke(method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return await getattr(ledger(), method)(*args, **kwargs)
        except UsageLedgerUnavailable:
            raise HTTPException(503, "模型调用台账暂不可用，请稍后重试") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("/model-usage/{kind}/{batch_id}")
    async def usage(kind: str, batch_id: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        stats = await invoke("stats", kind, batch_id)
        attempts = await invoke("attempts", kind, batch_id, limit, offset)
        return {"success": True, "data": {"stats": stats, "attempts": attempts, "limit": limit, "offset": offset}}

    @router.get("/model-usage/{kind}/{batch_id}/attempts")
    async def attempts(kind: str, batch_id: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        return {"success": True, "data": await invoke("attempts", kind, batch_id, limit, offset)}

    @router.get("/model-prices")
    async def prices():
        return {"success": True, "data": {"prices": await invoke("prices"), "note": "配置用于估算，缺少单价或必要用量保持未知；修改仅影响后续请求，既有请求保留原费率快照"}}

    @router.put("/model-prices")
    async def set_price(config: ModelPriceConfig):
        values = config.model_dump()
        provider, model = values.pop("provider"), values.pop("model")
        await invoke("set_price", provider, model, **values)
        return {"success": True, "data": {"prices": await invoke("prices"), "note": "单价已保存，仅用于后续请求估算；不会改写历史费用"}}

    return router
