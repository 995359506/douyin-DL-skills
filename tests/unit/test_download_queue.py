"""持久化下载队列：假时钟验证，不请求抖音或模型。"""

import asyncio
import logging
import sqlite3

import pytest

from skills.douyin_downloader.download_queue import PersistentDownloadQueue
from skills.douyin_downloader import download_queue as queue_module


@pytest.fixture
async def queue_factory(tmp_path):
    queues = []

    async def create(prepare=None, execute=None, now=None, reuse=None, interval_seconds=180):
        async def default_prepare(aweme_id, author, profile_id):
            return f"task-{aweme_id}"

        async def default_execute(task_id, aweme_id, auto_summary):
            return {"status": "completed", "task_id": task_id}

        queue = PersistentDownloadQueue(
            str(tmp_path / "queue.db"), prepare or default_prepare,
            execute or default_execute, now=now or (lambda: 1000.0),
            reuse_item=reuse, interval_seconds=interval_seconds,
        )
        await queue.initialize()
        queues.append(queue)
        return queue

    yield create
    for queue in queues:
        await queue.shutdown()


async def test_create_is_local_atomic_deduplicated_and_idempotent(queue_factory):
    calls = []

    async def must_not_call(*args):
        calls.append(args)
        raise AssertionError("入队不应访问下载接口")

    queue = await queue_factory(must_not_call, must_not_call)
    first = await queue.create(["10001", "10002", "10001"], "作者", "profile", "request-1")
    same = await queue.create(["10001", "10002"], "作者", "profile", "request-1")
    assert first["total"] == first["pending"] == 2
    assert first["status"] == "queued"
    assert same["batch_id"] == first["batch_id"]
    assert len(await queue.list_batches()) == 1
    assert calls == []
    with pytest.raises(ValueError, match="视频编号"):
        await queue.create(["10003", "https://example.invalid"])
    assert len(await queue.list_batches()) == 1


async def test_global_cooldown_starts_after_completion_and_crosses_batches(queue_factory):
    clock = [1000.0]
    starts = []

    async def execute(task_id, aweme_id, auto_summary):
        starts.append((aweme_id, clock[0], auto_summary))
        clock[0] += 9
        return {"status": "completed", "task_id": task_id}

    queue = await queue_factory(execute=execute, now=lambda: clock[0])
    first = await queue.create(["10001"])
    second = await queue.create(["10002"], auto_summary=False)
    assert await queue.process_next()
    assert (await queue.get(first["batch_id"]))["status"] == "completed"
    assert (await queue.get(second["batch_id"]))["next_allowed_at"] == 1189
    clock[0] = 1188.99
    assert not await queue.process_next()
    clock[0] = 1189
    assert await queue.process_next()
    assert starts == [("10001", 1000, True), ("10002", 1189, False)]


async def test_local_skips_finish_without_cooldown_and_survive_restart(queue_factory):
    calls = []

    async def execute(task_id, aweme_id, auto_summary):
        calls.append(aweme_id)
        return {"status": "completed", "skipped": True}

    queue = await queue_factory(execute=execute)
    batch = await queue.create(["10001", "10002", "10003"])
    for _ in range(3):
        assert await queue.process_next()
    view = await queue.get(batch["batch_id"])
    assert view["status"] == "completed"
    assert view["completed"] == view["skipped"] == view["total"] == 3
    assert view["pending"] == view["failed"] == view["cancelled"] == 0
    assert view["next_allowed_at"] == view["next_in_seconds"] == 0
    with sqlite3.connect(queue.db_path) as db:
        assert db.execute("SELECT DISTINCT status FROM download_queue_items").fetchall() == [("skipped",)]
    await queue.shutdown()
    restored = await queue_factory(execute=execute)
    assert await restored.get(batch["batch_id"]) == view
    assert not await restored.process_next()
    assert calls == ["10001", "10002", "10003"]


