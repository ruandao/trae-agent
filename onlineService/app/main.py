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
from .git_clone import clone_into_new_layer
from .hub import hub
from .layer_fs import (
    any_layer_has_git_repo,
    list_layer_children,
    list_layer_files,
    list_layers,
    read_layer_file,
)
from .layer_git import list_branches as list_layer_git_branches
from .job_trajectory import load_agent_steps_for_layer
from .jobs import store
from .paths import config_file_path, service_root

app = FastAPI(title="Trae Online Service", version="1.0.0")


class JobCreateBody(BaseModel):
    command: str = Field(..., min_length=1)
    parent_job_id: str | None = None
    repo_layer_id: str | None = Field(
        default=None,
        max_length=128,
        description="无父任务时从该层复制工作区（含 .git），用于在已克隆仓库上开任务。",
    )
    git_branch: str | None = Field(
        default=None,
        max_length=256,
        description="任务开始前在工作区内执行 git checkout。",
    )


class CloneRepoBody(BaseModel):
    """将远程仓库克隆到新的可写层根目录（空目录内 ``git clone … .``）。"""

    url: str = Field(..., min_length=1, max_length=4096)
    branch: str | None = None
    depth: int | None = Field(default=None, ge=1, le=10_000)


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


@app.post("/api/repos/clone")
async def clone_repo(_: AuthDep, body: CloneRepoBody) -> dict[str, Any]:
    """在新建可写层目录中执行 ``git clone``（需系统已安装 git）。"""
    async def _publish(ev: dict[str, Any]) -> None:
        await hub.publish(ev)

    try:
        layer_id, lp, out, code = await clone_into_new_layer(
            body.url,
            branch=body.branch,
            depth=body.depth,
            publish=_publish,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if code != 0:
        await hub.publish(
            {
                "type": "repo_clone_finished",
                "layer_id": layer_id,
                "layer_path": str(lp),
                "status": "error",
                "exit_code": code,
            }
        )
        raise HTTPException(
            status_code=400,
            detail={
                "message": "git clone failed",
                "exit_code": code,
                "output": out,
            },
        )
    await hub.publish(
        {
            "type": "repo_clone_finished",
            "layer_id": layer_id,
            "layer_path": str(lp),
            "status": "ok",
            "exit_code": code,
        }
    )
    await hub.publish(
        {
            "type": "repo_cloned",
            "layer_id": layer_id,
            "layer_path": str(lp),
        }
    )
    return {
        "status": "ok",
        "layer_id": layer_id,
        "layer_path": str(lp),
        "output": out,
    }


@app.post("/api/jobs")
async def create_job(_: AuthDep, body: JobCreateBody) -> dict[str, Any]:
    try:
        rec = await store.create_job(
            body.command.strip(),
            body.parent_job_id,
            repo_layer_id=body.repo_layer_id.strip() if body.repo_layer_id else None,
            git_branch=body.git_branch.strip() if body.git_branch else None,
        )
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


@app.get("/api/jobs/{job_id}/steps")
async def job_agent_steps(_: AuthDep, job_id: str) -> dict[str, Any]:
    """从任务可写层 `.trajectories/trajectory_*.json` 读取最新轨迹中的 ``agent_steps``。"""
    rec = store.get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        payload = load_agent_steps_for_layer(rec.layer_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"job_id": job_id, "layer_id": rec.layer_id, **payload}


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


@app.post("/api/jobs/{job_id}/redo")
async def redo_job(_: AuthDep, job_id: str) -> dict[str, Any]:
    try:
        rec = await store.redo_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return rec.to_dict()


@app.post("/api/jobs/{job_id}/continue")
async def continue_job(_: AuthDep, job_id: str) -> dict[str, Any]:
    try:
        rec = await store.continue_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return rec.to_dict()


@app.delete("/api/jobs/{job_id}")
async def delete_job(_: AuthDep, job_id: str) -> dict[str, Any]:
    try:
        return await store.delete_job(job_id)
    except ValueError as e:
        msg = str(e)
        if msg == "Job not found":
            raise HTTPException(status_code=404, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e


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


@app.get("/api/layers/{layer_id}/git/branches")
async def api_layer_git_branches(_: AuthDep, layer_id: str) -> dict[str, Any]:
    """列出该可写层内 git 仓库的分支（需存在 ``.git``）。"""
    return await list_layer_git_branches(layer_id)


@app.get("/api/requirements/task-gate")
async def api_task_gate(_: AuthDep) -> dict[str, Any]:
    """新建任务前是否已满足「至少成功克隆过一次」（存在含 ``.git`` 的可写层）。"""
    return {"clone_done": any_layer_has_git_repo()}


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
