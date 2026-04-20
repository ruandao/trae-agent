"""FastAPI entry: config push, jobs, SSE, public skill, web UI."""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import shutil
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.staticfiles import StaticFiles

from . import git_clone, task_api_bootstrap
from .auth import AuthDep
from .git_clone import (
    _git_core_ssh_command_args,
    clear_clone_layer_log,
    clone_into_new_layer,
    get_clone_layer_log_text,
)
from .hub import hub
from .job_trajectory import load_agent_steps_for_job
from .jobs import JobRecord, job_layer_git_destructive_locked, store
from .layer_changes_saas_push import run_layer_changes_saas_push_loop
from .layer_fs import (
    any_layer_has_git_repo,
    ensure_startup_empty_layer_id,
    list_layer_children,
    list_layer_files,
    list_layers,
    read_layer_file,
    resolved_parent_layer_id,
)
from .layer_git import (
    commit_layer_worktree,
    diff_layer_one_path_vs_parent,
    diff_layer_worktree_vs_parent,
    get_runtime_git_identity,
    git_ahead_of_upstream,
    git_log_at_path,
    git_worktree_dirty,
    latest_commit_log,
    list_layer_changes_vs_parent,
    push_layer_worktree,
    set_runtime_git_identity,
)
from .layer_git import (
    list_branches as list_layer_git_branches,
)
from .layer_graph_saas_push import (
    push_layer_graph_snapshot_if_changed,
    run_layer_graph_saas_push_loop,
)
from .layer_meta import read_layer_meta
from .online_project_view import get_online_project_active_info, set_online_project_tip
from .paths import (
    config_file_path,
    job_events_dir,
    job_events_file,
    layers_root,
    logs_dir,
    service_root,
)
from .request_trace import TRACE_ID_HEADER, trace_id_for_incoming_request, trace_id_var

log = logging.getLogger(__name__)

_layer_graph_snapshot_builder = None
_layer_graph_push_last_sent: list = []
_startup_empty_layer_id: str | None = None


def _trigger_layer_graph_push_now() -> None:
    if _layer_graph_snapshot_builder is None:
        return
    asyncio.create_task(
        push_layer_graph_snapshot_if_changed(
            build_snapshot=_layer_graph_snapshot_builder,
            last_sent=_layer_graph_push_last_sent,
        )
    )


def _strict_bootstrap_enabled() -> bool:
    """Whether Task API bootstrap failures should block service startup."""
    raw = (os.environ.get("TASK_API_BOOTSTRAP_STRICT_STARTUP") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _startup_empty_layer_id
    try:
        await asyncio.to_thread(task_api_bootstrap.bootstrap_container_config)
    except Exception:
        if _strict_bootstrap_enabled():
            raise
        log.exception(
            "startup bootstrap failed; continue without refreshed token/config "
            "(set TASK_API_BOOTSTRAP_STRICT_STARTUP=1 to fail-fast)"
        )

    try:
        jobs_for_empty = await asyncio.to_thread(store.list_jobs)
        eid = await asyncio.to_thread(ensure_startup_empty_layer_id, jobs_for_empty)
        _startup_empty_layer_id = eid
        if eid:
            log.info("startup: 空层级节点 layer_id=%s（复用或新建，并已清理未引用的重复空层）", eid)
    except Exception:
        log.exception("startup: 空层级节点初始化失败")

    bs_lid = task_api_bootstrap.bootstrap_clone_layer_id
    if bs_lid:
        try:
            await store.register_clone_layer_job(
                str(bs_lid).strip(),
                command="[bootstrap] 容器引导克隆",
                output="",
            )
        except Exception:
            log.exception(
                "bootstrap: 登记克隆层任务失败 layer_id=%s",
                bs_lid,
            )
    _layer_graph_task: asyncio.Task | None = None
    _layer_changes_task: asyncio.Task | None = None
    if _layer_graph_snapshot_builder is not None:
        _layer_graph_task = asyncio.create_task(
            run_layer_graph_saas_push_loop(
                _layer_graph_snapshot_builder, _layer_graph_push_last_sent
            )
        )
        _layer_changes_task = asyncio.create_task(
            run_layer_changes_saas_push_loop(_layer_graph_snapshot_builder)
        )
    try:
        yield
    finally:
        if _layer_graph_task is not None:
            _layer_graph_task.cancel()
            with suppress(asyncio.CancelledError):
                await _layer_graph_task
        if _layer_changes_task is not None:
            _layer_changes_task.cancel()
            with suppress(asyncio.CancelledError):
                await _layer_changes_task


app = FastAPI(title="Trae Online Service", version="1.0.0", lifespan=_lifespan)

app.mount(
    "/static",
    StaticFiles(directory=str(service_root() / "static")),
    name="static",
)

_request_access_logger = logging.getLogger("trae_online.http_requests")


def _request_path_with_qs(request: Request) -> str:
    if request.url.query:
        return f"{request.url.path}?{request.url.query}"
    return request.url.path


def _ensure_request_access_file_handler() -> None:
    log_path = (logs_dir() / "requests.log").resolve()
    logger = _request_access_logger
    logger.setLevel(logging.INFO)
    for h in logger.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            try:
                if Path(h.baseFilename).resolve() == log_path:
                    return
            except OSError:
                continue
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)
    logger.propagate = False