async def test_local_skip_preserves_prior_download_cooldown(queue_factory):
    clock, calls = [1000.0], []

    async def execute(task_id, aweme_id, auto_summary):
        calls.append((aweme_id, clock[0]))
        return {"status": "completed", "skipped": aweme_id == "10002"}

    queue = await queue_factory(execute=execute, now=lambda: clock[0])
    batch = await queue.create(["10001", "10002", "10003"])
    assert await queue.process_next()
    clock[0] = 1179.99
    assert not await queue.process_next()
    assert calls == [("10001", 1000)]
    clock[0] = 1180
    assert await queue.process_next()
    skipped = await queue.get(batch["batch_id"])
    assert skipped["next_allowed_at"] == 1180
    assert skipped["completed"] == 2 and skipped["skipped"] == 1
    assert skipped["pending"] == 1 and skipped["total"] == 3
    assert await queue.process_next()
    finished = await queue.get(batch["batch_id"])
    assert finished["status"] == "completed" and finished["completed"] == 3
    assert finished["skipped"] == 1 and finished["next_allowed_at"] == 1360
    assert calls == [("10001", 1000), ("10002", 1180), ("10003", 1180)]


async def test_failed_callback_cannot_claim_local_skip_to_avoid_cooldown(queue_factory):
    async def execute(*args):
        return {"status": "failed", "skipped": True}

    queue = await queue_factory(execute=execute)
    batch = await queue.create(["10001"])
    assert await queue.process_next()
    view = await queue.get(batch["batch_id"])
    assert view["status"] == "paused" and view["failed"] == 1
    assert view["completed"] == view["skipped"] == 0
    assert view["next_allowed_at"] == 1180


async def test_concurrent_process_next_never_downloads_in_parallel(queue_factory):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def execute(task_id, aweme_id, auto_summary):
        calls.append(aweme_id)
        started.set()
        await release.wait()
        return {"status": "completed"}

    queue = await queue_factory(execute=execute)
    await queue.create(["10001", "10002"])
    first = asyncio.create_task(queue.process_next())
    await asyncio.wait_for(started.wait(), 1)
    second = asyncio.create_task(queue.process_next())
    await asyncio.sleep(0)
    assert calls == ["10001"]
    release.set()
    assert await first
    assert not await second
    assert calls == ["10001"]


async def test_task_id_is_persisted_before_first_network_callback(queue_factory):
    queue = None

    async def execute(task_id, aweme_id, auto_summary):
        with sqlite3.connect(queue.db_path) as db:
            row = db.execute(
                "SELECT task_id, status FROM download_queue_items WHERE aweme_id = ?", (aweme_id,)
            ).fetchone()
        assert row == (task_id, "running")
        return {"status": "completed"}

    queue = await queue_factory(execute=execute)
    await queue.create(["10001"])
    assert await queue.process_next()


async def test_restart_keeps_completed_and_paused_without_repeating(queue_factory):
    clock, prepared = [1000.0], []

    async def prepare(aweme_id, author, profile_id):
        prepared.append(aweme_id)
        return f"task-{aweme_id}"

    queue = await queue_factory(prepare=prepare, now=lambda: clock[0])
    batch = await queue.create(["10001", "10002"])
    paused = await queue.create(["10003"])
    await queue.control(paused["batch_id"], "pause")
    assert await queue.process_next()
    await queue.shutdown()
    queue = await queue_factory(prepare=prepare, now=lambda: clock[0])
    assert not await queue.process_next()
    assert (await queue.get(paused["batch_id"]))["status"] == "paused"
    clock[0] = 1180
    assert await queue.process_next()
    assert prepared == ["10001", "10002"]
    view = await queue.get(batch["batch_id"])
    assert view["status"] == "completed"
    assert view["completed"] == 2


async def test_restart_running_item_waits_and_reuses_saved_task_id(queue_factory):
    clock, prepared, executed = [1000.0], [], []

    async def prepare(*args):
        prepared.append(args)
        return "wrong-new-task"

    async def execute(task_id, aweme_id, auto_summary):
        executed.append(task_id)
        return {"status": "completed"}

    queue = await queue_factory(prepare=prepare, execute=execute, now=lambda: clock[0])
    batch = await queue.create(["10001"])
    await queue._db.execute(
        "UPDATE download_queue_items SET status = 'running', task_id = 'existing-task'"
    )
    await queue._db.execute("UPDATE download_queue_batches SET status = 'running'")
    await queue._db.commit()
    await queue.shutdown()
    queue = await queue_factory(prepare=prepare, execute=execute, now=lambda: clock[0])
    assert not await queue.process_next()
    assert (await queue.get(batch["batch_id"]))["next_allowed_at"] == 1180
    clock[0] = 1180
    assert await queue.process_next()
    assert prepared == []
    assert executed == ["existing-task"]


