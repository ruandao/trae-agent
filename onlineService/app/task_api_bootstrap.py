"""任务容器启动时：换票、拉取任务详情（project_repos）、克隆到同一个新建层、再拉取 YAML。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from .git_clone import (
    _clone_subprocess_env,
    _git_config_prefix,
    append_clone_layer_log,
    clear_clone_layer_log,
    get_clone_layer_log_text,
)
from .layers import create_root_layer, new_layer_id
from .paths import config_file_path

log = logging.getLogger(__name__)

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


def _task_api_prefix() -> str | None:
    endpoint = os.environ.get("TaskApiEndPoint", "").strip()
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
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(
            "BusinessApiEndPoint/BUSINESS_API_ENDPOINT 必须是合法的 http(s) URL"
        )
    return raw.rstrip("/")


def _post_json(
    url: str,
    body: dict,
    *,
    step: str,
    timeout: float = 5.0,
) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except TimeoutError as e:
        raise RuntimeError(
            f"[{step}] 请求超时（{timeout:g}s）: {url}"
        ) from e
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"[{step}] 请求失败 HTTP {e.code} {url}: {err_body}"
        ) from e
    except URLError as e:
        raise RuntimeError(f"[{step}] 请求失败 {url}: {e}") from e
    return json.loads(raw) if raw else {}


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
    """从 task-detail 响应收集待克隆地址：优先 `project_repos[].git_repo`，并兼容 `task.parameters` 中的列表。"""
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

    for item in task_detail.get("project_repos") or []:
        if isinstance(item, dict):
            _add(item.get("git_repo") or item.get("url") or item.get("repo_url"))
    task_obj = task_detail.get("task")
    if isinstance(task_obj, dict):
        params = task_obj.get("parameters")
        if isinstance(params, dict):
            for key in ("project_urls", "project_repos", "repos", "repositories"):
                val = params.get(key)
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, str):
                            _add(item)
                        elif isinstance(item, dict):
                            _add(
                                item.get("git_repo")
                                or item.get("url")
                                or item.get("repo_url")
                            )
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
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await proc.wait()
    except OSError:
        pass


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


async def _clone_repos_into_shared_layer(
    urls: list[str],
    *,
    cloud_prefix: str,
    access_token: str,
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
    git_env = _clone_subprocess_env()
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
        cmd = list(_git_config_prefix())
        cmd.extend(["clone", "--progress", u, str(repo_dir)])
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
                f"\n[bootstrap-clone 超时] url={u!r} timeout={tout}s\n",
            )
            raise RuntimeError(
                f"[bootstrap-clone] git clone 超时 url={u} timeout={tout}s"
            ) from None
        if code != 0:
            full = await get_clone_layer_log_text(layer_id)
            tail = full[-2000:] if len(full) > 2000 else full
            raise RuntimeError(
                f"[bootstrap-clone] git clone 失败 exit={code} url={u} "
                f"layer_id={layer_id} dir={repo_dir.name} output={tail}"
            )
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
) -> str | None:
    if not repo_urls:
        log.info("task-detail 中未提供项目地址，跳过克隆")
        return None
    return asyncio.run(
        _clone_repos_into_shared_layer(
            repo_urls,
            cloud_prefix=cloud_prefix,
            access_token=access_token,
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
        raise RuntimeError(
            "已配置任务云 API 路径但 ACCESS_TOKEN 为空，无法进行 exchange-refresh"
        )

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
    new_access = ref.get("access_token")
    if not new_access:
        raise RuntimeError(f"refresh-access 响应缺少 access_token: {ref!r}")

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
