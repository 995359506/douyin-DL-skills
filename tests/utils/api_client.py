"""
API测试客户端工具
"""

import httpx
import asyncio
import json
from typing import Dict, Any, Optional


class APIClient:
    """API测试客户端"""

    def __init__(self, base_url: str = "http://localhost:8080"):
        self.base_url = base_url
        self.client = httpx.AsyncClient(base_url=base_url, timeout=30.0)

    async def get(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """GET请求"""
        response = await self.client.get(endpoint, params=params)
        return {
            "status_code": response.status_code,
            "json": response.json() if response.status_code == 200 else None,
            "text": response.text,
        }

    async def post(self, endpoint: str, data: Optional[Dict] = None) -> Dict:
        """POST请求"""
        response = await self.client.post(endpoint, json=data)
        return {
            "status_code": response.status_code,
            "json": response.json() if response.status_code in [200, 201] else None,
            "text": response.text,
        }

    async def delete(self, endpoint: str) -> Dict:
        """DELETE请求"""
        response = await self.client.delete(endpoint)
        return {
            "status_code": response.status_code,
            "json": response.json() if response.status_code == 200 else None,
            "text": response.text,
        }

    async def close(self):
        """关闭客户端"""
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