async def test_pause_stop_do_not_interrupt_current_and_prevent_following(queue_factory):
    started, release = asyncio.Event(), asyncio.Event()
    clock = [1000.0]

    async def execute(*args):
        started.set()
        await release.wait()
        return {"status": "completed"}

    queue = await queue_factory(execute=execute, now=lambda: clock[0])
    batch = await queue.create(["10001", "10002"])
    work = asyncio.create_task(queue.process_next())
    await asyncio.wait_for(started.wait(), 1)
    assert (await queue.control(batch["batch_id"], "pause"))["status"] == "paused"
    release.set()
    await work
    view = await queue.get(batch["batch_id"])
    assert view["completed"] == 1 and view["pending"] == 1
    clock[0] = 2000
    assert not await queue.process_next()
    await queue.control(batch["batch_id"], "resume")
    stopped = await queue.control(batch["batch_id"], "stop")
    assert stopped["status"] == "cancelled"
    assert stopped["cancelled"] == 1 and stopped["completed"] == 1
    assert not await queue.process_next()


async def test_failure_pauses_and_retry_reuses_task_without_sensitive_error(queue_factory, tmp_path):
    clock, attempt, prepared = [1000.0], [0], []
    secret = "sk-test-public-placeholder-only"

    async def prepare(aweme_id, *args):
        prepared.append(aweme_id)
        return f"task-{aweme_id}"

    async def execute(*args):
        attempt[0] += 1
        if attempt[0] == 1:
            raise RuntimeError(secret)
        return {"status": "completed"}

    queue = await queue_factory(prepare=prepare, execute=execute, now=lambda: clock[0])
    batch = await queue.create(["10001", "10002"])
    assert await queue.process_next()
    failed = await queue.get(batch["batch_id"])
    assert failed["status"] == "paused" and failed["failed"] == 1
    assert failed["error"]["code"] == "DOWNLOAD_QUEUE_ITEM_FAILED"
    await queue.control(batch["batch_id"], "resume")
    assert not await queue.process_next()
    clock[0] = 1180
    assert await queue.process_next()
    assert prepared == ["10001"]
    assert (await queue.get(batch["batch_id"]))["completed"] == 1
    assert secret.encode() not in (tmp_path / "queue.db").read_bytes()


async def test_worker_shutdown_preserves_current_for_restart(queue_factory):
    clock, started = [1000.0], asyncio.Event()
    calls = []

    async def execute(task_id, *args):
        calls.append(task_id)
        started.set()
        await asyncio.Event().wait()

    queue = await queue_factory(execute=execute, now=lambda: clock[0])
    batch = await queue.create(["10001"])
    await queue.start()
    await asyncio.wait_for(started.wait(), 1)
    await queue.shutdown()
    restored = await queue_factory(now=lambda: clock[0])
    view = await restored.get(batch["batch_id"])
    assert view["status"] == "queued" and view["pending"] == 1
    assert not await restored.process_next()
    clock[0] = 1180
    assert await restored.process_next()
    assert (await restored.get(batch["batch_id"]))["status"] == "completed"


async def test_safe_callback_diagnostic_keeps_only_user_facing_fields(queue_factory):
    async def execute(*args):
        return {"status": "failed", "error": {
            "code": "COOKIE_REQUIRED", "reason": "需要重新登录抖音", "action": "请扫码登录后继续",
            "response": "不应保存的上游响应", "api_key": "不应保存的密钥",
        }}

    queue = await queue_factory(execute=execute)
    batch = await queue.create(["10001"])
    await queue.process_next()
    assert (await queue.get(batch["batch_id"]))["error"] == {
        "code": "COOKIE_REQUIRED", "reason": "需要重新登录抖音", "action": "请扫码登录后继续",
    }


@pytest.mark.parametrize("changes", [
    {"video_ids": ["10002", "10001"]},
    {"video_ids": ["10001"]},
    {"author": "另一位作者"},
    {"profile_id": "another-profile"},
    {"auto_summary": False},
])
async def test_same_request_id_rejects_different_payload(queue_factory, changes):
    queue = await queue_factory()
    arguments = {
        "video_ids": ["10001", "10002", "10001"], "author": "作者",
        "profile_id": "profile", "client_request_id": "request-1", "auto_summary": True,
    }
    original = await queue.create(**arguments)
    with pytest.raises(ValueError, match="同一提交编号"):
        await queue.create(**(arguments | changes))
    same = await queue.create(**(arguments | {"video_ids": ["10001", "10002"]}))
    assert same["batch_id"] == original["batch_id"]
    assert len(await queue.list_batches()) == 1


