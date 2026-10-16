from types import SimpleNamespace

import pytest

from skills.douyin_downloader import web
from skills.douyin_downloader.db import TaskVideoInfo


class _FakeDB:
    def __init__(self, video):
        self.video = video
        self.created = []
        self.added = []
        self.count_updates = []
        self.status_updates = []

    async def get_task_videos(self, task_id):
        return [self.video] if task_id == "parent-1" else [item for item in self.added if item.task_id == task_id]

    async def fetch_one(self, _sql, _parameters=()):
        return None

    async def create_task(self, **kwargs):
        self.created.append(kwargs)
        return "child-1"

    async def add_video(self, video):
        self.added.append(video)

    async def update_task_counts(self, task_id, **kwargs):
        self.count_updates.append((task_id, kwargs))

    async def update_task_status(self, task_id, status):
        self.status_updates.append((task_id, status))


@pytest.mark.asyncio
async def test_batch_video_reference_materializes_without_creating_visible_duplicate(monkeypatch):
    video = TaskVideoInfo(
        id=1,
        task_id="parent-1",
        aweme_id="7683145456423740913",
        desc="一条作品",
        nickname="西西信箱",
        sec_user_id="sec-user",
        create_time="2026-10-07 00:00:00",
        status="downloaded",
        file_path="7683145456423740913_video.mp4",
        file_size=123,
        error_message=None,
        download_time="2026-10-07 00:01:00",
    )
    db = _FakeDB(video)
    parent = SimpleNamespace(type="post", mix_name="西西信箱 · 已选 1 条", base_path="Download")
    manager = SimpleNamespace(db=db, get_task=lambda task_id: _return_parent(task_id, parent))
    monkeypatch.setattr(web, "task_manager", manager)

    child_id = await web._materialize_batch_video_task_reference(
        "parent-1::7683145456423740913"
    )

    assert child_id == "child-1"
    assert db.created[0]["task_type"] == "batch_item"
    assert db.created[0]["mix_id"] == "parent-1"
    assert db.added[0].task_id == "child-1"
    assert db.added[0].file_path == video.file_path
    assert db.count_updates == [("child-1", {"total": 1, "downloaded": 1, "skipped": 0, "failed": 0})]
    assert db.status_updates == [("child-1", "completed")]


async def _return_parent(task_id, parent):
    return parent if task_id == "parent-1" else None
