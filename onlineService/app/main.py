"""FastAPI entry: config push, jobs, SSE, public skill, web UI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import yaml
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .auth import AuthDep
from .git_clone import (
    clear_clone_layer_log,
    clone_into_new_layer,
    get_clone_layer_log_text,
)
from .hub import hub
from .layer_fs import (
    any_layer_has_git_repo,
    infer_layer_parent_from_workspace,
    list_layer_children,
    list_layer_files,
    list_layers,
    read_layer_file,
)
from .layer_git import (
    commit_layer_worktree,
    diff_layer_worktree_vs_parent,
    git_ahead_of_upstream,
    git_worktree_dirty,
    list_branches as list_layer_git_branches,
    push_layer_worktree,
)
from .job_trajectory import load_agent_steps_for_layer
from .jobs import JobRecord, job_layer_git_destructive_locked, store
from .paths import config_file_path, service_root
from . import task_api_bootstrap

log = logging.getLogger(__name__)


def _strict_bootstrap_enabled() -> bool:
    """Whether Task API bootstrap failures should block service startup."""
    raw = (os.environ.get("TASK_API_BOOTSTRAP_STRICT_STARTUP") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    try:
        await asyncio.to_thread(task_api_bootstrap.bootstrap_container_config)
    except Exception:
        if _strict_bootstrap_enabled():
            raise
        log.exception(
            "startup bootstrap failed; continue without refreshed token/config "
            "(set TASK_API_BOOTSTRAP_STRICT_STARTUP=1 to fail-fast)"
        )
    yield


app = FastAPI(title="Trae Online Service", version="1.0.0", lifespan=_lifespan)


def _job_to_api_dict(rec: JobRecord) -> dict[str, Any]:
    d = rec.to_dict()
    d["git_destructive_locked"] = job_layer_git_destructive_locked(rec)
    return d


class JobCreateBody(BaseModel):
    command: str = Field(..., min_length=1)
    command_kind: Literal["trae", "shell"] = Field(
        default="trae",
        description="trae：经 trae-cli run；shell：在工作区内 bash -lc 执行原文。",
    )
    parent_job_id: str | None = None
    repo_layer_id: str | None = Field(
        default=None,
        max_length=128,
        description="无父任务时从该层复制工作区；.git 以符号链接共享，不逐层复制。",
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


class LayerGitCommitBody(BaseModel):
    """在工作区内执行 ``git add -A`` 与 ``git commit``。

    ``message`` 为空时由服务端自动生成说明：约定式主题行（type/scope）、
    若存在 ``.trajectories/trajectory_*.json`` 则附带代理结论与步骤节选，以及变更统计与文件列表。
    """

    message: str | None = Field(default=None, max_length=4096)


def _command_for_layer_id(layer_id: str) -> str | None:
    for j in store.list_jobs():
        if j.layer_id == layer_id:
            return j.command
    return None


def _valid_layer_id_param(layer_id: str) -> str:
    if (
        ".." in layer_id
        or "/" in layer_id
        or "\\" in layer_id
        or not layer_id.strip()
        or len(layer_id) > 256
    ):
        raise HTTPException(status_code=400, detail="invalid layer_id")
    return layer_id


async def _schedule_clear_clone_log(layer_id: str, delay: float = 4.0) -> None:
    async def _run() -> None:
        await asyncio.sleep(delay)
        await clear_clone_layer_log(layer_id)

    asyncio.create_task(_run())


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
                "title": f"克隆失败 (exit {code})",
                "status": "error",
            }
        )
        await _schedule_clear_clone_log(layer_id)
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
            "title": "克隆成功",
            "status": "ok",
        }
    )
    await hub.publish(
        {
            "type": "repo_cloned",
            "layer_id": layer_id,
            "title": "仓库已就绪",
        }
    )
    await _schedule_clear_clone_log(layer_id)
    return {
        "status": "ok",
        "layer_id": layer_id,
        "layer_path": str(lp),
        "output": out,
    }


@app.get("/api/repos/clone-log/{layer_id}")
async def api_clone_log(_: AuthDep, layer_id: str) -> dict[str, Any]:
    """轮询克隆进度：正文在服务端缓冲，SSE 仅通知 layer_id。"""
    lid = _valid_layer_id_param(layer_id)
    text = await get_clone_layer_log_text(lid)
    return {"layer_id": lid, "text": text}


@app.get("/api/repos/bootstrap-clone-log")
async def api_bootstrap_clone_log(_: AuthDep) -> dict[str, Any]:
    """容器启动引导阶段完成的克隆日志（无 SSE 时供首屏拉取）。"""
    lid = task_api_bootstrap.bootstrap_clone_layer_id
    if not lid:
        return {"layer_id": None, "text": ""}
    text = await get_clone_layer_log_text(lid)
    return {"layer_id": lid, "text": text}


@app.post("/api/jobs")
async def create_job(_: AuthDep, body: JobCreateBody) -> dict[str, Any]:
    try:
        rec = await store.create_job(
            body.command.strip(),
            body.parent_job_id,
            repo_layer_id=body.repo_layer_id.strip() if body.repo_layer_id else None,
            git_branch=body.git_branch.strip() if body.git_branch else None,
            command_kind=body.command_kind,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _job_to_api_dict(rec)


@app.get("/api/jobs")
async def list_jobs(_: AuthDep) -> dict[str, Any]:
    jobs = [_job_to_api_dict(j) for j in store.list_jobs()]
    return {"jobs": jobs}


@app.get("/api/jobs/{job_id}")
async def get_job(_: AuthDep, job_id: str) -> dict[str, Any]:
    rec = store.get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_api_dict(rec)


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
    try:
        ok = await store.interrupt(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=400, detail="Job not running or unknown")
    return {"job_id": job_id, "status": "interrupt_requested"}


@app.post("/api/jobs/{job_id}/redo")
async def redo_job(_: AuthDep, job_id: str) -> dict[str, Any]:
    try:
        rec = await store.redo_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _job_to_api_dict(rec)


@app.post("/api/jobs/{job_id}/continue")
async def continue_job(_: AuthDep, job_id: str) -> dict[str, Any]:
    try:
        rec = await store.continue_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _job_to_api_dict(rec)


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


@app.post("/api/layers/{layer_id}/git/commit")
async def api_layer_git_commit(
    _: AuthDep,
    layer_id: str,
    body: LayerGitCommitBody,
) -> dict[str, Any]:
    """将当前可写层工作区全部暂存并提交到本地仓库（不 push）。"""
    return await commit_layer_worktree(
        layer_id,
        body.message,
        command_hint=_command_for_layer_id(layer_id),
    )


@app.post("/api/layers/{layer_id}/git/push")
async def api_layer_git_push(_: AuthDep, layer_id: str) -> dict[str, Any]:
    """将当前分支推送到已配置的上游远程。"""
    return await push_layer_worktree(layer_id)


@app.get("/api/layers/{layer_id}/diff/parent")
async def api_layer_diff_parent(_: AuthDep, layer_id: str) -> dict[str, Any]:
    """当前可写层工作区目录与已解析父层目录的差异（``diff -ruN -x .git``）。"""
    layers = list_layers()
    known_ids = {str(x.get("layer_id")) for x in layers if x.get("layer_id")}
    jobs = store.list_jobs()
    parent = _resolved_parent_layer_id(layer_id, known_ids, jobs)
    if not parent:
        return {
            "layer_id": layer_id,
            "parent_layer_id": None,
            "same": None,
            "diff": "",
            "truncated": False,
            "detail": "无父层可对比（根层或父层目录已不存在）",
        }
    return await asyncio.to_thread(diff_layer_worktree_vs_parent, parent, layer_id)


@app.get("/api/requirements/task-gate")
async def api_task_gate(_: AuthDep) -> dict[str, Any]:
    """新建任务前是否已满足「至少成功克隆过一次」（存在含 ``.git`` 的可写层）。"""
    return {"clone_done": any_layer_has_git_repo()}


def _layer_parent_from_jobs(layer_id: str, jobs: list[Any]) -> str | None:
    """无 ``.git`` 符号链接时（例如旧版逐层复制），用任务记录推断父层。"""
    job_by_layer: dict[str, Any] = {}
    for j in jobs:
        if j.layer_id not in job_by_layer:
            job_by_layer[j.layer_id] = j
    by_id = {j.id: j for j in jobs}
    j = job_by_layer.get(layer_id)
    if not j:
        return None
    if j.parent_job_id:
        p = by_id.get(j.parent_job_id)
        return str(p.layer_id) if p else None
    if j.repo_layer_id:
        return str(j.repo_layer_id)
    return None


def _resolved_parent_layer_id(layer_id: str, known_ids: set[str], jobs: list[Any]) -> str | None:
    p = infer_layer_parent_from_workspace(str(layer_id)) or _layer_parent_from_jobs(str(layer_id), jobs)
    if p and p in known_ids:
        return p
    return None


async def _layer_git_meta(lid: str) -> tuple[bool | None, dict[str, Any]]:
    """在线程中跑 git 子进程，避免阻塞事件循环。"""
    return await asyncio.gather(
        asyncio.to_thread(git_worktree_dirty, lid),
        asyncio.to_thread(git_ahead_of_upstream, lid),
    )


@app.get("/api/layers")
async def api_list_layers(_: AuthDep) -> dict[str, Any]:
    layers, jobs = await asyncio.gather(
        asyncio.to_thread(list_layers),
        asyncio.to_thread(store.list_jobs),
    )
    # 将 layer_id 映射到 jobs 记录的执行命令（用于 UI 展示）。
    # 如果某个 layer 目录存在但 jobs 状态里已不存在，则不返回 command。
    cmd_by_layer_id: dict[str, str] = {j.layer_id: j.command for j in jobs}
    known_ids = {str(x.get("layer_id")) for x in layers if x.get("layer_id")}
    with_git: list[tuple[dict[str, Any], str]] = []
    for item in layers:
        lid = item.get("layer_id")
        if not lid:
            continue
        lid_s = str(lid)
        if lid_s in cmd_by_layer_id:
            item["command"] = cmd_by_layer_id[lid_s]
        item["parent_layer_id"] = _resolved_parent_layer_id(lid_s, known_ids, jobs)
        with_git.append((item, lid_s))

    if with_git:
        meta_list = await asyncio.gather(
            *(_layer_git_meta(lid_s) for _item, lid_s in with_git)
        )
        for (item, _lid_s), (dirty, remote) in zip(with_git, meta_list, strict=True):
            item["git_worktree_dirty"] = dirty
            item["git_remote"] = remote

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
