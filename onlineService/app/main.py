"""FastAPI entry: config push, jobs, SSE, public skill, web UI."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated, Any

import yaml
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .auth import AuthDep
from .hub import hub
from .layer_fs import list_layer_children, list_layer_files, list_layers, read_layer_file
from .jobs import store
from .paths import config_file_path, service_root

app = FastAPI(title="Trae Online Service", version="1.0.0")


class JobCreateBody(BaseModel):
    command: str = Field(..., min_length=1)
    parent_job_id: str | None = None


@app.get("/skill.md", include_in_schema=False)
async def skill_markdown() -> FileResponse:
    path = service_root() / "skill.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="skill.md missing")
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


@app.get("/ui/{access_token}", include_in_schema=False)
async def ui_page(access_token: str) -> HTMLResponse:
    expected = os.environ.get("ACCESS_TOKEN", "").strip()
    if not expected or access_token != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing access token")
    raw = (service_root() / "static" / "index.html").read_text(encoding="utf-8")
    token_json = json.dumps(access_token)
    html = raw.replace("__ACCESS_TOKEN_JSON__", token_json)
    return HTMLResponse(html)


@app.post("/api/config")
async def push_config(_: AuthDep, file: UploadFile = File(...)) -> dict[str, str]:
    """使用 multipart 表单字段 `file` 上传 YAML。"""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        yaml.safe_load(data.decode("utf-8"))
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}") from e
    dest = config_file_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {"path": str(dest), "status": "ok"}


@app.post("/api/config/raw")
async def push_config_raw(_: AuthDep, content: str = Query(..., alias="yaml")) -> dict[str, str]:
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}") from e
    dest = config_file_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return {"path": str(dest), "status": "ok"}


@app.get("/api/config")
async def get_config(_: AuthDep) -> dict[str, Any]:
    p = config_file_path()
    if not p.is_file():
        raise HTTPException(status_code=404, detail="No config pushed yet")
    text = p.read_text(encoding="utf-8")
    return {"path": str(p), "yaml": text}


@app.post("/api/jobs")
async def create_job(_: AuthDep, body: JobCreateBody) -> dict[str, Any]:
    try:
        rec = await store.create_job(body.command.strip(), body.parent_job_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return rec.to_dict()


@app.get("/api/jobs")
async def list_jobs(_: AuthDep) -> dict[str, Any]:
    jobs = [j.to_dict() for j in store.list_jobs()]
    return {"jobs": jobs}


@app.get("/api/jobs/{job_id}")
async def get_job(_: AuthDep, job_id: str) -> dict[str, Any]:
    rec = store.get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job not found")
    return rec.to_dict()


@app.get("/api/jobs/{job_id}/parent")
async def get_job_parent(_: AuthDep, job_id: str) -> dict[str, Any]:
    rec = store.get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job not found")
    if not rec.parent_job_id:
        return {"job_id": job_id, "parent": None}
    parent = store.get(rec.parent_job_id)
    if not parent:
        return {
            "job_id": job_id,
            "parent": None,
            "parent_job_id": rec.parent_job_id,
            "note": "parent record missing",
        }
    return {
        "job_id": job_id,
        "parent": parent.to_dict(),
    }


@app.post("/api/jobs/{job_id}/interrupt")
async def interrupt_job(_: AuthDep, job_id: str) -> dict[str, Any]:
    ok = await store.interrupt(job_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Job not running or unknown")
    return {"job_id": job_id, "status": "interrupt_requested"}


@app.post("/api/jobs/reset")
async def reset_jobs(_: AuthDep) -> dict[str, Any]:
    return await store.reset_all()


@app.get("/api/events/stream")
async def events_stream(
    access_token: Annotated[str | None, Query(alias="access_token")] = None,
) -> StreamingResponse:
    """SSE：通过查询参数携带 access_token（浏览器 EventSource 无法自定义 Header）。"""
    expected = os.environ.get("ACCESS_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="ACCESS_TOKEN is not configured")
    if not access_token or access_token != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing access token")

    q = hub.subscribe()

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'connected'})}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/layers")
async def api_list_layers(_: AuthDep) -> dict[str, Any]:
    layers = list_layers()
    # 将 layer_id 映射到 jobs 记录的执行命令（用于 UI 展示）。
    # 如果某个 layer 目录存在但 jobs 状态里已不存在，则不返回 command。
    cmd_by_layer_id: dict[str, str] = {j.layer_id: j.command for j in store.list_jobs()}
    for item in layers:
        lid = item.get("layer_id")
        if lid and lid in cmd_by_layer_id:
            item["command"] = cmd_by_layer_id[lid]
    return {"layers": layers}


@app.get("/api/layers/{layer_id}/files")
async def api_list_layer_files(
    _: AuthDep,
    layer_id: str,
    prefix: str | None = Query(default=None, description="可选：相对路径前缀过滤（只支持前缀，不支持 .. / 绝对路径）"),
    max_files: int = Query(default=2000, ge=1, le=5000),
) -> dict[str, Any]:
    return list_layer_files(layer_id=layer_id, prefix=prefix, max_files=max_files)


@app.get("/api/layers/{layer_id}/files/{file_rel_posix:path}")
async def api_read_layer_file(
    _: AuthDep,
    layer_id: str,
    file_rel_posix: str,
    max_bytes: int | None = Query(default=None, ge=1, le=50_000_000),
    max_text_chars: int | None = Query(default=None, ge=1, le=5_000_000),
) -> dict[str, Any]:
    return read_layer_file(
        layer_id=layer_id,
        file_rel_posix=file_rel_posix,
        max_bytes=max_bytes,
        max_text_chars=max_text_chars,
    )


@app.get("/api/layers/{layer_id}/children")
async def api_list_layer_children(
    _: AuthDep,
    layer_id: str,
    dir: str = Query(default="", description="目录（相对层内路径）；空表示根目录"),
    prefix: str | None = Query(default=None, description="前缀过滤（相对路径，支持只看 src/ 之类）"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    return list_layer_children(
        layer_id=layer_id,
        dir_rel_posix=dir,
        prefix=prefix,
        offset=offset,
        limit=limit,
    )
