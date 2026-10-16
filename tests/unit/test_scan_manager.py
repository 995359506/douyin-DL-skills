import asyncio
import time

import pytest

from skills.douyin_downloader.scan_manager import ScanManager, ScanSession, normalize_scan_item


def test_scan_item_uses_local_timezone_for_epoch():
    item = normalize_scan_item({"aweme_id": "123456", "create_time": 0}, "scan", 1)
    assert item is not None
    assert item.publish_time is not None
    assert item.publish_time.endswith("00:00:00+00:00") or "+" in item.publish_time


@pytest.mark.asyncio
async def test_expired_scan_cancels_background_task():
    manager = ScanManager(ttl_seconds=1)
    session = ScanSession("scan", "token", "url", "sec", "name", created_at=time.time() - 5, items=[])
    manager.sessions["scan"] = session
    task = asyncio.create_task(asyncio.sleep(60))
    manager.tasks["scan"] = task
    manager.cleanup_expired()
    await asyncio.sleep(0)
    assert "scan" not in manager.sessions
    assert "scan" not in manager.tasks
    assert task.cancelled()


@pytest.mark.asyncio
async def test_scan_pagination_uses_stable_aweme_cursor():
    manager = ScanManager()
    session = await manager.create_scan("https://v.douyin.com/mock", "sec", "name", total_hint=5)
    session.status = "completed"
    session.items = [
        normalize_scan_item({"aweme_id": str(100000 + i), "create_time": i}, session.scan_id, 1)  # type: ignore[list-item]
        for i in range(1, 6)
    ]
    first = await manager.list_items(session.scan_id, session.token, None, 2)
    second = await manager.list_items(session.scan_id, session.token, first["next_cursor"], 2)
    assert len(first["items"]) == 2
    assert len(second["items"]) == 2
    assert session.to_dict()["total_hint"] == 5
    assert set(item["aweme_id"] for item in first["items"]).isdisjoint(item["aweme_id"] for item in second["items"])
