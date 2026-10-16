from skills.douyin_downloader.db import TaskInfo, TaskVideoInfo
from skills.douyin_downloader.task_manager_v3 import DownloadTask


def test_batch_task_exposes_each_video_for_gallery_rendering():
    task = DownloadTask.from_db(
        TaskInfo(
            task_id="batch-1",
            type="post",
            mix_id="sec-user-1",
            mix_name="作者 · 已选 2 条",
            status="completed",
            base_path="Download",
            total_count=2,
            downloaded_count=1,
            failed_count=1,
        ),
        [
            TaskVideoInfo(
                id=1,
                task_id="batch-1",
                aweme_id="10001",
                desc="第一条作品",
                nickname="作者",
                sec_user_id="sec-user-1",
                create_time="2026-10-07T00:00:00",
                status="downloaded",
                file_path="10001_第一条作品_video.mp4",
                file_size=123,
                error_message=None,
                download_time="2026-10-07T00:01:00",
            ),
            TaskVideoInfo(
                id=2,
                task_id="batch-1",
                aweme_id="10002",
                desc="第二条作品",
                nickname="作者",
                sec_user_id="sec-user-1",
                create_time="2026-10-07T00:00:01",
                status="failed",
                file_path=None,
                file_size=None,
                error_message="HTTP 403",
                download_time=None,
            ),
        ],
    )

    payload = task.to_dict()
    assert payload["type"] == "post"
    assert [item["aweme_id"] for item in payload["videos"]] == ["10001", "10002"]
    assert payload["videos"][0]["file_path"].endswith("video.mp4")
    assert payload["videos"][1]["error_message"] == "HTTP 403"
