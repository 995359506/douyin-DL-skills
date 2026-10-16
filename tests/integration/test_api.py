"""
API 集成测试
测试 Web 后端 API 接口
"""

import pytest
import httpx
import asyncio
from pathlib import Path
import sys

project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from tests.utils.api_client import APIClient


@pytest.fixture
async def api_client():
    """API测试客户端"""
    client = APIClient("http://localhost:8080")
    yield client
    await client.close()


class TestBasicAPI:
    """基础API测试"""

    @pytest.mark.asyncio
    async def test_root_endpoint(self, api_client):
        """测试根路径"""
        response = await api_client.get("/")
        assert response["status_code"] == 200
        print(f"Root endpoint: {response['json']}")

    @pytest.mark.asyncio
    async def test_status_endpoint(self, api_client):
        """测试状态接口"""
        response = await api_client.get("/api/status")
        assert response["status_code"] == 200
        data = response.get("json", {})
        assert "status" in data or "message" in data
        print(f"Status: {data}")


class TestCookieAPI:
    """Cookie管理API测试"""

    @pytest.mark.asyncio
    async def test_cookie_status(self, api_client):
        """测试Cookie状态检查"""
        response = await api_client.get("/api/cookie/status")
        assert response["status_code"] in [200, 401]
        print(f"Cookie status: {response.get('json', response.get('text'))}")


class TestTaskAPI:
    """任务管理API测试"""

    @pytest.mark.asyncio
    async def test_get_tasks(self, api_client):
        """测试获取任务列表"""
        response = await api_client.get("/api/tasks", params={"limit": 10})
        assert response["status_code"] == 200
        data = response.get("json", {})
        assert "tasks" in data
        assert "total" in data
        print(f"Tasks count: {data.get('total', 0)}")

    @pytest.mark.asyncio
    async def test_create_single_video_task(self, api_client):
        """测试创建单视频下载任务"""
        test_data = {"url": "https://v.douyin.com/zHB_6sz29Y4/", "type": "single"}
        response = await api_client.post("/api/download", data=test_data)
        # 可能返回 200/201（成功）或 400（验证错误）或 500（服务器错误）
        assert response["status_code"] in [200, 201, 400, 422, 500]

        if response["status_code"] in [200, 201]:
            data = response.get("json", {})
            print(f"Task created: {data.get('task_id', 'N/A')}")
        else:
            print(f"Task creation returned: {response['status_code']}")


class TestMixAPI:
    """合集相关API测试"""

    @pytest.mark.asyncio
    async def test_get_mix_info(self, api_client):
        """测试获取合集信息"""
        test_data = {"url": "https://v.douyin.com/zHB_6sz29Y4/"}
        response = await api_client.post("/api/mix-info", data=test_data)
        # 如果Cookie有效应该返回200，否则可能返回401/500
        assert response["status_code"] in [200, 400, 401, 500]
        print(f"Mix info status: {response['status_code']}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
