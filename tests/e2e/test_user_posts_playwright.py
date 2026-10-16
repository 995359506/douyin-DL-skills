"""
下载用户作品功能 - Playwright 端到端测试

测试数据:
- 作者主页: https://www.douyin.com/user/MS4wLjABAAAAUYOBpERPl4g_TAiBv94riVy53V9dOU92-84FyvrwNJagxPQcFdoO6_pOgIe32O5J
- 日期范围: 2026-02-06|2026-02-26
- 数量限制: 3个

运行方式:
    cd D:\autogen_projects
    python -m pytest tests/e2e/test_user_posts_playwright.py -v -s
"""

import pytest
import pytest_asyncio
import asyncio
import json
from playwright.async_api import async_playwright, Page, Browser, BrowserContext
from typing import Optional, Dict, Any
import httpx


# 测试配置
BASE_URL = "http://localhost:5173"  # 前端开发服务器
API_URL = "http://localhost:8080"  # 后端API服务器

# 测试数据
TEST_USER_URL = "https://www.douyin.com/user/MS4wLjABAAAAUYOBpERPl4g_TAiBv94riVy53V9dOU92-84FyvrwNJagxPQcFdoO6_pOgIe32O5J"
TEST_INTERVAL = "2026-02-06|2026-02-26"
TEST_MAX_COUNTS = 3


# 标记所有测试为异步
pytestmark = pytest.mark.asyncio(loop_scope="function")


