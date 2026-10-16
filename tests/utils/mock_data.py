"""
测试数据生成工具
"""

import uuid
from datetime import datetime
from typing import Dict, Any


class MockDataGenerator:
    """测试数据生成器"""

    @staticmethod
    def generate_task_id() -> str:
        """生成测试任务ID"""
        return str(uuid.uuid4())

    @staticmethod
    def create_test_task_data(video_url: str = None) -> Dict[str, Any]:
        """创建测试任务数据"""
        return {
            "task_id": MockDataGenerator.generate_task_id(),
            "url": video_url or "https://v.douyin.com/zHB_6sz29Y4/",
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "type": "single",
        }

    @staticmethod
    def create_test_mix_data(mix_id: str = None) -> Dict[str, Any]:
        """创建测试合集数据"""
        return {
            "mix_id": mix_id or "test_mix_123",
            "mix_name": "测试合集",
            "total_videos": 10,
            "videos": [],
        }
