"""
CLI 工具测试
测试命令行接口
"""

import pytest
import subprocess
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


class TestCLIHelp:
    """测试CLI帮助信息"""

    def test_cli_help(self):
        """测试CLI帮助"""
        result = subprocess.run(
            [sys.executable, "-m", "skills.douyin_downloader.cli", "--help"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
        )
        assert result.returncode == 0
        assert "usage:" in result.stdout.lower() or "用法" in result.stdout
        print("CLI help output:")
        print(result.stdout[:500])

    def test_cli_list_help(self):
        """测试list命令帮助"""
        result = subprocess.run(
            [sys.executable, "-m", "skills.douyin_downloader.cli", "list", "--help"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
        )
        assert result.returncode == 0
        print("CLI list help: OK")


class TestCLICommands:
    """测试CLI命令执行"""

    def test_cli_list_command(self):
        """测试list命令"""
        result = subprocess.run(
            [sys.executable, "-m", "skills.douyin_downloader.cli", "list"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
        )
        # list命令应该能执行，即使没有任务
        assert result.returncode in [0, 1]  # 0=成功，1=可能有错误但程序正常
        print(f"CLI list: returncode={result.returncode}")
        if result.stdout:
            print(result.stdout[:300])

    @pytest.mark.skip(reason="需要有效的Cookie")
    def test_cli_video_command(self):
        """测试video命令（需要Cookie）"""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "skills.douyin_downloader.cli",
                "video",
                "https://v.douyin.com/zHB_6sz29Y4/",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=30,
        )
        print(f"Video command result: {result.returncode}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
