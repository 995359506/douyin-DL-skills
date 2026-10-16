#!/usr/bin/env python3
"""
下载用户作品功能 - API测试

测试数据:
- 作者主页: https://www.douyin.com/user/MS4wLjABAAAAUYOBpERPl4g_TAiBv94riVy53V9dOU92-84FyvrwNJagxPQcFdoO6_pOgIe32O5J
- 日期范围: 2026-02-06|2026-02-26
- 数量限制: 3个
"""

import asyncio
import httpx
import json
import sys
from datetime import datetime

API_BASE_URL = "http://localhost:8081"
TEST_USER_URL = "https://www.douyin.com/user/MS4wLjABAAAAUYOBpERPl4g_TAiBv94riVy53V9dOU92-84FyvrwNJagxPQcFdoO6_pOgIe32O5J"
TEST_INTERVAL = "2026-02-06|2026-02-26"
TEST_MAX_COUNTS = 3


def log(message, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")


async def test_api_status(client):
    """测试API服务状态"""
    log("=" * 60)
    log("测试1: API服务状态检查")
    log("=" * 60)

    try:
        response = await client.get("/api/status")
        if response.status_code == 200:
            data = response.json()
            log(f"✓ 后端服务运行正常")
            log(f"  - Skill初始化: {data.get('skill_initialized', False)}")
            return True
        else:
            log(f"✗ 后端服务返回异常: {response.status_code}", "ERROR")
            return False
    except Exception as e:
        log(f"✗ 无法连接到后端服务: {e}", "ERROR")
        return False


async def test_cookie_status(client):
    """测试Cookie状态"""
    log("\n" + "=" * 60)
    log("测试2: Cookie状态检查")
    log("=" * 60)

    try:
        response = await client.get("/api/cookie/status")
        data = response.json()

        if data.get("data", {}).get("valid"):
            log("✓ Cookie有效")
            return True
        else:
            log("⚠ Cookie无效或未登录", "WARNING")
            return False
    except Exception as e:
        log(f"✗ 检查失败: {e}", "ERROR")
        return False


async def test_create_task(client):
    """测试创建下载任务"""
    log("\n" + "=" * 60)
    log("测试3: 创建下载任务")
    log("=" * 60)
    log(f"测试数据:")
    log(f"  - 日期范围: {TEST_INTERVAL}")
    log(f"  - 数量限制: {TEST_MAX_COUNTS}")
    log("-" * 60)

    payload = {
        "user_url": TEST_USER_URL,
        "max_counts": TEST_MAX_COUNTS,
        "interval": TEST_INTERVAL,
    }

    try:
        log("发送API请求...")
        response = await client.post("/api/download-user-posts", json=payload)

        log(f"响应状态码: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            log(f"响应:\n{json.dumps(data, indent=2, ensure_ascii=False)}")

            if data.get("success"):
                task_id = data.get("data", {}).get("task_id")
                log(f"✓ 任务创建成功!")
                log(f"  - Task ID: {task_id}")
                return task_id
            else:
                msg = data.get("message", "")
                if "已在队列" in msg:
                    log(f"✓ 重复任务检测正常: {msg}")
                    return "existing"
                else:
                    log(f"✗ 失败: {msg}", "ERROR")
                    return None
        elif response.status_code == 401:
            log("✗ Cookie无效 (401)", "ERROR")
            return None
        else:
            log(f"✗ 错误: {response.status_code}", "ERROR")
            return None

    except Exception as e:
        log(f"✗ 失败: {e}", "ERROR")
        return None


async def test_task_data_integrity(client, task_id):
    """验证任务数据完整性"""
    log("\n" + "=" * 60)
    log("测试4: 任务数据完整性验证")
    log("=" * 60)

    await asyncio.sleep(2)

    try:
        response = await client.get("/api/tasks", params={"limit": 10})
        data = response.json()
        tasks = data.get("data", {}).get("tasks", [])

        log(f"找到 {len(tasks)} 个任务")

        # 找POST类型的任务
        post_tasks = [t for t in tasks if t.get("type") == "post"]
        if not post_tasks:
            log("✗ 未找到POST任务", "ERROR")
            return False

        task = post_tasks[0]
        log(f"验证任务: {task['id']}")

        # 验证字段
        assert task["type"] == "post", "类型错误"
        assert task.get("mix_id") is not None, "mix_id为空"
        log("✓ 基本字段验证通过")

        # 验证download_options
        opts = task.get("download_options") or {}
        log(f"download_options: {opts}")

        if not opts:
            log("✗ download_options为空", "ERROR")
            return False

        max_counts = opts.get("max_counts")
        interval = opts.get("interval")

        log(f"验证: max_counts={max_counts}, interval={interval}")

        if max_counts != TEST_MAX_COUNTS:
            log(f"✗ max_counts不匹配!", "ERROR")
            return False

        if interval != TEST_INTERVAL:
            log(f"✗ interval不匹配!", "ERROR")
            return False

        log("✓ 数据完整性验证通过!")
        return True

    except Exception as e:
        log(f"✗ 验证失败: {e}", "ERROR")
        return False


async def main():
    log("\n" + "=" * 60)
    log("下载用户作品功能测试")
    log("=" * 60)
    log(f"API: {API_BASE_URL}")
    log("=" * 60)

    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30.0) as client:
        passed = 0
        failed = 0

        # 测试1: API状态
        if not await test_api_status(client):
            log("\n✗ API不可用，终止测试", "ERROR")
            return 1
        passed += 1

        # 测试2: Cookie
        cookie_ok = await test_cookie_status(client)
        if cookie_ok:
            passed += 1

        # 测试3: 创建任务
        task_id = await test_create_task(client)
        if task_id:
            passed += 1
            # 测试4: 数据完整性
            if await test_task_data_integrity(client, task_id):
                passed += 1
            else:
                failed += 1
        else:
            failed += 1

        # 输出总结
        log("\n" + "=" * 60)
        log("测试完成")
        log("=" * 60)
        log(f"通过: {passed}")
        log(f"失败: {failed}")
        log("=" * 60)

        return 0 if failed == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
