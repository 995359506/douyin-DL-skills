"""内容工作台的独立路由，不创建下载任务。"""

from __future__ import annotations

from typing import Any, Callable, Optional
from urllib.parse import quote

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse, Response


def create_content_router(get_service: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/content", tags=["内容工作台"])

    def service():
        instance = get_service()
        if instance is None:
            raise HTTPException(503, "内容工作台正在初始化")
        return instance

    async def invoke(method: str, *args: Any, **kwargs: Any) -> dict:
        try:
            result = await getattr(service(), method)(*args, **kwargs)
            return {"success": True, "data": result}
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from None
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("/search")
    async def search(q: str = "", author: str = "", types: str = "", sort: str = "relevance", page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), scope: str = "videos", date_from: str = "", date_to: str = "", collected_from: str = "", collected_to: str = ""):
        return await invoke("search", q, author, types, sort, page, page_size, scope, collected_from or date_from, collected_to or date_to)

    @router.post("/index/rebuild")
    async def rebuild():
        return await invoke("refresh")

    @router.get("/tasks/{task_id:path}")
    async def task_video(task_id: str):
        return await invoke("get_task_video", task_id)

    @router.get("/videos/{aweme_id}/media")
    async def media(aweme_id: str):
        path = await service().media_path(aweme_id)
        if path is None:
            raise HTTPException(404, "本地视频文件不存在，保留的文字仍可阅读")
        return FileResponse(path)

    @router.get("/videos/{aweme_id}")
    async def video(aweme_id: str):
        return await invoke("get_video", aweme_id)

    @router.get("/videos/{aweme_id}/reading")
    async def reading(aweme_id: str):
        return await invoke("get_reading", aweme_id)

    @router.put("/videos/{aweme_id}/reading")
    async def update_reading(aweme_id: str, data: dict = Body(...)):
        return await invoke("set_reading", aweme_id, data)

    @router.get("/sources/{source_id}")
    async def source(source_id: str, revision: Optional[str] = None):
        return await invoke("get_source", source_id, revision)

    @router.get("/materials")
    async def materials(q: str = "", author: str = "", tag: str = "", source_type: str = ""):
        return await invoke("list_materials", q, author, tag, source_type)

    @router.post("/materials")
    async def create_material(data: dict = Body(...)):
        return await invoke("create_material", data)

    @router.get("/materials/{material_id}")
    async def material(material_id: str):
        return await invoke("get_material", material_id)

    @router.patch("/materials/{material_id}")
    async def update_material(material_id: str, data: dict = Body(...)):
        return await invoke("update_material", material_id, data)

    @router.delete("/materials/{material_id}")
    async def delete_material(material_id: str):
        return await invoke("delete_material", material_id)

    @router.get("/topics")
    async def topics():
        return await invoke("list_topics")

    @router.post("/topics")
    async def create_topic(data: dict = Body(...)):
        return await invoke("create_topic", data)

    @router.get("/topics/{topic_id}")
    async def topic(topic_id: str):
        return await invoke("get_topic", topic_id)

    @router.patch("/topics/{topic_id}")
    async def update_topic(topic_id: str, data: dict = Body(...)):
        return await invoke("update_topic", topic_id, data)

    @router.delete("/topics/{topic_id}")
    async def delete_topic(topic_id: str):
        return await invoke("delete_topic", topic_id)

    @router.post("/topics/{topic_id}/members")
    async def add_member(topic_id: str, data: dict = Body(...)):
        return await invoke("add_member", topic_id, data)

    @router.patch("/topics/{topic_id}/members/{member_id}")
    async def update_member(topic_id: str, member_id: str, data: dict = Body(...)):
        return await invoke("update_member", topic_id, member_id, data)

    @router.delete("/topics/{topic_id}/members/{member_id}")
    async def remove_member(topic_id: str, member_id: str):
        return await invoke("remove_member", topic_id, member_id)

    @router.post("/topics/{topic_id}/preview")
    async def preview(topic_id: str, data: dict = Body(...)):
        return await invoke("preview_generation", topic_id, data)

    @router.post("/topics/{topic_id}/generations")
    async def generate(topic_id: str, data: dict = Body(...)):
        return await invoke("generate", topic_id, data)

    @router.patch("/topics/{topic_id}/generations/{version_id}")
    async def edit_version(topic_id: str, version_id: str, data: dict = Body(...)):
        return await invoke("edit_version", topic_id, version_id, data)

    @router.get("/topics/{topic_id}/export")
    async def export_topic(topic_id: str, version_id: Optional[str] = None):
        try:
            filename, content = await service().export_topic(topic_id, version_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from None
        return Response(content, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    return router