class _RequestAccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            client = request.client.host if request.client else "-"
            _request_access_logger.info(
                '%s "%s %s" error %.2fms',
                client,
                request.method,
                _request_path_with_qs(request),
                elapsed_ms,
            )
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000
        client = request.client.host if request.client else "-"
        _request_access_logger.info(
            '%s "%s %s" %d %.2fms',
            client,
            request.method,
            _request_path_with_qs(request),
            response.status_code,
            elapsed_ms,
        )
        return response


_ensure_request_access_file_handler()


class _TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        tid = trace_id_for_incoming_request(request)
        tok = trace_id_var.set(tid)
        try:
            response = await call_next(request)
            response.headers[TRACE_ID_HEADER] = tid
            return response
        finally:
            trace_id_var.reset(tok)


# 后注册者在外层；TraceId 须先于访问日志执行，故先加访问日志、再加 TraceId。
app.add_middleware(_RequestAccessLogMiddleware)
app.add_middleware(_TraceIdMiddleware)


def _job_to_api_dict(rec: JobRecord) -> dict[str, Any]:
    d = rec.to_dict()
    d["git_destructive_locked"] = job_layer_git_destructive_locked(rec)
    return d


def _parse_token_int(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    return n


def _step_token_total(step: Any) -> int | None:
    if not isinstance(step, dict):
        return None
    lr = step.get("llm_response")
    usage = lr.get("usage") if isinstance(lr, dict) else None
    if not isinstance(usage, dict):
        usage = step.get("usage")
    if not isinstance(usage, dict):
        return None
    for key in ("total_tokens", "total", "tokens", "token_count", "total_token"):
        n = _parse_token_int(usage.get(key))
        if n is not None:
            return n
    input_n = _parse_token_int(
        usage.get("input_tokens", usage.get("prompt_tokens", usage.get("input")))
    )
    output_n = _parse_token_int(
        usage.get(
            "output_tokens",
            usage.get("completion_tokens", usage.get("output")),
        )
    )
    if input_n is None and output_n is None:
        return None
    return (input_n or 0) + (output_n or 0)


def _step_model_name(step: Any) -> str:
    if not isinstance(step, dict):
        return ""
    for key in ("model", "model_name", "llm_model"):
        v = step.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    lr = step.get("llm_response")
    if isinstance(lr, dict):
        v = lr.get("model")
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _summarize_job_steps_usage(payload: dict[str, Any]) -> tuple[list[str], int | None]:
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return ([], None)
    models: list[str] = []
    seen: set[str] = set()
    total = 0
    has_any_token = False
    for step in steps:
        model = _step_model_name(step)
        if model and model not in seen:
            seen.add(model)
            models.append(model)
        delta = _step_token_total(step)
        if delta is not None:
            has_any_token = True
            total += delta
    return (models, total if has_any_token else None)


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
    env: dict[str, str] | None = Field(
        default=None,
        description="可选：为该任务执行进程注入额外环境变量。",
    )


def _git_clone_command_label(url: str, branch: str | None) -> str:
    u = (url or "").strip()
    parts: list[str] = ["git", "clone"]
    if branch and str(branch).strip():
        parts.extend(["-b", str(branch).strip()])
    parts.append(u)
    return " ".join(parts)


class CloneRepoBody(BaseModel):
    """将远程仓库克隆到新的可写层根目录（空目录内 ``git clone … .``）。"""

    url: str = Field(..., min_length=1, max_length=4096)
    branch: str | None = None
    depth: int | None = Field(default=None, ge=1, le=10_000)
    ssh_identity_file: str | None = Field(
        default=None,
        max_length=4096,
        description="可选：本机 SSH 私钥路径；克隆 ``git@`` / ``ssh://`` 时通过 "
        "``git -c core.sshCommand='ssh -i <path> …'`` 指定密钥（路径解析为绝对路径后传入）。",
    )
    ephemeral_ssh_private_key: str | None = Field(
        default=None,
        max_length=65536,
        description="可选：单次请求临时 SSH 私钥（PEM）；仅写入临时文件并在克隆结束后删除。"
        "用于 ``git@`` / ``ssh://`` 克隆；若 URL 为 HTTPS 且提供私钥，服务端会改用 SSH 形式远程。",
    )
    parent_layer_id: str | None = Field(
        default=None,
        max_length=128,
        description="可选：父层 layer_id，用于将克隆层挂载到指定父层下。",
    )


class RepoRecloneBody(BaseModel):
    """重新克隆仓库：删除容器内旧克隆目录并重新克隆。"""

    repo_url: str = Field(..., min_length=1, max_length=4096)
    ephemeral_ssh_private_key: str | None = Field(
        default=None,
        max_length=65536,
        description="SaaS 下发的临时 SSH 私钥；与引导克隆一致，HTTPS 远程将按 SSH 形式克隆。",
    )


class ProjectViewBody(BaseModel):
    """将 ``onlineProject`` 指向某可写层 tip（联合视图）。"""

    layer_id: str = Field(..., min_length=1, max_length=256)


class LayerQueueBody(BaseModel):
    """在节点任务运行中/排队时，向该可写层待执行队列追加一条指令。"""

    command: str = Field(..., min_length=1)
    command_kind: Literal["trae", "shell"] = "trae"


class LayerGitCommitBody(BaseModel):
    """在工作区内执行 ``git add -A`` 与 ``git commit``。

    ``message`` 为空时由服务端自动生成说明：约定式主题行（type/scope）、
    若存在 ``.trajectories/trajectory_*.json`` 则附带代理结论与步骤节选，以及变更统计与文件列表。
    """

    message: str | None = Field(default=None, max_length=4096)


class LayerGitPushBody(BaseModel):
    """在工作区内执行 ``git push``；可指定推送目标分支。"""

    target_branch: str | None = Field(default=None, max_length=256)
    ephemeral_ssh_private_key: str | None = Field(
        default=None,
        max_length=65536,
        description="单次请求临时 SSH 私钥（PEM）；仅写入临时文件并在 push 结束后删除。",
    )
    ephemeral_git_remote_username: str | None = Field(default=None, max_length=255)


class GitIdentityBody(BaseModel):
    """容器级当前 Git 身份（用于后续 commit/push）。"""

    name: str = Field(..., min_length=1, max_length=255)
    email: str = Field(..., min_length=3, max_length=320)


def _command_for_layer_id(layer_id: str) -> str | None:
    for j in store.list_jobs():
        if j.layer_id == layer_id:
            return j.command
    return None


def _http_detail_for_exec_log(detail: Any) -> str:
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail
    try:
        return json.dumps(detail, ensure_ascii=False)
    except TypeError:
        return str(detail)


async def _notify_layer_git_exec_log(
    layer_id: str,
    op: Literal["commit", "push"],
    *,
    result: dict[str, Any] | None = None,
    http_exc: HTTPException | None = None,
) -> None:
    """Git 提交/推送结果同步到任务云执行日志（与容器克隆进度同一上报通道）。"""
    parts: list[str] = []
    if http_exc is not None:
        d = _http_detail_for_exec_log(http_exc.detail).strip()
        if len(d) > 3200:
            d = d[:3200] + "…"
        parts.append(f"可写层 {layer_id}：Git {op} 失败")
        if d:
            parts.append(d)
    elif result is not None:
        st = str(result.get("status") or "").strip() or "?"
        summary = str(result.get("summary") or "").strip()
        head = f"可写层 {layer_id}：Git {op} 完成（{st}）"
        if summary:
            head += f" — {summary}"
        parts.append(head)
        out = str(result.get("output") or "").strip()
        if out:
            if len(out) > 2400:
                out = out[:2400] + "…"
            parts.append(out)
    msg = "\n".join(parts).strip()
    if not msg:
        return
    await task_api_bootstrap.notify_container_execution_log(msg)


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
async def push_config(
    _: AuthDep,
    file: UploadFile = File(...),  # noqa: B008
) -> dict[str, str]:
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
            ssh_identity_file=body.ssh_identity_file,
            ephemeral_ssh_private_key=body.ephemeral_ssh_private_key,
            parent_layer_id=body.parent_layer_id,
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
    try:
        out_trim = out if len(out) <= 120_000 else out[-120_000:] + "\n…(truncated)\n"
        await store.register_clone_layer_job(
            layer_id,
            command=_git_clone_command_label(body.url, body.branch),
            output=out_trim,
        )
    except Exception:
        log.exception("clone: 登记克隆层任务失败 layer_id=%s", layer_id)
    await hub.publish(
        {
            "type": "repo_cloned",
            "layer_id": layer_id,
            "title": "仓库已就绪",
        }
    )
    await _schedule_clear_clone_log(layer_id)
    try:
        await asyncio.to_thread(set_online_project_tip, layer_id)
    except Exception:
        log.exception("clone: 无法将 onlineProject 指向层 %s", layer_id)
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


@app.post("/api/repos/reclone")
async def repo_reclone(_: AuthDep, body: RepoRecloneBody) -> dict[str, Any]:
    """重新克隆仓库：删除容器内旧克隆目录并重新克隆。

    优先使用引导克隆层；若内存中未记录（启动异常、跳过克隆等），则根据磁盘上的仓库目录、
    ``onlineProject`` 指向的层解析；仍无时新建一层并克隆。
    """
    from .layers import layer_path
    from .task_api_bootstrap import (
        _bootstrap_git_clone_timeout_sec,
        _bootstrap_write_ssh_keyfile,
        _clone_subprocess_env,
        _git_clone_remote_for_ssh_pem,
        _git_config_prefix,
        _make_ipv4_curl_config_file,
        _max_clone_attempts,
        _repo_dir_name_from_url,
        _sleep_before_retry,
        default_repo_dir_for_reclone,
        ensure_reclone_layer_id,
        find_existing_repo_dir_in_layer,
    )

    repo_url = body.repo_url.strip()
    repo_dir_name = _repo_dir_name_from_url(repo_url)
    lid = ensure_reclone_layer_id(repo_url)
    layer_root = layer_path(lid)
    if not layer_root.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"克隆层目录不存在: {lid}",
        )

    existing = find_existing_repo_dir_in_layer(lid, repo_dir_name)
    repo_dir = existing if existing else default_repo_dir_for_reclone(lid, repo_dir_name)

    if repo_dir.exists():
        log.info("reclone: 删除旧克隆目录 %s 并重新克隆 %s", repo_dir.name, repo_url[:200])
        shutil.rmtree(repo_dir, ignore_errors=True)
    else:
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        log.info("reclone: 在层 %s 路径 %s 首次克隆 %s", lid, repo_dir.name, repo_url[:200])

    tout = _bootstrap_git_clone_timeout_sec()
    max_attempts = _max_clone_attempts()
    git_env = _clone_subprocess_env()
    pem = (body.ephemeral_ssh_private_key or "").strip()
    if not pem and repo_url.lower().startswith("https://"):
        log.warning(
            "reclone: 未收到 ephemeral_ssh_private_key，将使用 HTTPS 克隆（私有仓库通常会失败）；"
            "请确认 SaaS 已解析身份并转发私钥"
        )
    _ssh_url_via_env = os.environ.get("GIT_CLONE_USE_SSH_URL", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if pem or _ssh_url_via_env and repo_url.lower().startswith("https://"):
        clone_remote = _git_clone_remote_for_ssh_pem(repo_url)
    else:
        clone_remote = repo_url

    for attempt in range(max_attempts):
        ipv4_curl_cfg = None
        ssh_keyfile: str | None = None
        try:
            ipv4_curl_cfg = _make_ipv4_curl_config_file()
            if pem:
                ssh_keyfile = await asyncio.to_thread(_bootstrap_write_ssh_keyfile, pem)
            cmd = list(_git_config_prefix())
            if ipv4_curl_cfg is not None:
                cmd.extend(["-c", f"http.curlConfig={ipv4_curl_cfg}"])
            if ssh_keyfile:
                cmd.extend(
                    _git_core_ssh_command_args(
                        ssh_keyfile,
                        user_known_hosts_file_dev_null=True,
                    )
                )
            cmd.extend(["clone", "--progress", clone_remote, str(repo_dir)])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=git_env,
            )
            assert proc.stdout is not None
            out_parts: list[str] = []
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                out_parts.append(chunk.decode(errors="replace"))

            code = await proc.wait()
            out = "".join(out_parts)

            if code == 0:
                try:
                    head_sha = await asyncio.to_thread(
                        git_clone.verify_git_clone_workspace,
                        repo_dir,
                        env=git_env,
                    )
                except RuntimeError as ver_e:
                    out = out + f"\n[clone-verify] {ver_e}\n"
                    if attempt < max_attempts - 1:
                        log.warning(
                            "reclone 校验失败将重试 (%d/%d): %s",
                            attempt + 1,
                            max_attempts,
                            ver_e,
                        )
                        shutil.rmtree(repo_dir, ignore_errors=True)
                        await _sleep_before_retry(attempt)
                        continue
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "message": "克隆后校验失败",
                            "repo_url": repo_url,
                            "repo_dir": repo_dir_name,
                            "error": str(ver_e),
                            "output": out[-2000:] if len(out) > 2000 else out,
                        },
                    ) from ver_e
                log.info(
                    "reclone 完成: %s HEAD=%s", repo_dir.name, head_sha[:12] if head_sha else "?"
                )
                try:
                    await asyncio.to_thread(set_online_project_tip, lid)
                except Exception:
                    log.exception("reclone: 无法更新 onlineProject 指向层 %s", lid)
                return {
                    "status": "ok",
                    "repo_url": repo_url,
                    "repo_dir": repo_dir_name,
                    "head_sha": head_sha,
                    "output": out[-2000:] if len(out) > 2000 else out,
                }

            if attempt < max_attempts - 1 and git_clone._looks_like_transient_fetch_error(out):
                log.warning(
                    "reclone 网络错误将重试 (%d/%d): %s",
                    attempt + 1,
                    max_attempts,
                    repo_url[:256],
                )
                shutil.rmtree(repo_dir, ignore_errors=True)
                await _sleep_before_retry(attempt)
                continue

            raise HTTPException(
                status_code=400,
                detail={
                    "message": "git clone 失败",
                    "repo_url": repo_url,
                    "repo_dir": repo_dir_name,
                    "exit_code": code,
                    "output": out[-2000:] if len(out) > 2000 else out,
                },
            )
        except TimeoutError:
            out = "".join(out_parts) if out_parts else ""
            if attempt < max_attempts - 1:
                log.warning(
                    "reclone 超时将重试 (%d/%d): %s",
                    attempt + 1,
                    max_attempts,
                    repo_url[:256],
                )
                shutil.rmtree(repo_dir, ignore_errors=True)
                await _sleep_before_retry(attempt)
                continue
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "git clone 超时",
                    "repo_url": repo_url,
                    "repo_dir": repo_dir_name,
                    "timeout_sec": tout,
                    "output": out[-2000:] if len(out) > 2000 else out,
                },
            ) from None
        finally:
            if ssh_keyfile is not None:
                with suppress(OSError):
                    os.unlink(ssh_keyfile)
            if ipv4_curl_cfg is not None:
                with suppress(OSError):
                    ipv4_curl_cfg.unlink(missing_ok=True)

    raise RuntimeError("repo_reclone: retry loop exited without return")


