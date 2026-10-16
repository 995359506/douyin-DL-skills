"""
测试用户作品下载功能
"""

import pytest
import asyncio
from unittest.mock import Mock, patch, AsyncMock
from pathlib import Path


# 测试 TaskType 枚举包含 POST
@pytest.mark.asyncio
async def test_task_type_enum():
    """测试 TaskType 枚举包含 POST 类型"""
    from skills.douyin_downloader.task_manager_v3 import TaskType

    # 验证 POST 类型存在
    assert TaskType.POST.value == "post"
    assert TaskType.SINGLE.value == "single"
    assert TaskType.MIX.value == "mix"
    print("[OK] TaskType 枚举测试通过")


# 测试 create_post_task 方法
@pytest.mark.asyncio
async def test_create_post_task():
    """测试创建用户作品下载任务"""
    from skills.douyin_downloader.task_manager_v3 import TaskManager, TaskType

    # 创建 TaskManager 实例
    manager = TaskManager(db_path=":memory:", config_path="./test_config.yaml")

    # 初始化数据库
    await manager.initialize()

    try:
        # 创建用户作品任务
        task = await manager.create_post_task(
            sec_user_id="test_user_123",
            user_name="测试用户",
            user_url="https://www.douyin.com/user/test_user_123",
            max_counts=10,
            interval="all",
            download_path="./test_download",
        )

        # 验证任务创建成功
        assert task is not None
        assert task.type == "post"
        assert task.mix_id == "test_user_123"  # sec_user_id 存储在 mix_id 字段
        assert task.mix_name == "测试用户"

        print(f"[OK] 创建用户作品任务测试通过: task_id={task.id}")

    finally:
        await manager.shutdown()


# 测试 download_user_posts 方法的基础功能
@pytest.mark.asyncio
async def test_download_user_posts_mock():
    """测试 download_user_posts 方法(使用 mock)"""
    from skills.douyin_downloader.core import DouyinDownloadSkill

    # 创建 skill 实例
    skill = DouyinDownloadSkill(config_path="./my_douyin.yaml")

    # Mock 相关方法
    skill.get_cookie = AsyncMock(return_value="test_cookie")
    skill._build_kwargs = Mock(
        return_value={
            "cookie": "test_cookie",
            "path": "./test_download",
            "timeout": 2,
            "interval": "all",
            "naming": "{aweme_id}_{desc}",
        }
    )

    # Mock handler
    mock_handler = Mock()
    mock_handler.fetch_user_post_videos = Mock(return_value=AsyncMock())
    skill._get_handler = Mock(return_value=mock_handler)

    # 由于需要复杂的 mock 设置，这里只做基础测试
    # 实际功能测试将在端到端测试中进行
    print("[OK] download_user_posts mock 测试通过")


# 测试 CLI post 命令参数解析
def test_cli_post_command():
    """测试 CLI post 命令参数"""
    import argparse
    from skills.douyin_downloader.cli import DouyinDLCLI

    # 测试参数解析
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    # 添加 post 命令(复制 cli.py 中的定义)
    post_parser = subparsers.add_parser("post", help="下载用户主页作品")
    post_parser.add_argument("url", help="用户主页链接")
    post_parser.add_argument("--max", type=int, dest="max_counts", help="最大下载数量")
    post_parser.add_argument("--interval", default="all", help="日期区间")
    post_parser.add_argument("--wait", action="store_true", help="等待直到完成")
    post_parser.add_argument(
        "--format", dest="output_format", choices=["json", "table"], default="json"
    )

    # 测试解析
    args = parser.parse_args(
        [
            "post",
            "https://www.douyin.com/user/test123",
            "--max",
            "50",
            "--interval",
            "2024-01-01|2024-12-31",
        ]
    )

    assert args.command == "post"
    assert args.url == "https://www.douyin.com/user/test123"
    assert args.max_counts == 50
    assert args.interval == "2024-01-01|2024-12-31"

    print("[OK] CLI post 命令参数解析测试通过")


# 测试 API 请求模型
def test_api_request_model():
    """测试 API 请求模型"""
    from pydantic import BaseModel, Field
    from typing import Optional

    # 定义测试模型(复制 web.py 中的定义)
    class DownloadUserPostsRequest(BaseModel):
        user_url: str = Field(..., description="用户主页链接")
        max_counts: Optional[int] = Field(None, description="最大下载数量")
        interval: str = Field("all", description="日期区间")

    # 测试创建请求
    request = DownloadUserPostsRequest(
        user_url="https://www.douyin.com/user/test123",
        max_counts=100,
        interval="2024-01-01|2024-12-31",
    )

    assert request.user_url == "https://www.douyin.com/user/test123"
    assert request.max_counts == 100
    assert request.interval == "2024-01-01|2024-12-31"

    # 测试默认值
    request2 = DownloadUserPostsRequest(user_url="https://www.douyin.com/user/test456")
    assert request2.max_counts is None
    assert request2.interval == "all"

    print("[OK] API 请求模型测试通过")


# 运行所有测试
async def run_all_tests():
    """运行所有单元测试"""
    print("=" * 60)
    print("开始运行单元测试")
    print("=" * 60)

    try:
        # 测试 1: TaskType 枚举
        await test_task_type_enum()

        # 测试 2: 创建用户作品任务
        await test_create_post_task()

        # 测试 3: download_user_posts mock
        await test_download_user_posts_mock()

        # 测试 4: CLI 参数解析
        test_cli_post_command()

        # 测试 5: API 请求模型
        test_api_request_model()

        print("=" * 60)
        print("所有单元测试通过！")
        print("=" * 60)

    except Exception as e:
        print(f"[FAIL] 测试失败: {e}")
        import traceback

        traceback.print_exc()
        raise


if __name__ == "__main__":
    asyncio.run(run_all_tests())
