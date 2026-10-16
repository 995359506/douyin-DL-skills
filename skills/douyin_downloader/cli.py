"""
CLI入口 - douyin-dl命令

供AI Agent使用的命令行接口
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

# 确保可以导入技能模块
sys.path.insert(0, str(Path(__file__).parent.parent))

# 添加 dy_scripts 到路径以便导入 Cookie 相关函数
_cookie_project_root = Path(__file__).parent.parent.parent
_cookie_dy_scripts_path = _cookie_project_root / "dy_scripts"
if str(_cookie_dy_scripts_path) not in sys.path:
    sys.path.insert(0, str(_cookie_dy_scripts_path))

try:
    from .logger import setup_logging
    from .task_manager_v2 import TaskManager
except ImportError:
    from logger import setup_logging
    from task_manager_v2 import TaskManager

# 导入 Cookie 验证函数
try:
    from .cookie_manager import is_cookie_logged_in
except ImportError:
    from cookie_manager import is_cookie_logged_in


def print_json(data: dict):
    """打印JSON格式输出"""
    print(json.dumps(data, ensure_ascii=False, indent=2))


def print_table(headers: list, rows: list):
    """打印表格格式"""
    # 计算列宽
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))

    # 打印表头
    header_line = " | ".join(h.ljust(w) for h, w in zip(headers, col_widths))
    print(header_line)
    print("-" * len(header_line))

    # 打印行
    for row in rows:
        print(" | ".join(str(cell).ljust(w) for cell, w in zip(row, col_widths)))


class DouyinDLCLI:
    """douyin-dl CLI实现"""

    def __init__(self, db_path: str = "./douyin_downloader.db"):
        setup_logging()
        self.manager = TaskManager(db_path=db_path)

    async def init(self):
        """初始化"""
        await self.manager.initialize()

    async def close(self):
        """关闭"""
        await self.manager.shutdown()

    @staticmethod
    def _cleanup_skill(skill) -> None:
        """关闭 DouyinDownloadSkill 的线程池资源，避免进程挂起不退出"""
        if skill is None:
            return
        try:
            executor = getattr(skill, "_playwright_executor", None)
            if executor is not None:
                executor.shutdown(wait=False)
        except Exception:
            pass

    async def cmd_mix(
        self,
        mix_id: str,
        name: Optional[str] = None,
        max_counts: Optional[int] = None,
        wait: bool = False,
        output_format: str = "json",
    ):
        """
        下载合集（直接调用下载核心，真实下载，命令在下载完成后返回）

        Args:
            mix_id: 合集ID
            name: 合集名称
            max_counts: 最大下载数量
            wait: 是否显示实时进度（下载始终执行）
            output_format: 输出格式 (json/table)
        """
        original_skill = None
        try:
            # 导入原版Skill
            from .core import DouyinDownloadSkill

            original_skill = DouyinDownloadSkill(self.manager.config_path)

            print(f"开始下载合集: {mix_id}")

            # 调用原版的合集下载方法（真实下载，下载完成后返回）
            result = await original_skill.download_mix_by_id(
                mix_id=mix_id,
                mix_name=name or "",
                max_counts=max_counts,
                progress_callback=(
                    lambda c, t, m: print(f"\r进度: {c}/{t} - {m}", end="", flush=True)
                )
                if wait
                else None,
            )

            if wait:
                print()  # 换行

            # 输出结果
            if output_format == "json":
                print_json(
                    {
                        "success": result.get("success", False),
                        "mix_id": mix_id,
                        "mix_name": name,
                        "downloaded": result.get("downloaded", 0),
                        "skipped": result.get("skipped", 0),
                        "message": result.get("message", ""),
                        "download_path": result.get("download_path"),
                        "has_more": result.get("has_more", False),
                    }
                )
            else:
                status = "[OK] 成功" if result.get("success") else "[FAIL] 失败"
                print(f"{status}: {result.get('message', '')}")
                print(f"  下载: {result.get('downloaded', 0)}")
                print(f"  跳过: {result.get('skipped', 0)}")
                if result.get("download_path"):
                    print(f"  路径: {result['download_path']}")

        except Exception as e:
            if output_format == "json":
                print_json({"error": str(e)})
            else:
                print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            self._cleanup_skill(original_skill)

    async def cmd_post(
        self,
        user_url: str,
        max_counts: Optional[int] = None,
        interval: str = "all",
        wait: bool = False,
        output_format: str = "json",
    ):
        """
        下载用户主页作品

        Args:
            user_url: 用户主页链接
            max_counts: 最大下载数量
            interval: 日期区间，格式如 "2024-01-01|2024-12-31" 或 "all"
            wait: 是否等待直到完成
            output_format: 输出格式 (json/table)
        """
        original_skill = None
        try:
            # 导入原版Skill
            from .core import DouyinDownloadSkill

            original_skill = DouyinDownloadSkill(self.manager.config_path)

            print(f"开始下载用户作品: {user_url}")

            # 调用原版的用户作品下载方法
            result = await original_skill.download_user_posts(
                user_url=user_url,
                progress_callback=lambda c, t, m: print(
                    f"\r进度: {c}/{t} - {m}", end="", flush=True
                )
                if wait
                else None,
                max_counts=max_counts,
                interval=interval,
            )

            if wait:
                print()  # 换行

            # 输出结果
            if output_format == "json":
                print_json(
                    {
                        "success": result.get("success", False),
                        "downloaded": result.get("downloaded", 0),
                        "skipped": result.get("skipped", 0),
                        "message": result.get("message"),
                        "download_path": result.get("download_path"),
                        "has_more": result.get("has_more", False),
                    }
                )
            else:
                status = "[OK] 成功" if result.get("success") else "[FAIL] 失败"
                print(f"{status}: {result.get('message', '')}")
                print(f"  下载: {result.get('downloaded', 0)}")
                print(f"  跳过: {result.get('skipped', 0)}")

        except Exception as e:
            if output_format == "json":
                print_json({"error": str(e)})
            else:
                print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            self._cleanup_skill(original_skill)

    async def cmd_status(self, task_id: str, output_format: str = "json"):
        """查看任务状态"""
        task = await self.manager.get_task(task_id)

        if not task:
            if output_format == "json":
                print_json({"error": "任务不存在"})
            else:
                print("错误: 任务不存在", file=sys.stderr)
            sys.exit(1)

        if output_format == "json":
            print_json(
                {
                    "task_id": task.id,
                    "type": task.type,
                    "mix_id": task.mix_id,
                    "mix_name": task.mix_name,
                    "status": task.status,
                    "progress": task.progress,
                    "result": task.result,
                    "created_at": task.created_at,
                    "completed_at": task.completed_at,
                }
            )
        else:
            print(f"任务: {task.id}")
            print(f"类型: {task.type}")
            print(f"合集: {task.mix_name or 'N/A'} ({task.mix_id or 'N/A'})")
            print(f"状态: {task.status}")
            print(
                f"进度: {task.progress.get('percentage', 0)}% ({task.progress.get('current', 0)}/{task.progress.get('total', 0)})"
            )
            print(
                f"结果: 成功{task.result.get('downloaded', 0)} 跳过{task.result.get('skipped', 0)} 失败{task.result.get('failed', 0)}"
            )

    async def cmd_videos(
        self, task_id: str, status: Optional[str] = None, output_format: str = "json"
    ):
        """获取任务的所有视频"""
        videos = await self.manager.get_task_videos(task_id)

        if status:
            videos = [v for v in videos if v.get("status") == status]

        if output_format == "json":
            print_json({"task_id": task_id, "total": len(videos), "videos": videos})
        else:
            print(f"任务 {task_id} 的视频列表:")
            print()

            rows = []
            for v in videos:
                status_icon = {
                    "downloaded": "[OK]",
                    "skipped": "[SKIP]",
                    "failed": "[FAIL]",
                    "pending": "[PENDING]",
                }.get(v.get("status", ""), "[?]")

                rows.append(
                    [
                        status_icon,
                        v.get("aweme_id", "")[:15],
                        v.get("desc", "")[:40],
                        v.get("status", ""),
                    ]
                )

            print_table(["状态", "ID", "标题", "下载状态"], rows)

    async def cmd_export(self, task_id: str, output: Optional[str] = None):
        """导出任务元数据"""
        metadata = await self.manager.export_task_metadata(task_id)

        if not metadata:
            print_json({"error": "任务不存在或无法导出"})
            sys.exit(1)

        if output:
            # 写入文件
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)

            print(f"已导出到: {output}")
        else:
            # 输出到stdout
            print_json(metadata)

    async def cmd_video(
        self, url: str, wait: bool = False, output_format: str = "json"
    ):
        """
        下载单个视频

        Args:
            url: 视频链接（支持 https://v.douyin.com/xxxxx 或标准URL）
        """
        original_skill = None
        try:
            # 导入原版Skill
            from .core import DouyinDownloadSkill

            original_skill = DouyinDownloadSkill(self.manager.config_path)

            print(f"开始下载视频: {url}")

            # 调用原版的单视频下载方法
            result = await original_skill.download_single_video(
                video_url=url,
                progress_callback=lambda c, t, m: print(
                    f"\r进度: {c}% - {m}", end="", flush=True
                )
                if wait
                else None,
            )

            if wait:
                print()  # 换行

            # 输出结果
            if output_format == "json":
                print_json(
                    {
                        "success": result.get("success", False),
                        "aweme_id": result.get("aweme_id"),
                        "video_name": result.get("video_name"),
                        "file_name": result.get("file_name"),
                        "message": result.get("message"),
                        "download_path": result.get("download_path"),
                    }
                )
            else:
                status = "[OK] 成功" if result.get("success") else "[FAIL] 失败"
                print(f"{status}: {result.get('video_name', '未知')}")
                print(f"  文件: {result.get('file_name', 'N/A')}")
                print(f"  路径: {result.get('download_path', 'N/A')}")

        except Exception as e:
            if output_format == "json":
                print_json({"error": str(e)})
            else:
                print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            self._cleanup_skill(original_skill)

    async def cmd_retry(self, task_id: str, wait: bool = False):
        """
        重试任务（按任务类型重新下载，已存在的文件自动跳过）

        Args:
            task_id: 任务ID
            wait: 是否显示实时进度
        """
        original_skill = None
        try:
            task = await self.manager.get_task(task_id)
            if not task:
                print_json({"success": False, "error": f"任务不存在: {task_id}"})
                sys.exit(1)

            # 导入原版Skill
            from .core import DouyinDownloadSkill

            original_skill = DouyinDownloadSkill(self.manager.config_path)

            progress_cb = (
                lambda c, t, m: print(f"\r进度: {c}/{t} - {m}", end="", flush=True)
                if wait
                else None
            )
            options = task.download_options or {}

            if task.type == "mix":
                if not task.mix_id:
                    print_json({"success": False, "error": "任务缺少合集ID，无法重试"})
                    sys.exit(1)
                result = await original_skill.download_mix_by_id(
                    mix_id=task.mix_id,
                    mix_name=task.mix_name or "",
                    max_counts=options.get("max_counts"),
                    interval=options.get("interval", "all"),
                    progress_callback=progress_cb,
                )
                out = {
                    "success": result.get("success", False),
                    "task_id": task_id,
                    "type": "mix",
                    "downloaded": result.get("downloaded", 0),
                    "skipped": result.get("skipped", 0),
                    "message": result.get("message", ""),
                }
            elif task.type == "single":
                # 单视频：从任务视频记录取 aweme_id 重新下载
                videos = await self.manager.db.get_task_videos(task_id)
                if not videos:
                    print_json(
                        {"success": False, "error": "任务没有关联的视频记录，无法重试"}
                    )
                    sys.exit(1)
                aweme_id = videos[0].aweme_id
                url = f"https://www.douyin.com/video/{aweme_id}"
                result = await original_skill.download_single_video(video_url=url)
                out = {
                    "success": result.get("success", False),
                    "task_id": task_id,
                    "type": "single",
                    "aweme_id": aweme_id,
                    "message": result.get("message", ""),
                }
            else:
                print_json(
                    {
                        "success": False,
                        "error": f"暂不支持重试 {task.type} 类型任务，请重新发起下载",
                    }
                )
                sys.exit(1)

            if wait:
                print()  # 换行

            print_json(out)

        except Exception as e:
            print_json({"success": False, "error": str(e)})
            sys.exit(1)
        finally:
            self._cleanup_skill(original_skill)

    async def cmd_refresh_cookie(
        self, headless: bool = False, output_format: str = "json"
    ):
        """
        刷新Cookie（通过浏览器扫码登录）

        Args:
            headless: 是否使用无头模式（不显示浏览器窗口）
        """
        import asyncio

        original_skill = None
        cookie = None

        try:
            print("正在启动浏览器获取最新Cookie...")
            print("提示: 请在120秒内完成扫码登录")
            print("      如需取消，请直接关闭浏览器窗口或按 Ctrl+C")
            print()

            # 调用原版的 refresh_cookie 方法
            from .core import DouyinDownloadSkill

            original_skill = DouyinDownloadSkill(self.manager.config_path)

            # 强制刷新Cookie（添加超时保护）
            try:
                cookie = await asyncio.wait_for(
                    original_skill.refresh_cookie(headless=headless),
                    timeout=125,  # 略长于内部超时（120秒）
                )
            except asyncio.TimeoutError:
                # 处理超时
                if output_format == "json":
                    print_json(
                        {
                            "success": False,
                            "error": "获取Cookie超时",
                            "message": "120秒内未完成登录，操作已取消。如需继续，请重新运行命令。",
                        }
                    )
                else:
                    print("[FAIL] 获取Cookie超时")
                    print("  原因: 120秒内未完成登录")
                    print("  提示: 如需继续，请重新运行命令")
                sys.exit(1)
            except asyncio.CancelledError:
                # 用户取消
                raise  # 重新抛出，让上层处理

            # 验证Cookie（使用已导入的函数）
            is_valid = is_cookie_logged_in(cookie)

            # 输出结果
            if output_format == "json":
                print_json(
                    {
                        "success": is_valid,
                        "message": "Cookie刷新成功" if is_valid else "Cookie可能无效",
                        "cookie_length": len(cookie) if cookie else 0,
                        "cookie_preview": (cookie[:100] + "...")
                        if cookie and len(cookie) > 100
                        else (cookie or ""),
                    }
                )
            else:
                if is_valid:
                    print("[OK] Cookie刷新成功")
                    print(f"  Cookie长度: {len(cookie)}")
                    print(f"  预览: {cookie[:100]}...")
                else:
                    print("⚠ Cookie获取完成，但验证未通过")
                    print(f"  Cookie长度: {len(cookie) if cookie else 0}")

        except RuntimeError as e:
            # 处理特定运行时错误（如用户取消）
            error_msg = str(e)
            if "超时" in error_msg or "取消" in error_msg or "中断" in error_msg:
                if output_format == "json":
                    print_json(
                        {
                            "success": False,
                            "error": "操作已取消",
                            "message": error_msg,
                        }
                    )
                else:
                    print("[CANCEL] 操作已取消")
                    print(f"  原因: {error_msg}")
            else:
                raise  # 重新抛出其他运行时错误

        finally:
            # 确保关闭 Playwright 浏览器
            if original_skill:
                try:
                    # 关闭 executor 中的浏览器资源
                    if hasattr(original_skill, "_playwright_executor"):
                        original_skill._playwright_executor.shutdown(wait=False)
                        print("[DY-SKILL] 浏览器资源已清理")
                except Exception:
                    pass  # 清理时出错可以忽略

    async def cmd_list(
        self, status: Optional[str] = None, limit: int = 10, output_format: str = "json"
    ):
        """列出所有任务"""
        result = await self.manager.get_tasks(status=status, limit=limit)
        tasks = result.get("tasks", [])
        total = result.get("total", 0)

        if output_format == "json":
            print_json(
                {
                    "total": total,
                    "tasks": [
                        {
                            "task_id": t.id,
                            "type": t.type,
                            "mix_name": t.mix_name,
                            "status": t.status,
                            "progress": t.progress,
                            "created_at": t.created_at,
                        }
                        for t in tasks
                    ],
                }
            )
        else:
            print(f"任务列表 (共 {total} 个):")
            print()

            rows = []
            for t in tasks:
                progress = f"{t.progress.get('percentage', 0)}%"
                rows.append(
                    [
                        t.id[:8],
                        t.type,
                        t.mix_name[:30] if t.mix_name else "N/A",
                        t.status,
                        progress,
                    ]
                )

            print_table(["ID", "类型", "合集名称", "状态", "进度"], rows)


def main():
    """CLI入口"""
    parser = argparse.ArgumentParser(prog="douyin-dl", description="抖音视频下载器 CLI")

    parser.add_argument(
        "--db-path",
        default="./douyin_downloader.db",
        help="数据库路径 (默认: ./douyin_downloader.db)",
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # mix 命令
    mix_parser = subparsers.add_parser("mix", help="下载合集")
    mix_parser.add_argument("mix_id", help="合集ID")
    mix_parser.add_argument("--name", help="合集名称")
    mix_parser.add_argument("--max", type=int, dest="max_counts", help="最大下载数量")
    mix_parser.add_argument("--wait", action="store_true", help="等待直到完成")
    mix_parser.add_argument(
        "--format",
        dest="output_format",
        choices=["json", "table"],
        default="json",
        help="输出格式",
    )

    # video 命令（下载单个视频）
    video_parser = subparsers.add_parser("video", help="下载单个视频")
    video_parser.add_argument("url", help="视频链接（支持分享链接或标准URL）")
    video_parser.add_argument("--wait", action="store_true", help="等待直到完成")
    video_parser.add_argument(
        "--format",
        dest="output_format",
        choices=["json", "table"],
        default="json",
        help="输出格式",
    )

    # post 命令（下载用户主页作品）
    post_parser = subparsers.add_parser("post", help="下载用户主页作品")
    post_parser.add_argument("url", help="用户主页链接")
    post_parser.add_argument("--max", type=int, dest="max_counts", help="最大下载数量")
    post_parser.add_argument(
        "--interval",
        default="all",
        help="日期区间，格式如 '2024-01-01|2024-12-31' 或 'all'",
    )
    post_parser.add_argument("--wait", action="store_true", help="等待直到完成")
    post_parser.add_argument(
        "--format",
        dest="output_format",
        choices=["json", "table"],
        default="json",
        help="输出格式",
    )

    # status 命令
    status_parser = subparsers.add_parser("status", help="查看任务状态")
    status_parser.add_argument("task_id", help="任务ID")
    status_parser.add_argument(
        "--format",
        dest="output_format",
        choices=["json", "table"],
        default="json",
        help="输出格式",
    )

    # videos 命令
    videos_parser = subparsers.add_parser("videos", help="获取任务的所有视频")
    videos_parser.add_argument("task_id", help="任务ID")
    videos_parser.add_argument(
        "--status",
        choices=["pending", "downloaded", "skipped", "failed"],
        help="筛选状态",
    )
    videos_parser.add_argument(
        "--format",
        dest="output_format",
        choices=["json", "table"],
        default="json",
        help="输出格式",
    )

    # export 命令
    export_parser = subparsers.add_parser("export", help="导出任务元数据")
    export_parser.add_argument("task_id", help="任务ID")
    export_parser.add_argument("--output", help="输出文件路径 (默认: stdout)")

    # refresh-cookie 命令
    refresh_cookie_parser = subparsers.add_parser(
        "refresh-cookie", help="刷新Cookie（浏览器扫码登录）"
    )
    refresh_cookie_parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="使用无头模式（不显示浏览器窗口）",
    )
    refresh_cookie_parser.add_argument(
        "--format",
        dest="output_format",
        choices=["json", "table"],
        default="json",
        help="输出格式",
    )

    # retry 命令
    retry_parser = subparsers.add_parser("retry", help="重试失败的任务")
    retry_parser.add_argument("task_id", help="任务ID")
    retry_parser.add_argument("--wait", action="store_true", help="等待直到完成")

    # list 命令
    list_parser = subparsers.add_parser("list", help="列出所有任务")
    list_parser.add_argument(
        "--status",
        choices=["pending", "downloading", "completed", "failed"],
        help="筛选状态",
    )
    list_parser.add_argument(
        "--limit", type=int, default=10, help="返回数量限制 (默认: 10)"
    )
    list_parser.add_argument(
        "--format",
        dest="output_format",
        choices=["json", "table"],
        default="json",
        help="输出格式",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # 运行命令
    cli = DouyinDLCLI(db_path=args.db_path)

    async def run():
        await cli.init()

        try:
            if args.command == "mix":
                await cli.cmd_mix(
                    args.mix_id,
                    name=args.name,
                    max_counts=args.max_counts,
                    wait=args.wait,
                    output_format=args.output_format,
                )
            elif args.command == "status":
                await cli.cmd_status(args.task_id, output_format=args.output_format)
            elif args.command == "videos":
                await cli.cmd_videos(
                    args.task_id, status=args.status, output_format=args.output_format
                )
            elif args.command == "export":
                await cli.cmd_export(args.task_id, output=args.output)
            elif args.command == "video":
                await cli.cmd_video(
                    args.url, wait=args.wait, output_format=args.output_format
                )
            elif args.command == "refresh-cookie":
                await cli.cmd_refresh_cookie(
                    headless=args.headless,
                    output_format=args.output_format,
                )
            elif args.command == "retry":
                await cli.cmd_retry(args.task_id, wait=args.wait)
            elif args.command == "list":
                await cli.cmd_list(
                    status=args.status,
                    limit=args.limit,
                    output_format=args.output_format,
                )

            elif args.command == "post":
                await cli.cmd_post(
                    args.url,
                    max_counts=args.max_counts,
                    interval=args.interval,
                    wait=args.wait,
                    output_format=args.output_format,
                )
        except asyncio.CancelledError:
            # 用户取消操作（Ctrl+C）
            print("\n\n操作已取消")
            sys.exit(130)  # 130 是 Ctrl+C 的标准退出码
        except KeyboardInterrupt:
            # 键盘中断
            print("\n\n操作被用户中断")
            sys.exit(130)
        finally:
            await cli.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
