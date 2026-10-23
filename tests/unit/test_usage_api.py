import json

import httpx
import pytest
from fastapi import FastAPI

from skills.douyin_downloader.usage_api import create_usage_router
from skills.douyin_downloader.usage_ledger import UsageLedger, model_request_context


def make_app(ledger):
    app = FastAPI()
    app.include_router(create_usage_router(lambda: ledger))
    return app


@pytest.mark.asyncio
async def test_usage_routes_return_only_metadata_and_paginate(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.db")
    with model_request_context(batch_kind="download", batch_id="b", execution_id="job"):
        for index in range(2):
            request = await ledger.reserve(execution_id="job", stage="summary", provider="gateway", model="test", retry_index=index)
            await ledger.finish(request, elapsed_seconds=2 + index, status="succeeded", response={"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "source_text": "不能暴露的来源正文"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(ledger)), base_url="http://test") as client:
        response = await client.get("/api/model-usage/download/b?limit=1&offset=1")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["stats"]["request_count"] == 2 and len(data["attempts"]) == 1
        assert data["offset"] == 1 and data["limit"] == 1
        assert "不能暴露的来源正文" not in json.dumps(data, ensure_ascii=False)
        assert data["stats"]["provider_models"][0]["request_count"] == 2
        assert "重试" in data["stats"]["cost_scope"] and "上游账单" in data["stats"]["cost_method"]
        assert (await client.get("/api/model-usage/download/b/attempts?limit=1")).json()["data"][0]["request_id"]
        assert (await client.get("/api/model-usage/download/b?limit=0")).status_code == 422


@pytest.mark.asyncio
async def test_price_routes_save_structured_rates_without_rewriting_history(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.db")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(ledger)), base_url="http://test") as client:
        assert (await client.get("/api/model-prices")).json()["data"]["prices"] == []
        config = {"provider": "gateway", "model": "gemini-3.8-flash", "version": "2026-10-23", "currency": "CNY", "per_request": 0.5}
        response = await client.put("/api/model-prices", json=config)
        assert response.status_code == 200
        assert response.json()["data"]["prices"][0]["per_request"] == .5
        with model_request_context(batch_kind="topic", batch_id="t", execution_id="version"):
            request = await ledger.reserve(execution_id="version", stage="topic", provider="gateway", model=config["model"])
            await ledger.finish(request, elapsed_seconds=1, status="succeeded")
        assert (await client.put("/api/model-prices", json={**config, "version": "next", "per_request": 9})).status_code == 200
        stats = (await client.get("/api/model-usage/topic/t")).json()["data"]["stats"]
        assert stats["estimated_costs"] == {"CNY": .5}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"api_key": "must-not-be-accepted"}, {"per_request": -1}, {"per_request": True},
    {"model": "https://gateway.invalid"}, {"version": "sk-abcdefghijklmnop"},
    {"per_request": None}, {"currency": "not-a-currency"},
])
async def test_price_routes_reject_invalid_or_sensitive_configuration(tmp_path, change):
    ledger = UsageLedger(tmp_path / "usage.db")
    config = {"provider": "gateway", "model": "gemini-3.8-flash", "version": "v1", "currency": "CNY", "per_request": 1}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(ledger)), base_url="http://test") as client:
        assert (await client.put("/api/model-prices", json={**config, **change})).status_code == 422
    assert await ledger.prices() == []


@pytest.mark.asyncio
async def test_usage_routes_report_uninitialized_ledger():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(None)), base_url="http://test") as client:
        assert (await client.get("/api/model-prices")).status_code == 503
        assert (await client.get("/api/model-usage/download/b")).status_code == 503


@pytest.mark.asyncio
async def test_timing_samples_are_grouped_by_stage_and_model_not_mixed(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.db")
    with model_request_context(batch_kind="download", batch_id="b", execution_id="job"):
        for stage, model, count in (("summary_segment", "model-a", 10), ("polishing", "model-a", 2), ("summary_segment", "model-b", 3)):
            for index in range(count):
                request = await ledger.reserve(execution_id="job", stage=stage, provider="gateway", model=model)
                await ledger.finish(request, elapsed_seconds=1 + index, status="succeeded")
    stats = await ledger.stats("download", "b")
    groups = {(row["stage"], row["model"]): row for row in stats["network_timing_by_stage"]}
    assert groups[("summary_segment", "model-a")]["network_seconds_range"] == [1, 9]
    assert groups[("summary_segment", "model-a")]["percentiles"] == [10, 90]
    assert groups[("polishing", "model-a")]["network_seconds_range"] is None
    assert groups[("summary_segment", "model-b")]["network_seconds_range"] is None
