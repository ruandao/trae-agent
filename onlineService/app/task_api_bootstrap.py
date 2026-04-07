"""任务容器启动时：换票、拉取任务详情（project_repos）、克隆到同一个新建层、再拉取 YAML。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from .layers import create_root_layer, new_layer_id
from .paths import config_file_path

log = logging.getLogger(__name__)


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


def _repo_dir_name_from_url(url: str) -> str:
    parsed = urlsplit(url)
    base = (parsed.path.rsplit("/", 1)[-1] or "").strip()
    if base.endswith(".git"):
        base = base[:-4]
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-._")
    if not base:
        base = "repo"
    return base


async def _clone_repos_into_shared_layer(urls: list[str]) -> None:
    """多个仓库克隆到同一个新建层，不同仓库放在该层不同子目录。"""
    tout = _bootstrap_git_clone_timeout_sec()
    layer_id = new_layer_id()
    layer_path = create_root_layer(layer_id)
    n = len(urls)
    log.info("bootstrap 创建共享层 layer_id=%s path=%s", layer_id, layer_path)
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
        cmd = ["git", "clone", "--progress", u, str(repo_dir)]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=tout,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"[bootstrap-clone] git clone 超时 url={u} timeout={tout}s"
            ) from e
        if proc.returncode != 0:
            out = (proc.stdout or "") + (proc.stderr or "")
            tail = out[-2000:] if len(out) > 2000 else out
            raise RuntimeError(
                f"[bootstrap-clone] git clone 失败 exit={proc.returncode} url={u} "
                f"layer_id={layer_id} dir={repo_dir.name} output={tail}"
            )
        log.info(
            "bootstrap 克隆完成 layer_id=%s dir=%s",
            layer_id,
            repo_dir.name,
        )


def _clone_projects_via_shared_layer(repo_urls: list[str]) -> None:
    if not repo_urls:
        log.info("task-detail 中未提供项目地址，跳过克隆")
        return
    asyncio.run(_clone_repos_into_shared_layer(repo_urls))


def bootstrap_container_config() -> None:
    """与 machine_container.md 一致：exchange-refresh → refresh-access → task-detail → feature-params-yaml。"""
    prefix = _task_api_prefix()
    if not prefix:
        return

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
    _clone_projects_via_shared_layer(repo_urls)

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
