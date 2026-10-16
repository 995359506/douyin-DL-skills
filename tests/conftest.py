"""
测试配置文件
"""

import pytest
import asyncio
import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 测试数据配置
TEST_CONFIG = {
    "test_video_url": "https://v.douyin.com/zHB_6sz29Y4/",
    "backend_url": "http://localhost:8080",
    "test_timeout": 300,
    "test_db_path": "./test_douyin_downloader.db",
    "download_path": "./test_downloads",
}

# pytest-asyncio配置
pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(scope="session")
def event_loop():
    """创建事件循环"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def test_config():
    """测试配置"""
    return TEST_CONFIG.copy()


@pytest.fixture
def test_video_url():
    """测试视频URL"""
    return TEST_CONFIG["test_video_url"]
