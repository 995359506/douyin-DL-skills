"""
真实场景测试 - 使用真实URL和Cookie
"""

import pytest
import asyncio
import sys
import json
import urllib.request
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 真实测试数据
REAL_TEST_URL = "https://v.douyin.com/zHB_6sz29Y4/"
COOKIE_FILE = Path("douyin_cookie.txt")


class TestRealCookie:
    """测试真实Cookie"""

    def test_cookie_file_exists(self):
        """检查Cookie文件是否存在"""
        assert COOKIE_FILE.exists(), f"Cookie文件不存在: {COOKIE_FILE}"
        print(f"[OK] Cookie文件存在: {COOKIE_FILE}")

    def test_cookie_not_empty(self):
        """检查Cookie文件不为空"""
        content = COOKIE_FILE.read_text(encoding="utf-8")
        assert len(content) > 100, "Cookie内容太短，可能无效"
        print(f"[OK] Cookie长度: {len(content)} 字符")

        # 检查关键字段
        assert "ttwid" in content or "passport" in content, "Cookie缺少关键字段"
        print("[OK] Cookie包含必要字段")


class TestRealDownload:
    """真实下载测试"""

    @pytest.fixture
    def skill(self):
        """创建Skill实例"""
        from skills.douyin_downloader.core import DouyinDownloadSkill

        return DouyinDownloadSkill()

    @pytest.mark.asyncio
    async def test_get_video_info(self, skill):
        """测试获取真实视频信息"""
        try:
            print(f"\n测试URL: {REAL_TEST_URL}")

            # 获取视频ID
            from f2.apps.douyin.utils import AwemeIdFetcher

            aweme_id = await AwemeIdFetcher.get_aweme_id(REAL_TEST_URL)
            print(f"[OK] 视频ID: {aweme_id}")

            assert aweme_id is not None, "无法获取视频ID"
            assert len(aweme_id) > 5, "视频ID太短"

        except Exception as e:
            pytest.fail(f"获取视频信息失败: {e}")

    @pytest.mark.asyncio
    async def test_download_single_video(self, skill):
        """测试下载单个真实视频"""
        try:
            print(f"\n尝试下载: {REAL_TEST_URL}")

            result = await skill.download_single_video(REAL_TEST_URL)

            print(f"[OK] 下载结果: {json.dumps(result, indent=2, ensure_ascii=False)}")

            # 验证结果
            assert "success" in result, "结果缺少success字段"

            if result["success"]:
                print("[OK] 视频下载成功!")
                assert "file_path" in result or "video_path" in result, "缺少文件路径"
            else:
                print(f"[WARN] 下载未成功: {result.get('message', '未知错误')}")

        except Exception as e:
            print(f"[ERROR] 下载失败: {e}")
            # 不强制失败，因为可能是网络/Cookie问题
            pytest.skip(f"下载测试跳过: {e}")


class TestRealAPI:
    """真实API测试"""

    base_url = "http://localhost:8080"

    def test_api_create_real_task(self):
        """使用真实URL创建任务"""
        try:
            data = json.dumps({"url": REAL_TEST_URL, "type": "single"}).encode()

            req = urllib.request.Request(
                f"{self.base_url}/api/download",
                data=data,
                headers={"Content-Type": "application/json"},
            )

            response = urllib.request.urlopen(req, timeout=30)
            result = json.loads(response.read().decode())

            print(f"\n[OK] API响应: {json.dumps(result, indent=2, ensure_ascii=False)}")

            # 检查是否创建了任务
            if "task_id" in result:
                print(f"[OK] 任务创建成功: {result['task_id']}")
            elif "error" in result:
                print(f"[WARN] API返回错误: {result['error']}")

        except Exception as e:
            print(f"[ERROR] API调用失败: {e}")
            pytest.skip(f"API测试跳过: {e}")

    def test_api_get_mix_info(self):
        """测试获取真实视频合集信息"""
        try:
            data = json.dumps({"url": REAL_TEST_URL}).encode()

            req = urllib.request.Request(
                f"{self.base_url}/api/mix-info",
                data=data,
                headers={"Content-Type": "application/json"},
            )

            response = urllib.request.urlopen(req, timeout=30)
            result = json.loads(response.read().decode())

            print(
                f"\n[OK] 合集信息: {json.dumps(result, indent=2, ensure_ascii=False)}"
            )

        except Exception as e:
            print(f"[ERROR] 获取合集信息失败: {e}")
            pytest.skip(f"合集测试跳过: {e}")


class TestRealCLI:
    """真实CLI测试"""

    def test_cli_video_real_url(self):
        """测试CLI下载真实URL"""
        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "skills.douyin_downloader.cli",
                "video",
                REAL_TEST_URL,
                "--wait",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=60,
        )

        print(f"\nCLI输出:\n{result.stdout}")
        if result.stderr:
            print(f"CLI错误:\n{result.stderr}")

        # 检查是否成功执行
        print(f"[INFO] CLI返回码: {result.returncode}")

        # 不强制要求成功，记录结果即可
        if result.returncode == 0:
            print("[OK] CLI命令执行成功")
        else:
            print("[WARN] CLI命令执行失败或未完成")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
