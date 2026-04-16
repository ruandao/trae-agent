"""任务容器启动时：换票、拉取任务详情（project_repos）、克隆到同一个新建层、再拉取 YAML。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

import yaml

from .git_clone import (
    _clone_subprocess_env,
    _git_config_prefix,
    _git_core_ssh_command_args,
    _looks_like_transient_fetch_error,
    _make_ipv4_curl_config_file,
    _max_clone_attempts,
    _sleep_before_retry,
    append_clone_layer_log,
    clear_clone_layer_log,
    get_clone_layer_log_text,
    verify_git_clone_workspace,
)
from .layers import create_root_layer, new_layer_id
from .paths import config_file_path, req_logs_dir
from .request_trace import TRACE_ID_HEADER, get_trace_id_for_outbound_http

log = logging.getLogger(__name__)
_outbound_req_logger = logging.getLogger("trae_online.outbound_http")

# 容器启动引导克隆所用的 layer_id，供 UI GET /api/repos/bootstrap-clone-log 展示日志（无 SSE）。
bootstrap_clone_layer_id: str | None = None


def _bootstrap_http_timeout_sec() -> float:
    raw = os.environ.get("TASK_API_BOOTSTRAP_TIMEOUT_SEC", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            log.warning("忽略无效的 TASK_API_BOOTSTRAP_TIMEOUT_SEC=%r，使用默认 5s", raw)
    return 5.0


def _rewrite_host_docker_internal_url(url: str) -> str:
    """
    将 URL 中的 host.docker.internal 换为数值 IP（仅当显式设置 DOCKER_HOST_GATEWAY_IP 时）。

    未设置时保留主机名，以便 HTTP Host 与 Django ALLOWED_HOSTS 中的 host.docker.internal 一致；
    模拟启动（Docker Desktop）等场景下容器内 DNS 可解析该主机名。
    """
    u = url.strip()
    if not u:
        return u
    p = urlsplit(u)
    if (p.hostname or "").lower() != "host.docker.internal":
        return u
    ip = os.environ.get("DOCKER_HOST_GATEWAY_IP", "").strip()
    if not ip:
        return u
    nu = p.netloc
    if "@" in nu:
        ui, _, _hp = nu.rpartition("@")
        ui_prefix = ui + "@"
    else:
        ui_prefix = ""
    port = p.port
    new_host_part = f"{ip}:{port}" if port is not None else ip
    netloc = ui_prefix + new_host_part
    out = urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    if out != u:
        log.info("已将 host.docker.internal 替换为宿主机 IP：%s -> %s", u, out)
    return out


def _git_clone_remote_for_ssh_pem(canonical_url: str) -> str:
    """当使用 SSH 私钥引导克隆时，将常见 ``https://`` 远程转为 ``git@host:path.git``。

    ``git -c core.sshCommand=…`` 仅对基于 SSH 的传输生效；若仍用 ``https://`` 克隆 GitHub/GitLab 等，
    私有仓库会触发 ``could not read Username for 'https://github.com'``。
    """
    u = (canonical_url or "").strip()
    if not u:
        return u
    low = u.lower()
    if low.startswith("git@") or low.startswith("ssh://"):
        return u
    if not low.startswith("https://"):
        return u
    parts = urlsplit(u)
    host = (parts.hostname or "").lower()
    if host == "www.github.com":
        host = "github.com"
    path = (parts.path or "").strip().rstrip("/")
    if not host or not path:
        return u
    path_body = path.lstrip("/")
    if not path_body or ".." in path_body:
        return u
    if not path_body.endswith(".git"):
        path_body = f"{path_body}.git"
    return f"git@{host}:{path_body}"


def _task_api_prefix() -> str | None:
    endpoint = _rewrite_host_docker_internal_url(os.environ.get("TaskApiEndPoint", "").strip())
    if not endpoint:
        return None
    tenant = os.environ.get("tenantId", "").strip()
    workspace = os.environ.get("workspaceId", "").strip()
    task = os.environ.get("taskId", "").strip()
    if not (tenant and workspace and task):
        raise RuntimeError(
            "已设置 TaskApiEndPoint 时，tenantId、workspaceId、taskId 均须为非空字符串"
        )
    base = endpoint.rstrip("/")
    return f"{base}/api/tenant/{tenant}/workspace/{workspace}/task/{task}/cloud"


def _business_api_endpoint() -> str:
    raw = os.environ.get("BusinessApiEndPoint", "").strip()
    if not raw:
        raw = os.environ.get("BUSINESS_API_ENDPOINT", "").strip()
    if not raw:
        raise RuntimeError(
            "已配置任务云 API 路径但 BusinessApiEndPoint/BUSINESS_API_ENDPOINT 为空，"
            "无法进行 exchange-refresh"
        )
    raw = _rewrite_host_docker_internal_url(raw)
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("BusinessApiEndPoint/BUSINESS_API_ENDPOINT 必须是合法的 http(s) URL")
    return raw.rstrip("/")


def _skip_exchange_for_local_business_api(endpoint: str) -> bool:
    """BusinessApiEndPoint 指向本机 onlineService 时跳过换票，保留 ACCESS_TOKEN 便于 /ui/<token> 本地调试。

    注意：此跳过逻辑仅在 TaskApiEndPoint 也指向本机 Django 时生效（本地开发调试场景）。
    若 TaskApiEndPoint 指向宿主机 Django（容器环境），即使 BusinessApiEndPoint 指向本机 onlineService，
    也必须执行换票以获取访问 Django API 的有效 access_token。
    """
    e = endpoint.rstrip("/").lower()
    listen = os.environ.get("PORT", "").strip()
    try:
        pnum = int(listen) if listen else 8765
    except ValueError:
        pnum = 8765
    for host in ("127.0.0.1", "localhost"):
        base = f"http://{host}:{pnum}"
        if e == base or e.startswith(f"{base}/"):
            task_api_ep = (os.environ.get("TaskApiEndPoint", "") or "").strip().lower()
            if not task_api_ep:
                return True
            for h in ("127.0.0.1", "localhost", "::1"):
                if task_api_ep.startswith(f"http://{h}:") or task_api_ep.startswith(f"http://{h}/"):
                    return True
            return False
    base6 = f"http://[::1]:{pnum}"
    if e == base6 or e.startswith(f"{base6}/"):
        task_api_ep = (os.environ.get("TaskApiEndPoint", "") or "").strip().lower()
        if not task_api_ep:
            return True
        for h in ("127.0.0.1", "localhost", "::1"):
            if task_api_ep.startswith(f"http://{h}:") or task_api_ep.startswith(f"http://{h}/"):
                return True
        return False
    return False


def _redact_for_log(obj: Any) -> Any:
    """脱敏后再写入 reqLogs，避免 access_token / refresh_token 等落盘。"""
    sensitive = frozenset(
        {
            "access_token",
            "refresh_token",
            "password",
            "client_secret",
            "authorization",
        }
    )

    def key_sensitive(k: object) -> bool:
        lk = str(k).lower().replace("-", "_")
        if lk in sensitive:
            return True
        if lk.endswith("_secret"):
            return True
        return lk.endswith("_token") or "private_key" in lk

    if isinstance(obj, dict):
        return {k: ("***" if key_sensitive(k) else _redact_for_log(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_for_log(x) for x in obj]
    return obj


def _redact_error_detail_snippet(text: str, max_len: int = 2048) -> str:
    text = text[:max_len]
    try:
        parsed = json.loads(text)
        return json.dumps(_redact_for_log(parsed), ensure_ascii=False)
    except json.JSONDecodeError:
        return text


def _response_body_snippet_for_log(parsed: Any, max_len: int = 2048) -> str:
    """成功响应写入 reqLogs 时的脱敏摘要（与 error detail 同量级，避免单行过大）。"""
    s = json.dumps(_redact_for_log(parsed), ensure_ascii=False)
    if len(s) > max_len:
        return s[:max_len] + "…"
    return s


def _flush_outbound_log_handlers() -> None:
    for h in _outbound_req_logger.handlers:
        flush = getattr(h, "flush", None)
        if callable(flush):
            with contextlib.suppress(OSError, ValueError):
                flush()


def _ensure_outbound_req_log_handler() -> None:
    log_path = (req_logs_dir() / "outbound.log").resolve()
    logger = _outbound_req_logger
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


def _log_outbound_post(
    *,
    step: str,
    url: str,
    body: dict,
    elapsed_ms: float,
    status: int | None,
    error_kind: str | None,
    error_detail: str | None = None,
    response_detail: str | None = None,
) -> None:
    _ensure_outbound_req_log_handler()
    body_s = json.dumps(_redact_for_log(body), ensure_ascii=False)
    if len(body_s) > 4096:
        body_s = body_s[:4096] + "…"
    parts = [
        f"step={step}",
        "POST",
        url,
        f"{elapsed_ms:.2f}ms",
    ]
    if status is not None:
        parts.append(f"status={status}")
    if error_kind:
        parts.append(f"error={error_kind}")
    parts.append(f"body={body_s}")
    if response_detail:
        parts.append(f"response={response_detail}")
    if error_detail:
        parts.append(f"detail={error_detail}")
    _outbound_req_logger.info(" | ".join(parts))
    _flush_outbound_log_handlers()


def _post_json(
    url: str,
    body: dict,
    *,
    step: str,
    timeout: float = 5.0,
) -> dict:
    data = json.dumps(body).encode("utf-8")
    hdr = {
        "Content-Type": "application/json",
        TRACE_ID_HEADER: get_trace_id_for_outbound_http(),
    }
    req = Request(
        url,
        data=data,
        method="POST",
        headers=hdr,
    )
    t0 = time.perf_counter()
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read()
            status = getattr(resp, "status", None)
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            _log_outbound_post(
                step=step,
                url=url,
                body=body,
                elapsed_ms=elapsed_ms,
                status=status,
                error_kind="decode",
                error_detail=str(e),
            )
            raise RuntimeError(f"[{step}] 响应体不是合法 UTF-8: {url}") from e
        elapsed_ms = (time.perf_counter() - t0) * 1000
        try:
            parsed: Any = json.loads(raw) if raw else {}
        except json.JSONDecodeError as e:
            _log_outbound_post(
                step=step,
                url=url,
                body=body,
                elapsed_ms=elapsed_ms,
                status=status,
                error_kind="json",
                error_detail=_redact_error_detail_snippet(raw),
            )
            raise RuntimeError(f"[{step}] 响应不是合法 JSON {url}: {raw[:500]!r}") from e
        _log_outbound_post(
            step=step,
            url=url,
            body=body,
            elapsed_ms=elapsed_ms,
            status=status,
            error_kind=None,
            response_detail=_response_body_snippet_for_log(parsed),
        )
    except TimeoutError as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        _log_outbound_post(
            step=step,
            url=url,
            body=body,
            elapsed_ms=elapsed_ms,
            status=None,
            error_kind="timeout",
        )
        raise RuntimeError(f"[{step}] 请求超时（{timeout:g}s）: {url}") from e
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        _log_outbound_post(
            step=step,
            url=url,
            body=body,
            elapsed_ms=elapsed_ms,
            status=e.code,
            error_kind="http",
            error_detail=_redact_error_detail_snippet(err_body),
        )
        raise RuntimeError(f"[{step}] 请求失败 HTTP {e.code} {url}: {err_body}") from e
    except URLError as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        _log_outbound_post(
            step=step,
            url=url,
            body=body,
            elapsed_ms=elapsed_ms,
            status=None,
            error_kind="url",
            error_detail=str(e),
        )
        raise RuntimeError(f"[{step}] 请求失败 {url}: {e}") from e
    return parsed if raw else {}


def _bootstrap_git_clone_timeout_sec() -> int | None:
    raw = os.environ.get("TASK_API_BOOTSTRAP_GIT_TIMEOUT_SEC", "").strip()
    if not raw:
        return None
    try:
        return max(10, int(float(raw)))
    except ValueError:
        log.warning(
            "忽略无效的 TASK_API_BOOTSTRAP_GIT_TIMEOUT_SEC=%r，使用 git_clone 默认超时",
            raw,
        )
        return None


def _extract_git_repo_urls(task_detail: dict) -> list[str]:
    """从 task-detail 响应收集待克隆地址，兼容 `git_repo` 与 `git_repos[]` 结构。"""
    out: list[str] = []
    seen: set[str] = set()

    def _add(raw: str | None) -> None:
        if not raw:
            return
        u = raw.strip()
        if not u or u in seen:
            return
        seen.add(u)
        out.append(u)

    def _collect_repo_list(value: Any) -> None:
        if isinstance(value, str):
            _add(value)
            return
        if isinstance(value, list):
            for item in value:
                _collect_repo_list(item)
            return
        if not isinstance(value, dict):
            return
        _add(value.get("git_repo") or value.get("url") or value.get("repo_url"))
        nested = value.get("git_repos")
        if nested is not None:
            _collect_repo_list(nested)

    # 任务详情主结构：project_repos[].git_repos[]（多项目多仓库）。
    _collect_repo_list(task_detail.get("project_repos"))
    # 兼容直接平铺到根节点的 git_repos。
    _collect_repo_list(task_detail.get("git_repos"))

    task_obj = task_detail.get("task")
    if isinstance(task_obj, dict):
        # 兼容 task.git_repos。
        _collect_repo_list(task_obj.get("git_repos"))
        params = task_obj.get("parameters")
        if isinstance(params, dict):
            for key in ("git_repos", "project_urls", "project_repos", "repos", "repositories"):
                _collect_repo_list(params.get(key))
    return out


_GIT_PHASE_PCT = re.compile(
    r"(?:Receiving objects|Resolving deltas|Compressing objects|Unpacking objects|Counting objects):\s*(\d+)%",
    re.IGNORECASE,
)


def _max_git_phase_percent(text: str) -> int | None:
    nums = [int(x) for x in _GIT_PHASE_PCT.findall(text)]
    return max(nums) if nums else None


def _overall_clone_percent(repo_index: int, repo_total: int, phase_pct: int) -> int:
    if repo_total <= 0:
        return min(99, phase_pct)
    return min(
        99,
        int(((repo_index - 1) + phase_pct / 100.0) / repo_total * 100),
    )


async def _post_git_clone_progress_saas(
    cloud_prefix: str,
    access_token: str,
    progress: int,
    message: str,
) -> None:
    url = f"{cloud_prefix.rstrip('/')}/server-container-token/git-clone-progress/"
    body = {
        "access_token": access_token,
        "progress": max(0, min(100, progress)),
        "message": message,
    }
    try:
        await asyncio.to_thread(
            _post_json,
            url,
            body,
            step="git-clone-progress",
            timeout=8.0,
        )
    except Exception as e:
        log.warning("git-clone-progress 上报 SaaS 失败: %s", e)


_EXEC_LOG_VIA_CLONE_PROGRESS_MAX = 3500


async def notify_container_execution_log(message: str, *, progress: int = 100) -> None:
    """将文本推送到任务云，经 SSE 进入任务详情执行日志（与 ``git-clone-progress`` 克隆阶段同一通道）。

    未配置 ``TaskApiEndPoint`` / ``ACCESS_TOKEN`` 时静默跳过，不影响本地 API。
    """
    try:
        prefix = _task_api_prefix()
    except Exception:
        return
    token = (os.environ.get("ACCESS_TOKEN") or "").strip()
    if not prefix or not token:
        return
    msg = (message or "").strip()
    if len(msg) > _EXEC_LOG_VIA_CLONE_PROGRESS_MAX:
        msg = msg[:_EXEC_LOG_VIA_CLONE_PROGRESS_MAX] + "…"
    await _post_git_clone_progress_saas(prefix, token, progress, msg)


async def _maybe_report_clone_progress(
    *,
    cloud_prefix: str,
    access_token: str,
    repo_index: int,
    repo_total: int,
    repo_url: str,
    stderr_text: str,
    last_sent: list,
) -> None:
    phase = _max_git_phase_percent(stderr_text)
    if phase is None:
        return
    overall = _overall_clone_percent(repo_index, repo_total, phase)
    now = asyncio.get_running_loop().time()
    t_prev, p_prev = last_sent[0], last_sent[1]
    if overall < p_prev:
        return
    if overall == p_prev and (now - t_prev) < 0.4:
        return
    if overall > p_prev and (now - t_prev) < 0.22 and (overall - p_prev) < 3:
        return
    last_sent[0] = now
    last_sent[1] = overall
    short = repo_url[:72] + ("…" if len(repo_url) > 72 else "")
    msg = f"容器克隆 ({repo_index}/{repo_total}) 阶段 {phase}% · {short}"
    await _post_git_clone_progress_saas(cloud_prefix, access_token, overall, msg)


async def _kill_bootstrap_git_proc(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(OSError):
        await proc.wait()


async def _run_git_clone_repo_streaming(
    *,
    cmd: list[str],
    env: dict,
    layer_id: str,
    timeout_sec: int | None,
    cloud_prefix: str,
    access_token: str,
    repo_index: int,
    repo_total: int,
    repo_url: str,
) -> int:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    err_parts: list[str] = []
    out_parts: list[str] = []
    last_sent = [0.0, -1]

    async def pump_stream(stream, parts: list[str], is_stderr: bool) -> None:
        assert stream is not None
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            t = chunk.decode(errors="replace")
            parts.append(t)
            await append_clone_layer_log(layer_id, t)
            if is_stderr:
                await _maybe_report_clone_progress(
                    cloud_prefix=cloud_prefix,
                    access_token=access_token,
                    repo_index=repo_index,
                    repo_total=repo_total,
                    repo_url=repo_url,
                    stderr_text="".join(err_parts),
                    last_sent=last_sent,
                )

    assert proc.stdout is not None and proc.stderr is not None
    try:
        if timeout_sec is not None:
            await asyncio.wait_for(
                asyncio.gather(
                    pump_stream(proc.stdout, out_parts, False),
                    pump_stream(proc.stderr, err_parts, True),
                ),
                timeout=float(timeout_sec),
            )
        else:
            await asyncio.gather(
                pump_stream(proc.stdout, out_parts, False),
                pump_stream(proc.stderr, err_parts, True),
            )
    except asyncio.TimeoutError:
        await _kill_bootstrap_git_proc(proc)
        raise TimeoutError from None

    return await proc.wait()


def _repo_dir_name_from_url(url: str) -> str:
    parsed = urlsplit(url)
    base = (parsed.path.rsplit("/", 1)[-1] or "").strip()
    if base.endswith(".git"):
        base = base[:-4]
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-._")
    if not base:
        base = "repo"
    return base


def _bootstrap_write_ssh_keyfile(private_key: str) -> str:
    fd, path = tempfile.mkstemp(prefix="bootstrap_git_ssh_", suffix=".key", text=True)
    try:
        key_content = private_key.strip()
        if not key_content.endswith("\n"):
            key_content += "\n"
        os.write(fd, key_content.encode("utf-8"))
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return path


async def _clone_repos_into_shared_layer(
    urls: list[str],
    *,
    cloud_prefix: str,
    access_token: str,
    task_detail: dict[str, Any] | None = None,
) -> str:
    """多个仓库克隆到同一个新建层，不同仓库放在该层不同子目录。

    将 git 输出写入与 UI 克隆相同的内存缓冲，便于页面加载后通过
    ``GET /api/repos/bootstrap-clone-log`` 展示；同时将解析出的百分比经 SaaS SSE 推到任务详情评论区。
    """
    if shutil.which("git") is None:
        raise RuntimeError("git executable not found on PATH")

    tout = _bootstrap_git_clone_timeout_sec()
    layer_id = new_layer_id()
    layer_path = create_root_layer(layer_id)
    await clear_clone_layer_log(layer_id)
    await append_clone_layer_log(
        layer_id,
        "【容器启动引导】正在克隆任务关联仓库…\n\n",
    )
    n = len(urls)
    await _post_git_clone_progress_saas(
        cloud_prefix,
        access_token,
        0,
        "【容器启动引导】开始克隆任务关联仓库…",
    )
    log.info("bootstrap 创建共享层 layer_id=%s path=%s", layer_id, layer_path)
    cred_root: dict[str, Any] = {}
    if isinstance(task_detail, dict):
        raw_cred = task_detail.get("repo_clone_credentials")
        if isinstance(raw_cred, dict):
            cred_root = raw_cred
    for i, raw in enumerate(urls, start=1):
        u = raw.strip()
        if not u:
            continue
        repo_dir = layer_path / _repo_dir_name_from_url(u)
        # 子目录重名时自动追加序号，避免覆盖已克隆仓库。
        if repo_dir.exists():
            suffix = 2
            candidate = repo_dir
            while candidate.exists():
                candidate = layer_path / f"{repo_dir.name}_{suffix}"
                suffix += 1
            repo_dir = candidate
        log.info(
            "bootstrap 克隆到共享层 (%d/%d): %s -> %s",
            i,
            n,
            u[:512],
            repo_dir.name,
        )
        await append_clone_layer_log(
            layer_id,
            f"━━ ({i}/{n}) {u}\n→ {repo_dir.name}\n",
        )
        cred = cred_root.get(u) if isinstance(cred_root.get(u), dict) else None
        if cred is None:
            alt = cred_root.get(u.rstrip("/"))
            cred = alt if isinstance(alt, dict) else None
        pem = (
            str(cred.get("ephemeral_ssh_private_key", "")).strip() if isinstance(cred, dict) else ""
        )
        clone_remote = _git_clone_remote_for_ssh_pem(u) if pem else u
        if pem and clone_remote != u:
            await append_clone_layer_log(
                layer_id,
                "[bootstrap-clone] 已配置 SSH 私钥，HTTPS 登记地址将改用 SSH 传输克隆：\n"
                f"    {clone_remote}\n",
            )
        max_attempts = _max_clone_attempts()
        for attempt in range(max_attempts):
            if repo_dir.exists():
                shutil.rmtree(repo_dir, ignore_errors=True)
            ipv4_curl_cfg = None
            try:
                ipv4_curl_cfg = _make_ipv4_curl_config_file()
                ssh_keyfile: str | None = None
                if pem:
                    ssh_keyfile = _bootstrap_write_ssh_keyfile(pem)
                git_env = _clone_subprocess_env()
                try:
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
                    try:
                        code = await _run_git_clone_repo_streaming(
                            cmd=cmd,
                            env=git_env,
                            layer_id=layer_id,
                            timeout_sec=tout,
                            cloud_prefix=cloud_prefix,
                            access_token=access_token,
                            repo_index=i,
                            repo_total=n,
                            repo_url=u,
                        )
                    except TimeoutError:
                        await append_clone_layer_log(
                            layer_id,
                            f"\n[bootstrap-clone 超时] url={u!r} timeout={tout}s "
                            f"(尝试 {attempt + 1}/{max_attempts})\n",
                        )
                        if attempt < max_attempts - 1:
                            await append_clone_layer_log(
                                layer_id,
                                f"[bootstrap-clone] 将重试 ({attempt + 2}/{max_attempts}) …\n",
                            )
                            await _sleep_before_retry(attempt)
                            continue
                        raise RuntimeError(
                            f"[bootstrap-clone] git clone 超时 url={u} timeout={tout}s"
                        ) from None
                    if code == 0:
                        try:
                            head_sha = await asyncio.to_thread(
                                verify_git_clone_workspace,
                                repo_dir,
                                env=git_env,
                            )
                        except RuntimeError as ver_e:
                            await append_clone_layer_log(
                                layer_id,
                                f"\n[bootstrap-clone 校验失败] {ver_e}\n",
                            )
                            if attempt < max_attempts - 1:
                                log.warning(
                                    "bootstrap-clone 校验失败将重试 (%d/%d): %s",
                                    attempt + 1,
                                    max_attempts,
                                    ver_e,
                                )
                                shutil.rmtree(repo_dir, ignore_errors=True)
                                await _sleep_before_retry(attempt)
                                continue
                            raise RuntimeError(
                                f"[bootstrap-clone] 克隆后校验失败 url={u} "
                                f"layer_id={layer_id} dir={repo_dir.name}: {ver_e}"
                            ) from ver_e
                        if head_sha:
                            await append_clone_layer_log(
                                layer_id,
                                f"[校验] 拉取验证通过 HEAD={head_sha[:12]}\n",
                            )
                        break
                    full = await get_clone_layer_log_text(layer_id)
                    if attempt < max_attempts - 1 and _looks_like_transient_fetch_error(full):
                        await append_clone_layer_log(
                            layer_id,
                            f"\n[bootstrap-clone] 网络/TLS 瞬时错误，"
                            f"{attempt + 2}/{max_attempts} 次重试 …\n",
                        )
                        log.warning(
                            "bootstrap-clone 将重试 (%d/%d) url=%s exit=%s",
                            attempt + 1,
                            max_attempts,
                            u[:256],
                            code,
                        )
                        await _sleep_before_retry(attempt)
                        continue
                    tail = full[-2000:] if len(full) > 2000 else full
                    raise RuntimeError(
                        f"[bootstrap-clone] git clone 失败 exit={code} url={u} "
                        f"layer_id={layer_id} dir={repo_dir.name} output={tail}"
                    )
                finally:
                    if ssh_keyfile:
                        with contextlib.suppress(OSError):
                            os.unlink(ssh_keyfile)
            finally:
                if ipv4_curl_cfg is not None:
                    with contextlib.suppress(OSError):
                        ipv4_curl_cfg.unlink(missing_ok=True)
        log.info(
            "bootstrap 克隆完成 layer_id=%s dir=%s",
            layer_id,
            repo_dir.name,
        )
    await append_clone_layer_log(layer_id, "\n【容器启动引导】克隆完成。\n")
    await _post_git_clone_progress_saas(
        cloud_prefix,
        access_token,
        100,
        "【容器启动引导】仓库克隆已完成",
    )
    return layer_id


def _clone_projects_via_shared_layer(
    repo_urls: list[str],
    *,
    cloud_prefix: str,
    access_token: str,
    task_detail: dict[str, Any] | None = None,
) -> str | None:
    if not repo_urls:
        log.info("task-detail 中未提供项目地址，跳过克隆")
        return None
    return asyncio.run(
        _clone_repos_into_shared_layer(
            repo_urls,
            cloud_prefix=cloud_prefix,
            access_token=access_token,
            task_detail=task_detail,
        )
    )


def bootstrap_container_config() -> None:
    """与 machine_container.md 一致：exchange-refresh → refresh-access → task-detail → feature-params-yaml。"""
    global bootstrap_clone_layer_id

    prefix = _task_api_prefix()
    if not prefix:
        return

    bootstrap_clone_layer_id = None
    timeout = _bootstrap_http_timeout_sec()
    business_api_endpoint = _business_api_endpoint()

    initial = os.environ.get("ACCESS_TOKEN", "").strip()
    if not initial:
        raise RuntimeError("已配置任务云 API 路径但 ACCESS_TOKEN 为空，无法进行 exchange-refresh")

    if _skip_exchange_for_local_business_api(business_api_endpoint):
        log.info(
            "BusinessApiEndPoint=%s 指向本机开发端口，跳过 exchange-refresh / refresh-access",
            business_api_endpoint,
        )
        new_access = initial
    else:
        ex = _post_json(
            f"{prefix}/server-container-token/exchange-refresh/",
            {
                "access_token": initial,
                "business_api_endpoint": business_api_endpoint,
            },
            step="exchange-refresh",
            timeout=timeout,
        )
        refresh_token = ex.get("refresh_token")
        if not refresh_token:
            raise RuntimeError(f"exchange-refresh 响应缺少 refresh_token: {ex!r}")

        ref = _post_json(
            f"{prefix}/server-container-token/refresh-access/",
            {"refresh_token": refresh_token},
            step="refresh-access",
            timeout=timeout,
        )
        new_access_raw = ref.get("access_token")
        if not isinstance(new_access_raw, str) or not new_access_raw:
            raise RuntimeError(f"refresh-access 响应缺少 access_token: {ref!r}")
        new_access = new_access_raw

        os.environ["ACCESS_TOKEN"] = new_access

    detail = _post_json(
        f"{prefix}/server-container-token/task-detail/",
        {"access_token": new_access},
        step="task-detail",
        timeout=timeout,
    )
    repo_urls = _extract_git_repo_urls(detail)
    bootstrap_clone_layer_id = _clone_projects_via_shared_layer(
        repo_urls,
        cloud_prefix=prefix,
        access_token=new_access,
        task_detail=detail if isinstance(detail, dict) else None,
    )
    if bootstrap_clone_layer_id:
        try:
            from .online_project_view import set_online_project_tip

            set_online_project_tip(bootstrap_clone_layer_id)
        except Exception:
            log.exception(
                "bootstrap: 无法将 onlineProject 指向克隆层 %s",
                bootstrap_clone_layer_id,
            )

    y = _post_json(
        f"{prefix}/server-container-token/feature-params-yaml/",
        {"access_token": new_access},
        step="feature-params-yaml",
        timeout=timeout,
    )
    yaml_text = y.get("yaml")
    if yaml_text is None:
        raise RuntimeError(f"feature-params-yaml 响应缺少 yaml 字段: {y!r}")

    yaml.safe_load(yaml_text)

    dest = config_file_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(yaml_text, encoding="utf-8")
    log.info("已从任务云拉取功能参数 YAML 并写入 %s", dest)
