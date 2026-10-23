"""只用临时合成资料测量内容搜索，不读取或修改真实下载数据库。"""

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from skills.douyin_downloader.content_service import ContentService
from skills.douyin_downloader.db import TaskDB


async def benchmark(videos: int, repeats: int) -> None:
    with tempfile.TemporaryDirectory(prefix="douyin-content-benchmark-") as directory:
        db = TaskDB(str(Path(directory) / "synthetic.db"))
        await db.connect()
        now = "2026-01-01T00:00:00"
        # 每视频5块：metadata、两种口播、summary概览和要点。
        paragraph = "这是用于离线性能验收的合成中文资料，内容围绕表达结构和沟通方法。" * 8
        await db.conn.executemany("INSERT INTO tasks(task_id,type,status,created_at) VALUES (?,'single','completed',?)", [(f"t{i}", now) for i in range(videos)])
        await db.conn.executemany("INSERT INTO task_videos(task_id,aweme_id,desc,nickname) VALUES (?,?,?,?)", [(f"t{i}", str(i), f"合成标题{i}", f"作者{i%20}") for i in range(videos)])
        await db.conn.executemany("INSERT INTO transcript_jobs(job_id,task_id,status,original_transcript,polished_text,created_at,updated_at) VALUES (?,?,'completed',?,?,?,?)", [(f"tr{i}", f"t{i}", paragraph + ("目标短语" if i%100 == 0 else ""), paragraph, now, now) for i in range(videos)])
        await db.conn.executemany("INSERT INTO video_summary_jobs(job_id,task_id,status,summary_json,created_at,updated_at) VALUES (?,?,'completed',?,?,?)", [(f"s{i}", f"t{i}", json.dumps({"overview": paragraph, "key_points": ["合成要点和素材方法"]}, ensure_ascii=False), now, now) for i in range(videos)])
        await db.commit()
        service = ContentService(db)
        await service.initialize()
        started = time.perf_counter()
        cold = await service.search("目标短语 方法")
        cold_seconds = time.perf_counter()-started
        warm = []
        for _ in range(repeats):
            started = time.perf_counter()
            result = await service.search("目标短语 方法")
            assert result["total"] == cold["total"]
            warm.append(time.perf_counter()-started)
        blocks = (await db.fetch_one("SELECT COUNT(*) FROM content_sources WHERE active=1"))[0]
        print(json.dumps({"videos": videos, "blocks": blocks, "characters_per_transcript": len(paragraph), "matches": cold["total"], "cold_seconds": round(cold_seconds,3), "warm_seconds": [round(value,3) for value in warm], "warm_median_seconds": round(statistics.median(warm),3), "scope": "完整refresh+中文AND查询+聚合+分页，不含浏览器渲染；临时SQLite合成数据"}, ensure_ascii=False))
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--videos", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=3)
    options = parser.parse_args()
    asyncio.run(benchmark(options.videos, options.repeats))
