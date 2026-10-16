#!/usr/bin/env python3
"""
执行下载用户作品功能的端到端测试

使用方法:
    python run_user_posts_test.py

环境要求:
    - 前端服务器运行在 http://localhost:5173
    - 后端服务器运行在 http://localhost:8080
    - Playwright 已安装
"""

import subprocess
import sys
import os

# 测试文件路径
TEST_FILE = "tests/e2e/test_user_posts_playwright.py"


# 检查环境
def check_environment():
    """检查测试环境是否就绪"""
    import httpx
    import asyncio

    async def _check():
        print("检查测试环境...")

        # 检查后端服务
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "http://localhost:8080/api/status", timeout=5
                )
                if response.status_code == 200:
                    print("✓ 后端服务运行正常 (http://localhost:8080)")
                else:
                    print(f"✗ 后端服务返回异常状态码: {response.status_code}")
                    return False
        except Exception as e:
            print(f"✗ 无法连接到后端服务: {e}")
            print("  请确保后端服务已启动: python -m skills.douyin_downloader.web")
            return False

        # 检查前端服务
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get("http://localhost:5173", timeout=5)
                if response.status_code == 200:
                    print("✓ 前端服务运行正常 (http://localhost:5173)")
                else:
                    print(f"⚠ 前端服务返回状态码: {response.status_code}")
        except Exception as e:
            print(f"⚠ 无法连接到前端开发服务器: {e}")
            print("  请确保前端服务已启动: cd frontend && npm run dev")
            return False

        return True

    return asyncio.run(_check())


def run_tests():
    """运行测试"""
    print("\n" + "=" * 60)
    print("启动下载用户作品功能端到端测试")
    print("=" * 60)

    # 构建pytest命令
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        TEST_FILE,
        "-v",  # 详细输出
        "-s",  # 显示print输出
        "--tb=short",  # 简短的traceback
        "--html=tests/reports/user_posts_test_report.html",  # HTML报告
        "--self-contained-html",  # 自包含的HTML报告
    ]

    # 添加额外选项
    if "--headed" in sys.argv:
        # 有头模式运行（可以看到浏览器窗口）
        os.environ["PLAYWRIGHT_HEADLESS"] = "false"
        print("模式: 有头模式 (可以看到浏览器操作)")
    else:
        print("模式: 无头模式 (后台运行)")
        print("提示: 添加 --headed 参数以查看浏览器操作")

    print("-" * 60)

    # 执行测试
    try:
        result = subprocess.run(cmd, cwd="/d/autogen_projects")
        return result.returncode
    except KeyboardInterrupt:
        print("\n测试被用户中断")
        return 130
    except Exception as e:
        print(f"\n测试执行失败: {e}")
        return 1


def main():
    """主函数"""
    # 检查环境
    if not check_environment():
        print("\n环境检查失败，请确保前后端服务都已启动")
        print("\n启动命令:")
        print("  后端: python -m skills.douyin_downloader.web")
        print("  前端: cd frontend && npm run dev")
        return 1

    # 运行测试
    exit_code = run_tests()

    # 输出结果
    print("\n" + "=" * 60)
    if exit_code == 0:
        print("✓ 所有测试通过!")
    else:
        print(f"✗ 测试失败 (退出码: {exit_code})")
    print("=" * 60)

    # 提示报告位置
    report_path = "tests/reports/user_posts_test_report.html"
    if os.path.exists(f"/d/autogen_projects/{report_path}"):
        print(f"\n测试报告: {report_path}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
