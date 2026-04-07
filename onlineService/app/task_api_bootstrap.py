"""任务容器启动时：换票、拉取任务详情（project_repos）、克隆仓库、再拉取 feature-params YAML。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from .paths import config_file_path, runtime_dir

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


def _bootstrap_git_timeout_sec() -> float:
    raw = os.environ.get("TASK_API_BOOTSTRAP_GIT_TIMEOUT_SEC", "").strip()
    if raw:
        try:
            return max(10.0, float(raw))
        except ValueError:
            log.warning(
                "忽略无效的 TASK_API_BOOTSTRAP_GIT_TIMEOUT_SEC=%r，使用默认 1800s",
                raw,
            )
    return 1800.0


def _bootstrap_projects_root() -> Path:
    raw = os.environ.get("TASK_PROJECTS_CLONE_DIR", "").strip()
    if raw:
        p = Path(raw).expanduser().resolve()
    else:
        p = (runtime_dir() / "task_projects").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


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


def _is_supported_git_url(url: str) -> bool:
    if url.startswith("git@") and ":" in url:
        return True
    parsed = urlsplit(url)
    return parsed.scheme in {"http", "https", "ssh", "git"} and bool(parsed.netloc)


def _repo_name_from_url(url: str) -> str:
    if url.startswith("git@"):
        path_part = url.split(":", 1)[-1]
    else:
        path_part = urlsplit(url).path
    name = path_part.rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name or "").strip("-._")
    return name or "repo"


def _clone_projects_from_urls(project_urls: list[str]) -> None:
    if not project_urls:
        log.info("task-detail 中未提供项目地址列表，跳过克隆")
        return
    if shutil.which("git") is None:
        raise RuntimeError("系统未安装 git，无法克隆项目地址列表")

    root = _bootstrap_projects_root()
    timeout = _bootstrap_git_timeout_sec()
    cloned = 0
    updated = 0
    skipped = 0

    for idx, url in enumerate(project_urls, start=1):
        u = url.strip()
        if not _is_supported_git_url(u):
            log.warning("跳过不支持的项目地址: %s", u)
            skipped += 1
            continue

        suffix = hashlib.sha1(u.encode("utf-8")).hexdigest()[:8]
        dest = root / f"{idx:02d}-{_repo_name_from_url(u)}-{suffix}"
        git_dir = dest / ".git"
        if git_dir.is_dir():
            cmd = ["git", "-C", str(dest), "fetch", "--all", "--prune"]
            step = "git-fetch"
            updated += 1
        elif dest.exists() and any(dest.iterdir()):
            log.warning("目标目录非空且不是 Git 仓库，跳过: %s", dest)
            skipped += 1
            continue
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            cmd = ["git", "clone", "--progress", u, str(dest)]
            step = "git-clone"
            cloned += 1

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            out = (proc.stdout or "") + (proc.stderr or "")
            short = out[-2000:] if len(out) > 2000 else out
            raise RuntimeError(
                f"[{step}] 执行失败（exit={proc.returncode}） url={u} dest={dest}: {short}"
            )

    log.info(
        "任务项目克隆完成：总数=%d，新增克隆=%d，已存在更新=%d，跳过=%d，目录=%s",
        len(project_urls),
        cloned,
        updated,
        skipped,
        root,
    )


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
    _clone_projects_from_urls(repo_urls)

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