@app.post("/api/jobs")
async def create_job(_: AuthDep, body: JobCreateBody) -> dict[str, Any]:
    try:
        rec = await store.create_job(
            body.command.strip(),
            body.parent_job_id,
            repo_layer_id=body.repo_layer_id.strip() if body.repo_layer_id else None,
            git_branch=body.git_branch.strip() if body.git_branch else None,
            command_kind=body.command_kind,
            command_env=body.env,
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


@app.get("/api/jobs/{job_id}/events")
async def get_job_events(
    _: AuthDep,
    job_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    """按行偏移读取任务结构化 JSONL 事件。"""
    rec = store.get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job not found")
    p = job_events_file(job_id)
    d = job_events_dir() / str(job_id)
    events: list[dict[str, Any]] = []
    if d.is_dir():
        files = sorted(d.glob("step_*.json"))
        total = len(files)
        end = min(total, offset + limit)
        for fp in files[offset:end]:
            try:
                row = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(row, dict):
                    events.append(row)
            except (OSError, json.JSONDecodeError):
                continue
    elif p.is_file():
        # 兼容旧版 JSONL
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"read job events failed: {e}") from e
        total = len(lines)
        end = min(total, offset + limit)
        for line in lines[offset:end]:
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    events.append(row)
            except json.JSONDecodeError:
                continue
    else:
        total = 0
        end = offset
    return {
        "job_id": job_id,
        "events": events,
        "offset": offset,
        "next_offset": end,
        "total": total,
        "truncated": end < total,
    }


@app.get("/api/jobs/{job_id}/steps")
async def job_agent_steps(_: AuthDep, job_id: str) -> dict[str, Any]:
    """优先从该 job 的 ``.trae_agent_json/{job_id}`` 读取步骤；否则回退到层目录最新 ``.trajectories``。"""
    rec = store.get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        payload = load_agent_steps_for_job(rec.layer_path, job_id)
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


@app.get("/api/layers/{layer_id}/git/log")
async def api_layer_git_log(
    _: AuthDep,
    layer_id: str,
    path: Annotated[
        str | None, Query(description="层内目录相对路径；省略则返回各仓库最近一次提交")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """在指定目录下查看 ``git log``；无 ``path`` 时等价于各仓库 ``git log -1`` 汇总。"""
    p = (path or "").strip()
    if not p:
        return await latest_commit_log(layer_id)
    return await git_log_at_path(layer_id, p, limit=limit)


@app.get("/api/layers/{layer_id}/git/commit/latest-log")
async def api_layer_git_commit_latest_log(_: AuthDep, layer_id: str) -> dict[str, Any]:
    """各 git 仓库最近一次提交（``git log -1 --stat``），与 ``GET .../git/log`` 无 path 时一致。"""
    return await latest_commit_log(layer_id)


@app.post("/api/layers/{layer_id}/git/commit")
async def api_layer_git_commit(
    _: AuthDep,
    layer_id: str,
    body: LayerGitCommitBody,
) -> dict[str, Any]:
    """将当前可写层工作区全部暂存并提交到本地仓库（不 push）。"""
    try:
        result = await commit_layer_worktree(
            layer_id,
            body.message,
            command_hint=_command_for_layer_id(layer_id),
        )
    except HTTPException as e:
        await _notify_layer_git_exec_log(layer_id, "commit", http_exc=e)
        raise
    await _notify_layer_git_exec_log(layer_id, "commit", result=result)
    _trigger_layer_graph_push_now()
    return result


@app.post("/api/layers/{layer_id}/git/push")
async def api_layer_git_push(
    _: AuthDep,
    layer_id: str,
    body: LayerGitPushBody | None = None,
) -> dict[str, Any]:
    """将当前分支推送到已配置的上游远程。"""
    target_branch = None
    ephemeral_ssh_private_key = None
    ephemeral_git_remote_username = None
    if body is not None and body.target_branch is not None:
        target_branch = body.target_branch.strip() or None
    if body is not None:
        ephemeral_ssh_private_key = body.ephemeral_ssh_private_key
        if body.ephemeral_git_remote_username is not None:
            ephemeral_git_remote_username = body.ephemeral_git_remote_username.strip() or None
    try:
        result = await push_layer_worktree(
            layer_id,
            target_branch=target_branch,
            ephemeral_ssh_private_key=ephemeral_ssh_private_key,
            ephemeral_git_remote_username=ephemeral_git_remote_username,
        )
    except HTTPException as e:
        await _notify_layer_git_exec_log(layer_id, "push", http_exc=e)
        raise
    await _notify_layer_git_exec_log(layer_id, "push", result=result)
    _trigger_layer_graph_push_now()
    return result


@app.get("/api/git/identity")
async def api_git_identity_get(_: AuthDep) -> dict[str, Any]:
    identity = get_runtime_git_identity()
    return {"status": "ok", "name": identity["name"], "email": identity["email"]}


@app.post("/api/git/identity")
async def api_git_identity_set(_: AuthDep, body: GitIdentityBody) -> dict[str, Any]:
    identity = set_runtime_git_identity(body.name, body.email)
    return {"status": "ok", "name": identity["name"], "email": identity["email"]}


@app.get("/api/layers/{layer_id}/diff/parent/files")
async def api_layer_diff_parent_files(_: AuthDep, layer_id: str) -> dict[str, Any]:
    """子层相对已解析父层的变动路径列表（``diff -rq`` 解析摘要，含 ``.git``）。"""
    layers = await asyncio.to_thread(list_layers)
    known_ids = {str(x.get("layer_id")) for x in layers if x.get("layer_id")}
    jobs = await asyncio.to_thread(store.list_jobs)
    parent = resolved_parent_layer_id(layer_id, known_ids, jobs)
    if not parent:
        return {
            "layer_id": layer_id,
            "parent_layer_id": None,
            "same": None,
            "changes": [],
            "truncated": False,
            "detail": "无父层可对比（根层或父层目录已不存在）",
        }
    return await asyncio.to_thread(list_layer_changes_vs_parent, parent, layer_id)


@app.get("/api/layers/{layer_id}/diff/parent/file")
async def api_layer_diff_parent_file(
    _: AuthDep,
    layer_id: str,
    path: str = Query(..., min_length=1, description="层内相对 POSIX 路径"),
) -> dict[str, Any]:
    """单路径相对已解析父层的 unified diff（文件 ``diff -uN``；目录 ``diff -ruN``，含 ``.git``）。"""
    layers = await asyncio.to_thread(list_layers)
    known_ids = {str(x.get("layer_id")) for x in layers if x.get("layer_id")}
    jobs = await asyncio.to_thread(store.list_jobs)
    parent = resolved_parent_layer_id(layer_id, known_ids, jobs)
    if not parent:
        raise HTTPException(
            status_code=400,
            detail="无父层可对比（根层或父层目录已不存在）",
        )
    return await asyncio.to_thread(diff_layer_one_path_vs_parent, parent, layer_id, path)


@app.get("/api/layers/{layer_id}/diff/parent")
async def api_layer_diff_parent(_: AuthDep, layer_id: str) -> dict[str, Any]:
    """当前可写层工作区目录与已解析父层目录的差异（``diff -ruN``，含 ``.git``）。"""
    layers = list_layers()
    known_ids = {str(x.get("layer_id")) for x in layers if x.get("layer_id")}
    jobs = store.list_jobs()
    parent = resolved_parent_layer_id(layer_id, known_ids, jobs)
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


@app.post("/api/project/view")
async def api_set_project_view(_: AuthDep, body: ProjectViewBody) -> dict[str, Any]:
    """将 ``onlineProject`` 指向某可写层 tip（与层关系树中选中节点一致）。"""
    try:
        await asyncio.to_thread(set_online_project_tip, body.layer_id.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    info = await asyncio.to_thread(get_online_project_active_info)
    return {"status": "ok", **info}


@app.get("/api/project/active")
async def api_project_active(_: AuthDep) -> dict[str, Any]:
    """当前 ``onlineProject`` 指向的 tip 层（符号链接解析）。"""
    return await asyncio.to_thread(get_online_project_active_info)


@app.get("/api/layers/empty-root")
async def api_get_empty_root_layer(_: AuthDep) -> dict[str, Any]:
    """获取服务启动时创建的空层级节点 ID，用于作为克隆仓库的父层。"""
    return {"layer_id": _startup_empty_layer_id}


async def _layer_git_meta(lid: str) -> tuple[bool | None, dict[str, Any]]:
    """在线程中跑 git 子进程，避免阻塞事件循环。

    同一 layer 的两次调用必须串行：二者都会物化 merged 目录，并行会并发 rmtree/copy 同一 dest。
    """
    dirty = await asyncio.to_thread(git_worktree_dirty, lid)
    remote = await asyncio.to_thread(git_ahead_of_upstream, lid)
    return dirty, remote


def _layer_row_is_empty_anchor(layer_id: str) -> bool:
    """仅含 ``layer_meta.kind=empty`` 的锚点层：供克隆 API 使用，不进入层关系 UI 列表。"""
    if not layer_id:
        return False
    m = read_layer_meta(layer_id)
    return m is not None and m.kind == "empty"


async def build_layer_graph_snapshot_for_saas() -> dict[str, Any]:
    """与 GET /api/layers 相同的层列表富集逻辑，并附带 jobs 列表供 SaaS 评论区 zTree。"""
    layers, jobs = await asyncio.gather(
        asyncio.to_thread(list_layers),
        asyncio.to_thread(store.list_jobs),
    )
    # ``kind=empty`` 仅作克隆父锚点，不应出现在串行列表（避免每次重启多一条「无 git」空层）
    layers = [
        x for x in layers if not _layer_row_is_empty_anchor(str(x.get("layer_id") or "").strip())
    ]
    cmd_by_layer_id: dict[str, str] = {j.layer_id: j.command for j in jobs}
    job_by_layer_id: dict[str, JobRecord] = {}
    for j in jobs:
        if j.layer_id not in job_by_layer_id:
            job_by_layer_id[j.layer_id] = j
    known_ids = {str(x.get("layer_id")) for x in layers if x.get("layer_id")}
    with_git: list[tuple[dict[str, Any], str]] = []
    for item in layers:
        lid = item.get("layer_id")
        if not lid:
            continue
        lid_s = str(lid)
        if lid_s in cmd_by_layer_id:
            item["command"] = cmd_by_layer_id[lid_s]
        lm = read_layer_meta(lid_s)
        if lm:
            item["meta_kind"] = lm.kind
        item["parent_layer_id"] = resolved_parent_layer_id(lid_s, known_ids, jobs)
        jrec = job_by_layer_id.get(lid_s)
        if jrec:
            item["job_id"] = jrec.id
            item["job_status"] = jrec.status
        else:
            item["job_id"] = None
            item["job_status"] = None
        item["queue_depth"] = store.layer_queue_depth(lid_s)
        if jrec and jrec.status in ("pending", "running"):
            item["mind_state"] = "running"
        else:
            item["mind_state"] = "idle_done"
        with_git.append((item, lid_s))

    if with_git:
        meta_list = await asyncio.gather(*(_layer_git_meta(lid_s) for _item, lid_s in with_git))
        for (item, _lid_s), (dirty, remote) in zip(with_git, meta_list, strict=True):
            item["git_worktree_dirty"] = dirty
            item["git_remote"] = remote

    bs = task_api_bootstrap.bootstrap_clone_layer_id
    bs_s = str(bs).strip() if bs else ""
    if bs_s:
        idx = next(
            (i for i, x in enumerate(layers) if str(x.get("layer_id", "")) == bs_s),
            None,
        )
        if idx is not None and idx > 0:
            layers.insert(0, layers.pop(idx))

    jobs_api = [_job_to_api_dict(j) for j in jobs]
    jobs_api_by_id: dict[str, dict[str, Any]] = {
        str(x.get("id")): x for x in jobs_api if x.get("id")
    }
    latest_job_by_layer: dict[str, JobRecord] = {}
    latest_job_rank: dict[str, tuple[str, int]] = {}
    for idx, rec in enumerate(jobs):
        lid = str(rec.layer_id or "").strip()
        if not lid or rec.command_kind == "clone":
            continue
        cur_rank = (str(rec.created_at or ""), idx)
        prev_rank = latest_job_rank.get(lid)
        if prev_rank is None or cur_rank >= prev_rank:
            latest_job_rank[lid] = cur_rank
            latest_job_by_layer[lid] = rec
    for rec in latest_job_by_layer.values():
        try:
            payload = load_agent_steps_for_job(rec.layer_path, rec.id)
        except ValueError:
            continue
        models, total_tokens = _summarize_job_steps_usage(payload)
        row = jobs_api_by_id.get(rec.id)
        if not row:
            continue
        if models:
            row["llm_models"] = models
            row["llm_model"] = models[0]
        if total_tokens is not None:
            row["llm_total_tokens"] = total_tokens
    return {
        "layers": layers,
        "jobs": jobs_api,
        "layers_root": str(layers_root().resolve()),
        "bootstrap_layer_id": bs_s or None,
    }


@app.get("/api/layers")
async def api_list_layers(_: AuthDep) -> dict[str, Any]:
    snap = await build_layer_graph_snapshot_for_saas()
    return {
        "layers": snap["layers"],
        "layers_root": snap["layers_root"],
        "bootstrap_layer_id": snap.get("bootstrap_layer_id"),
    }


@app.delete("/api/layers/{layer_id}")
async def api_delete_layer(_: AuthDep, layer_id: str) -> dict[str, Any]:
    """删除可写层：若任务运行中则先中断；含子任务时自底向上删除。"""
    lid = _valid_layer_id_param(layer_id)
    try:
        return await store.delete_layer_by_layer_id(lid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/layers/{layer_id}/queue")
async def api_layer_enqueue(_: AuthDep, layer_id: str, body: LayerQueueBody) -> dict[str, Any]:
    """向该层当前排队/运行中的任务追加待执行指令（队列在任务完成时消费）。"""
    lid = _valid_layer_id_param(layer_id)
    try:
        return await store.enqueue_layer_command(lid, body.command, command_kind=body.command_kind)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/layers/{layer_id}/files")
async def api_list_layer_files(
    _: AuthDep,
    layer_id: str,
    prefix: str | None = Query(
        default=None, description="可选：相对路径前缀过滤（只支持前缀，不支持 .. / 绝对路径）"
    ),
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
    prefix: str | None = Query(
        default=None, description="前缀过滤（相对路径，支持只看 src/ 之类）"
    ),
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


_layer_graph_snapshot_builder = build_layer_graph_snapshot_for_saas
