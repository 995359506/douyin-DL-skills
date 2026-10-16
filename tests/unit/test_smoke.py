"""
基础冒烟测试 - 验证核心功能
"""

import pytest
import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


class TestImports:
    """测试基础导入"""

    def test_task_manager_v2_import(self):
        """测试task_manager_v2导入"""
        from skills.douyin_downloader.task_manager_v2 import (
            task_manager,
            TaskStatus,
            TaskType,
        )

        assert task_manager is not None
        assert hasattr(TaskStatus, "QUEUED")
        assert hasattr(TaskStatus, "PENDING")
        assert hasattr(TaskType, "SINGLE_VIDEO")
        assert hasattr(TaskType, "MIX_VIDEO")

    def test_web_import(self):
        """测试web模块导入"""
        try:
            from skills.douyin_downloader import web

            assert hasattr(web, "app")
            print("✓ web模块导入成功")
        except ImportError as e:
            pytest.fail(f"web模块导入失败: {e}")

    def test_package_and_web_import_without_network(self):
        """导入包和 Web 模块时不得建立网络连接。"""
        code = textwrap.dedent(
            """
            import socket

            attempts = []

            def block_network(*args, **kwargs):
                attempts.append(repr(args[:2]))
                raise AssertionError("import attempted network access")

            socket.socket.connect = block_network
            socket.socket.connect_ex = block_network
            socket.create_connection = block_network
            socket.getaddrinfo = block_network

            import skills.douyin_downloader
            import skills.douyin_downloader.web

            assert not attempts, attempts
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=15,
        )

        assert result.returncode == 0, result.stdout + result.stderr


class TestTaskManagerV2:
    """测试TaskManager V2"""

    @pytest.fixture
    def task_mgr(self):
        from skills.douyin_downloader.task_manager_v2 import task_manager

        return task_manager

    def test_tasks_property(self, task_mgr):
        """测试tasks属性"""
        tasks = task_mgr.tasks
        assert isinstance(tasks, dict)

    def test_download_task_download_options(self):
        """DownloadTask.from_db_task 应解析 download_options（JSON字符串→dict）"""
        from skills.douyin_downloader.task_manager_v2 import DownloadTask
        from skills.douyin_downloader.db import TaskInfo

        info = TaskInfo(
            task_id="t1",
            type="mix",
            mix_id="m1",
            mix_name="合集",
            status="completed",
            base_path="Download",
            download_options='{"max_counts": 10, "interval": "2024-01-01|2024-12-31"}',
        )
        task = DownloadTask.from_db_task(info, [])
        assert task.download_options == {
            "max_counts": 10,
            "interval": "2024-01-01|2024-12-31",
        }
        # 无 download_options 时应回退为空 dict，不抛异常
        info2 = TaskInfo(
            task_id="t2",
            type="mix",
            mix_id="m2",
            mix_name="合集2",
            status="completed",
            base_path="Download",
        )
        task2 = DownloadTask.from_db_task(info2, [])
        assert task2.download_options == {}

    @pytest.mark.asyncio
    async def test_get_tasks(self, task_mgr):
        """测试get_tasks方法"""
        result = await task_mgr.get_tasks(limit=10)
        assert isinstance(result, dict)
        assert "tasks" in result
        assert "total" in result

    @pytest.mark.asyncio
    async def test_create_task(self, task_mgr):
        """测试创建任务"""
        from skills.douyin_downloader.task_manager_v2 import TaskType

        task = await task_mgr.create_task(
            task_type=TaskType.SINGLE_VIDEO,
            video_name="测试视频",
            file_name="test.mp4",
            original_desc="测试",
            video_info={},
            url="https://v.douyin.com/zHB_6sz29Y4/",
            download_path="Download",
        )
        assert task is not None
        assert hasattr(task, "id")
        print(f"✓ 任务创建成功: {task.id}")


class TestCleanFilename:
    """文件名清理与下载文件查找"""

    def test_segment_truncation(self):
        """多段 desc 按下划线取前 4 段"""
        from skills.douyin_downloader.core import clean_filename_for_display

        assert clean_filename_for_display(
            "7664166442036089961_第一集_第二集_第三集_第四集_第五集"
        ) == "第一集_第二集_第三集_第四集"

    def test_no_underscore_kept(self):
        """不含下划线时保持原样"""
        from skills.douyin_downloader.core import clean_filename_for_display

        assert clean_filename_for_display("7664166442036089961_单一描述") == "单一描述"

    def test_find_downloaded_file_prefers_video(self, tmp_path):
        """下载文件查找应优先视频文件（即使封面 mtime 更新）"""
        import os
        import time

        from skills.douyin_downloader.core import DouyinDownloadSkill

        aweme = "7664166442036089961"
        cover = tmp_path / f"{aweme}_desc_cover.jpeg"
        video = tmp_path / f"{aweme}_desc_video.mp4"
        cover.write_bytes(b"jpeg")
        video.write_bytes(b"mp4")
        # 让封面的 mtime 更新（旧逻辑会误选封面）
        os.utime(cover, (time.time(), time.time()))
        os.utime(video, (time.time() - 10, time.time() - 10))

        result = DouyinDownloadSkill._find_downloaded_file({"aweme_id": aweme}, tmp_path)
        assert result == video.name

    def test_find_downloaded_file_fallback_image(self, tmp_path):
        """图文作品（aweme_type=68）无视频文件时退回图片"""
        from skills.douyin_downloader.core import DouyinDownloadSkill

        aweme = "7664166442036089961"
        cover = tmp_path / f"{aweme}_desc_cover.jpeg"
        cover.write_bytes(b"jpeg")

        result = DouyinDownloadSkill._find_downloaded_file(
            {"aweme_id": aweme, "aweme_type": 68}, tmp_path
        )
        assert result == cover.name

    def test_build_kwargs_naming_upgrade(self):
        """_build_kwargs 的 naming 升级逻辑：旧默认升级、自定义保留、幂等"""
        from skills.douyin_downloader.core import DouyinDownloadSkill

        skill = DouyinDownloadSkill("./my_douyin.yaml")

        # 无 naming → 升级为截断版
        skill.douyin_conf = {}
        assert skill._build_kwargs()["naming"] == "{aweme_id}_{desc:.80}"

        # 旧默认 {aweme_id}_{desc} → 升级
        skill.douyin_conf = {"naming": "{aweme_id}_{desc}"}
        assert skill._build_kwargs()["naming"] == "{aweme_id}_{desc:.80}"

        # 旧默认 {create}_{desc} → 升级
        skill.douyin_conf = {"naming": "{create}_{desc}"}
        assert skill._build_kwargs()["naming"] == "{aweme_id}_{desc:.80}"

        # 用户自定义模板 → 保留
        skill.douyin_conf = {"naming": "{aweme_id}_{desc:.50}"}
        assert skill._build_kwargs()["naming"] == "{aweme_id}_{desc:.50}"

        # 已是新模板 → 幂等
        skill.douyin_conf = {"naming": "{aweme_id}_{desc:.80}"}
        assert skill._build_kwargs()["naming"] == "{aweme_id}_{desc:.80}"

        # 清理线程池资源
        skill._playwright_executor.shutdown(wait=False)

    def test_build_disk_file_name_matches_f2(self):
        """_build_disk_file_name 输出应与 F2 磁盘实际文件名一致（锁死契约）"""
        from skills.douyin_downloader.downloader import DouyinDownloader

        d = DouyinDownloader("./my_douyin.yaml")
        name = d._build_disk_file_name(
            {"naming": "{aweme_id}_{desc:.80}"},
            "7664166442036089961",
            "第一集_" * 40,
            "昵称",
            "2026-08-05 10:00:00",
        )
        # 与 f2 format_file_name("{aweme_id}_{desc:.80}") + "_video.mp4" 完全一致
        from f2.apps.douyin.utils import format_file_name

        base = format_file_name(
            "{aweme_id}_{desc:.80}",
            {
                "aweme_id": "7664166442036089961",
                "desc": "第一集_" * 40,
                "nickname": "昵称",
                "create_time": "2026-08-05 10:00:00",
            },
        )
        assert name == f"{base}_video.mp4"
        assert name.endswith("_video.mp4")
        assert name.startswith("7664166442036089961_")


class TestEnums:
    """测试枚举值"""

    def test_task_status_values(self):
        """测试TaskStatus值"""
        from skills.douyin_downloader.task_manager_v2 import TaskStatus

        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.QUEUED.value == "pending"  # 兼容旧版
        assert TaskStatus.DOWNLOADING.value == "downloading"
        assert TaskStatus.COMPLETED.value == "completed"
        assert TaskStatus.FAILED.value == "failed"

    def test_task_type_values(self):
        """测试TaskType值"""
        from skills.douyin_downloader.task_manager_v2 import TaskType

        assert TaskType.SINGLE.value == "single"
        assert TaskType.SINGLE_VIDEO.value == "single"  # 兼容旧版
        assert TaskType.MIX.value == "mix"
        assert TaskType.MIX_VIDEO.value == "mix"  # 兼容旧版


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