async def test_payload_limits_align_with_api_and_summary_flag_is_visible(queue_factory):
    queue = await queue_factory()
    batch = await queue.create(
        [str(10000 + index) for index in range(20000)], author="作" * 80,
        profile_id="p" * 256, client_request_id="r" * 128, auto_summary=False,
    )
    assert batch["total"] == 20000
    assert batch["auto_summary"] is False
    assert (await queue.get(batch["batch_id"]))["auto_summary"] is False
    for arguments in (
        {"video_ids": ["10001"] * 20001},
        {"video_ids": ["10001"], "author": "作" * 81},
        {"video_ids": ["10001"], "profile_id": "p" * 257},
        {"video_ids": ["10001"], "client_request_id": "r" * 129},
        {"video_ids": ["１２３４５"]},
    ):
        with pytest.raises(ValueError):
            await queue.create(**arguments)
    assert len(await queue.list_batches()) == 1


async def test_worker_storage_error_pauses_safely_and_can_resume(queue_factory, monkeypatch, caplog):
    clock, calls = [1000.0], []
    secret = "sk-test-public-placeholder-only"

    async def execute(task_id, *args):
        calls.append(task_id)
        return {"status": "completed"}

    queue = await queue_factory(execute=execute, now=lambda: clock[0])
    batch = await queue.create(["10001"])
    original_fetchone = queue._fetchone
    failed = False

    async def fail_storage_once(sql, params=()):
        nonlocal failed
        if not failed and asyncio.current_task() is queue._worker and sql.startswith("SELECT not_before"):
            failed = True
            raise sqlite3.OperationalError(secret)
        return await original_fetchone(sql, params)

    monkeypatch.setattr(queue, "_fetchone", fail_storage_once)
    monkeypatch.setattr(queue_module, "logger", logging.getLogger("download_queue_test"))
    await queue.start()
    for _ in range(100):
        view = await queue.get(batch["batch_id"])
        if view["status"] == "paused":
            break
        await asyncio.sleep(0.01)
    assert view["status"] == "paused"
    assert view["error"]["code"] == "DOWNLOAD_QUEUE_WORKER_FAILED"
    assert calls == []
    assert not queue._worker.done()
    assert "OperationalError" in caplog.text and secret not in caplog.text
    clock[0] = 1180
    await queue.control(batch["batch_id"], "resume")
    for _ in range(100):
        view = await queue.get(batch["batch_id"])
        if view["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert view["status"] == "completed"
    assert calls == ["task-10001"]


async def test_resume_recreates_deleted_task_and_persists_before_request(queue_factory):
    clock, prepared, calls = [1000.0], [], []
    deleted, queue = [False], None

    async def prepare(*args):
        task_id = f"task-{len(prepared) + 1}"
        prepared.append(task_id)
        return task_id

    async def execute(task_id, aweme_id, auto_summary):
        calls.append(task_id)
        with sqlite3.connect(queue.db_path) as db:
            saved = db.execute(
                "SELECT task_id FROM download_queue_items WHERE aweme_id = ?", (aweme_id,)
            ).fetchone()[0]
        assert saved == task_id
        if len(calls) == 1:
            return {"status": "failed"}
        if deleted[0] and task_id == "task-1":
            return {"status": "missing"}
        return {"status": "completed"}

    queue = await queue_factory(prepare=prepare, execute=execute, now=lambda: clock[0])
    batch = await queue.create(["10001"])
    await queue.process_next()
    assert (await queue.get(batch["batch_id"]))["status"] == "paused"
    deleted[0] = True
    clock[0] = 1180
    await queue.control(batch["batch_id"], "resume")
    assert await queue.process_next()
    assert calls == ["task-1", "task-1", "task-2"]
    assert prepared == ["task-1", "task-2"]
    assert (await queue.get(batch["batch_id"]))["status"] == "completed"


async def test_missing_task_is_recreated_only_once_per_attempt(queue_factory):
    prepared, calls = [], []

    async def prepare(*args):
        task_id = f"task-{len(prepared) + 1}"
        prepared.append(task_id)
        return task_id

    async def execute(task_id, *args):
        calls.append(task_id)
        return {"status": "missing"}

    queue = await queue_factory(prepare=prepare, execute=execute)
    batch = await queue.create(["10001"])
    assert await queue.process_next()
    assert prepared == calls == ["task-1", "task-2"]
    view = await queue.get(batch["batch_id"])
    assert view["status"] == "paused" and view["failed"] == 1


def test_default_interval_is_thirty_seconds_and_keeps_minimum(tmp_path):
    async def callback(*args):
        raise AssertionError("构造队列不应调用接口")

    queue = PersistentDownloadQueue(str(tmp_path / "queue.db"), callback, callback)
    assert queue.interval_seconds == 30
    with pytest.raises(ValueError, match="30 秒"):
        PersistentDownloadQueue(str(tmp_path / "queue.db"), callback, callback, interval_seconds=29)


async def test_thirty_second_interval_keeps_serial_downloads_and_existing_cooldown(queue_factory):
    clock, calls = [1000.0], []

    async def execute(task_id, aweme_id, auto_summary):
        calls.append((aweme_id, clock[0]))
        clock[0] += 5
        return {"status": "completed"}

    queue = await queue_factory(execute=execute, now=lambda: clock[0], interval_seconds=30)
    batch = await queue.create(["10001", "10002"])
    # 旧90秒冷却不因升级或暂停/恢复而缩短。
    await queue._db.execute("UPDATE download_queue_state SET not_before = 1090")
    await queue._db.commit()
    assert not await queue.process_next()
    await queue.control(batch["batch_id"], "pause")
    await queue.control(batch["batch_id"], "resume")
    assert (await queue.get(batch["batch_id"]))["next_allowed_at"] == 1090
    clock[0] = 1090
    assert await queue.process_next()
    assert (await queue.get(batch["batch_id"]))["next_allowed_at"] == 1125
    clock[0] = 1124
    assert not await queue.process_next()
    clock[0] = 1125
    assert await queue.process_next()
    assert calls == [("10001", 1090), ("10002", 1125)]


async def test_cooldown_preflight_skips_later_local_items_without_network(queue_factory):
    clock, checked, prepared, executed = [1000.0], [], [], []

    async def reuse(aweme_id, author, profile_id, auto_summary):
        checked.append((aweme_id, author, profile_id, auto_summary))
        return f"local-{aweme_id}" if aweme_id != "10001" else None

    async def prepare(aweme_id, *args):
        prepared.append(aweme_id)
        return f"task-{aweme_id}"

    async def execute(task_id, aweme_id, auto_summary):
        executed.append((task_id, aweme_id, clock[0]))
        return {"status": "completed"}

    queue = await queue_factory(prepare, execute, now=lambda: clock[0], reuse=reuse, interval_seconds=90)
    first = await queue.create(["10001", "10002"], "作者甲", "profile-a")
    second = await queue.create(["10003"], "作者乙", "profile-b", "request-second", auto_summary=False)
    await queue._db.execute("UPDATE download_queue_state SET not_before = 1090")
    await queue._db.commit()
    assert await queue.process_next()
    waiting, finished = await queue.get(first["batch_id"]), await queue.get(second["batch_id"])
    assert waiting["completed"] == waiting["skipped"] == waiting["pending"] == 1
    assert finished["status"] == "completed" and finished["completed"] == finished["skipped"] == 1
    assert waiting["next_allowed_at"] == finished["next_allowed_at"] == 1090
    assert prepared == executed == []
    assert checked == [("10001", "作者甲", "profile-a", True), ("10002", "作者甲", "profile-a", True),
                       ("10003", "作者乙", "profile-b", False)]
    assert not await queue.process_next()
    assert len(checked) == 3
    clock[0] = 1090
    assert await queue.process_next()
    assert prepared == ["10001"] and executed == [("task-10001", "10001", 1090)]
    view = await queue.get(first["batch_id"])
    assert view["status"] == "completed" and view["completed"] == view["total"] == 2
    assert view["skipped"] == 1 and view["pending"] == 0 and view["next_allowed_at"] == 1180
    await queue.shutdown()
    restored = await queue_factory(reuse=reuse, now=lambda: clock[0], interval_seconds=90)
    assert await restored.get(first["batch_id"]) == view
    assert not await restored.process_next()
    same = await restored.create(["10003"], "作者乙", "profile-b", "request-second", auto_summary=False)
    assert same == await restored.get(second["batch_id"])
    assert len(checked) == 3


@pytest.mark.parametrize("action, expected_status", [("pause", "paused"), ("stop", "cancelled")])
async def test_preflight_rechecks_pause_or_stop_before_persisting_skip(queue_factory, action, expected_status):
    started, release, calls = asyncio.Event(), asyncio.Event(), []

    async def reuse(aweme_id, *args):
        calls.append(aweme_id)
        started.set()
        await release.wait()
        return f"local-{aweme_id}"

    async def must_not_call(*args):
        raise AssertionError("暂停或停止后不应准备或下载")

    queue = await queue_factory(must_not_call, must_not_call, reuse=reuse, interval_seconds=90)
    batch = await queue.create(["10001", "10002"])
    work = asyncio.create_task(queue.process_next())
    await asyncio.wait_for(started.wait(), 1)
    await queue.control(batch["batch_id"], action)
    release.set()
    assert not await work
    view = await queue.get(batch["batch_id"])
    assert view["status"] == expected_status and view["skipped"] == view["completed"] == 0
    assert view["next_allowed_at"] == 0
    assert calls == ["10001"]
    await queue.shutdown()
    restored = await queue_factory(must_not_call, must_not_call, reuse=reuse, interval_seconds=90)
    assert await restored.get(batch["batch_id"]) == view
    assert not await restored.process_next()


async def test_preflight_is_bounded_and_absent_files_are_rechecked_after_resume(queue_factory):
    checked, available = [], set()

    async def reuse(aweme_id, *args):
        checked.append(aweme_id)
        return f"local-{aweme_id}" if aweme_id in available else None

    async def must_not_call(*args):
        raise AssertionError("冷却期间不应准备或下载")

    queue = await queue_factory(must_not_call, must_not_call, reuse=reuse, interval_seconds=90)
    ids = [str(10000 + index) for index in range(130)]
    batch = await queue.create(ids)
    await queue._db.execute("UPDATE download_queue_state SET not_before = 1090")
    await queue._db.commit()
    assert not await queue.process_next() and len(checked) == 64
    assert not await queue.process_next() and len(checked) == 128
    assert not await queue.process_next() and len(checked) == 130
    assert not await queue.process_next() and len(checked) == 130
    available.update(ids[:2])
    await queue.control(batch["batch_id"], "pause")
    await queue.control(batch["batch_id"], "resume")
    assert await queue.process_next()
    view = await queue.get(batch["batch_id"])
    assert view["completed"] == view["skipped"] == 2 and view["pending"] == 128
    assert view["total"] == 130 and view["next_allowed_at"] == 1090


async def test_preflight_failure_stops_before_prepare_or_execute(queue_factory):
    calls = []

    async def reuse(*args):
        raise OSError("本地读取失败")

    async def must_not_call(*args):
        calls.append(args)

    queue = await queue_factory(must_not_call, must_not_call, reuse=reuse, interval_seconds=90)
    batch = await queue.create(["10001"])
    with pytest.raises(OSError):
        await queue.process_next()
    await queue._pause_after_worker_error()
    view = await queue.get(batch["batch_id"])
    assert view["status"] == "paused" and view["pending"] == 1 and view["skipped"] == 0
    assert "90 秒" in view["error"]["action"] and calls == []


async def test_file_missing_in_preflight_can_still_be_reused_at_execution(queue_factory):
    clock, available, checked = [1000.0], [False], []

    async def reuse(aweme_id, *args):
        checked.append(aweme_id)
        return f"local-{aweme_id}" if available[0] else None

    async def execute(*args):
        assert available[0]
        return {"status": "completed", "skipped": True}

    queue = await queue_factory(execute=execute, reuse=reuse, now=lambda: clock[0], interval_seconds=90)
    batch = await queue.create(["10001"])
    await queue._db.execute("UPDATE download_queue_state SET not_before = 1090")
    await queue._db.commit()
    assert not await queue.process_next()
    available[0], clock[0] = True, 1090
    assert await queue.process_next()
    view = await queue.get(batch["batch_id"])
    assert checked == ["10001"]
    assert view["status"] == "completed" and view["completed"] == view["skipped"] == 1
    assert view["next_allowed_at"] == 1090