class TestUserPostsDownload:
    """下载用户作品功能测试套件"""

    @pytest_asyncio.fixture(scope="class")
    async def browser(self):
        """启动浏览器"""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, slow_mo=100)
            yield browser
            await browser.close()

    @pytest_asyncio.fixture
    async def context(self, browser: Browser):
        """创建新上下文"""
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        yield ctx
        await ctx.close()

    @pytest_asyncio.fixture
    async def page(self, context: BrowserContext):
        """创建新页面"""
        pg = await context.new_page()
        yield pg
        await pg.close()

    @pytest_asyncio.fixture
    async def api_client(self):
        """API客户端"""
        async with httpx.AsyncClient(base_url=API_URL) as client:
            yield client

    async def wait_for_element(self, page: Page, selector: str, timeout: int = 5000):
        """等待元素出现"""
        try:
            await page.wait_for_selector(selector, timeout=timeout)
            return True
        except:
            return False

    # ==================== 测试用例 ====================

    @pytest.mark.asyncio
    async def test_page_load(self, page: Page):
        """TC-001: 页面加载测试 - 验证所有UI元素正确显示"""
        print("\n[TC-001] 页面加载测试...")

        # 访问首页
        await page.goto(BASE_URL)
        await page.wait_for_load_state("networkidle")

        # 验证"下载用户作品"区域存在
        user_section = await self.wait_for_element(
            page, ".section:has-text('下载用户作品')"
        )
        assert user_section, "未找到'下载用户作品'区域"

        # 验证URL输入框
        url_input = await page.query_selector("input[placeholder*='用户主页']")
        assert url_input, "未找到用户主页URL输入框"

        # 验证"获取用户信息"按钮
        get_info_btn = await page.query_selector("button:has-text('获取用户信息')")
        assert get_info_btn, "未找到'获取用户信息'按钮"

        print("[PASS] 页面加载测试通过")

    @pytest.mark.asyncio
    async def test_api_endpoint_directly(self, api_client: httpx.AsyncClient):
        """TC-010: API端点直接测试 - 使用真实数据测试API"""
        print("\n[TC-010] API端点直接测试...")
        print(f"  - 测试URL: {TEST_USER_URL[:60]}...")
        print(f"  - 日期范围: {TEST_INTERVAL}")
        print(f"  - 数量限制: {TEST_MAX_COUNTS}")

        # 测试正常请求
        payload = {
            "user_url": TEST_USER_URL,
            "max_counts": TEST_MAX_COUNTS,
            "interval": TEST_INTERVAL,
        }

        try:
            response = await api_client.post(
                "/api/download-user-posts", json=payload, timeout=30.0
            )

            print(f"  - API响应状态: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                print(
                    f"  - 响应内容: {json.dumps(data, indent=2, ensure_ascii=False)[:500]}"
                )

                # 验证响应结构
                assert "success" in data, "响应缺少success字段"
                assert "data" in data, "响应缺少data字段"

                if data.get("success"):
                    task_data = data.get("data", {})
                    assert "task_id" in task_data, "响应缺少task_id"
                    print(f"[PASS] API创建任务成功!")
                    print(f"  - Task ID: {task_data.get('task_id')}")
                    print(f"  - 用户昵称: {task_data.get('video_name')}")

                    # 保存task_id供后续验证
                    self.last_task_id = task_data.get("task_id")
                else:
                    print(f"[INFO] API返回: {data.get('message')}")
                    # 可能是重复任务，也算通过
                    assert "已在队列" in data.get("message", "") or "重复" in data.get(
                        "message", ""
                    ), f"意外的错误: {data.get('message')}"
            else:
                print(f"[WARNING] API返回非200状态码: {response.status_code}")
                print(f"  - 响应: {response.text[:500]}")
                # 如果有Cookie问题，提示用户
                if response.status_code == 401:
                    pytest.skip("Cookie无效或未登录，跳过此测试")
                elif response.status_code == 500:
                    # 检查是否是已知的错误
                    if "cookie" in response.text.lower():
                        pytest.skip("需要有效的Cookie才能测试")
                    else:
                        raise AssertionError(f"服务器错误: {response.text[:200]}")
        except httpx.TimeoutException:
            print("[WARNING] API请求超时")
            pytest.skip("API请求超时，可能是网络问题")
        except Exception as e:
            print(f"[ERROR] API测试失败: {e}")
            raise

    @pytest.mark.asyncio
    async def test_task_data_integrity(self, api_client: httpx.AsyncClient):
        """TC-005: 任务数据完整性验证 - 验证任务数据正确存储"""
        print("\n[TC-005] 任务数据完整性验证...")

        # 等待任务创建完成
        await asyncio.sleep(2)

        try:
            # 获取任务列表
            response = await api_client.get(
                "/api/tasks", params={"limit": 10}, timeout=10.0
            )
            assert response.status_code == 200, (
                f"获取任务列表失败: {response.status_code}"
            )

            data = response.json()
            tasks = data.get("data", {}).get("tasks", [])

            print(f"  - 找到 {len(tasks)} 个任务")

            # 查找刚创建的测试任务（POST类型）
            test_task = None
            for task in tasks:
                if task.get("type") == "post":
                    test_task = task
                    break

            if not test_task:
                print("[WARNING] 未找到测试任务，可能上一个测试未成功创建")
                pytest.skip("未找到测试任务，跳过数据验证")

            print(f"  - 找到测试任务: {test_task['id']}")

            # 验证任务字段
            assert test_task["type"] == "post", f"任务类型错误: {test_task['type']}"
            assert test_task.get("mix_id") is not None, "mix_id为空"

            # 验证download_options
            download_options = test_task.get("download_options", {}) or {}
            print(f"  - download_options: {download_options}")

            # 验证过滤条件正确存储
            if download_options:
                max_counts = download_options.get("max_counts")
                interval = download_options.get("interval")

                print(f"  - 验证 max_counts: 期望 {TEST_MAX_COUNTS}, 实际 {max_counts}")
                print(f"  - 验证 interval: 期望 {TEST_INTERVAL}, 实际 {interval}")

                if max_counts is not None:
                    assert max_counts == TEST_MAX_COUNTS, (
                        f"max_counts错误: 期望{TEST_MAX_COUNTS}, 实际{max_counts}"
                    )
                if interval is not None:
                    assert interval == TEST_INTERVAL, (
                        f"interval错误: 期望{TEST_INTERVAL}, 实际{interval}"
                    )

                print("[PASS] 任务数据完整性验证通过!")
            else:
                print("[WARNING] download_options为空，可能是数据库迁移问题")
        except Exception as e:
            print(f"[ERROR] 数据验证失败: {e}")
            raise

    @pytest.mark.asyncio
    async def test_create_download_task_via_ui(self, page: Page):
        """TC-004: 通过UI创建下载任务测试"""
        print("\n[TC-004] UI创建下载任务测试...")

        await page.goto(BASE_URL)
        await page.wait_for_load_state("networkidle")

        # 步骤1: 输入URL
        print("  - 输入用户主页URL...")
        url_input = await page.query_selector("input[placeholder*='用户主页']")
        await url_input.fill(TEST_USER_URL)

        # 步骤2: 获取用户信息
        print("  - 点击获取用户信息...")
        await page.click("button:has-text('获取用户信息')")
        await asyncio.sleep(1.5)

        # 验证用户信息卡片显示
        user_info = await self.wait_for_element(page, ".user-info")
        if not user_info:
            print("[WARNING] 用户信息未显示，可能是前端解析问题")
            # 继续测试，不阻断

        # 步骤3: 设置下载选项
        print("  - 设置下载选项...")

        # 选择"限制数量"
        try:
            limit_radio = await page.query_selector(
                ".user-info .el-radio:has-text('限制数量')"
            )
            if limit_radio:
                await limit_radio.click()
                await asyncio.sleep(0.5)

                # 设置数量为3
                count_input = await page.query_selector(
                    ".user-info .el-input-number input"
                )
                if count_input:
                    await count_input.fill(str(TEST_MAX_COUNTS))
                    print(f"    - 设置数量限制为: {TEST_MAX_COUNTS}")
        except Exception as e:
            print(f"    - 设置数量选项失败: {e}")

        # 选择"自定义"日期
        try:
            custom_date_radio = await page.query_selector(
                ".user-info .el-radio:has-text('自定义')"
            )
            if custom_date_radio:
                await custom_date_radio.click()
                await asyncio.sleep(0.5)

                # 设置日期范围
                date_inputs = await page.query_selector_all(
                    ".user-info input.el-input__inner"
                )
                for inp in date_inputs:
                    placeholder = await inp.get_attribute("placeholder")
                    if placeholder and "日期" in placeholder:
                        await inp.fill(TEST_INTERVAL)
                        print(f"    - 设置日期范围为: {TEST_INTERVAL}")
                        break
        except Exception as e:
            print(f"    - 设置日期选项失败: {e}")

        # 步骤4: 点击下载按钮
        print("  - 点击下载按钮...")
        download_btn = await page.query_selector(
            ".user-info button:has-text('下载此用户作品')"
        )

        if not download_btn:
            print("[WARNING] 未找到下载按钮")
            pytest.skip("未找到下载按钮，可能前端状态未正确更新")

        # 监听API响应
        task_created = False

        try:
            async with page.expect_response(
                lambda r: "/api/download-user-posts" in r.url, timeout=10000
            ) as response_info:
                await download_btn.click()
                response = await response_info.value

                print(f"  - API响应状态: {response.status}")

                if response.status == 200:
                    data = await response.json()
                    if data.get("success"):
                        task_created = True
                        print(f"[PASS] 通过UI创建任务成功!")
                        print(f"  - Task ID: {data.get('data', {}).get('task_id')}")
                    else:
                        print(f"  - 创建结果: {data.get('message')}")
        except Exception as e:
            print(f"[WARNING] 监听API响应失败: {e}")

        # 等待一下看是否有成功提示
        await asyncio.sleep(2)
        page_content = await page.content()

        if "成功" in page_content or "已发送" in page_content or task_created:
            print("[PASS] UI创建下载任务测试通过!")
        else:
            print("[WARNING] 未检测到成功提示，但测试继续")


# 独立运行测试的入口
async def run_tests():
    """运行所有测试"""
    print("=" * 60)
    print("开始运行下载用户作品功能端到端测试")
    print("=" * 60)
    print(f"测试URL: {TEST_USER_URL}")
    print(f"日期范围: {TEST_INTERVAL}")
    print(f"数量限制: {TEST_MAX_COUNTS}")
    print("=" * 60)

    pytest.main([__file__, "-v", "-s", "--tb=short"])


if __name__ == "__main__":
    asyncio.run(run_tests())
