"""任务云引导 URL 与 task-detail 中 git 仓库列表解析（供容器 bootstrap 与单测）。"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)


def rewrite_host_docker_internal_url(url: str) -> str:
    """
    将 URL 中的 host.docker.internal 换为数值 IP（仅当显式设置 DOCKER_HOST_GATEWAY_IP 时）。

    未设置时保留主机名，以便 HTTP Host 与 Django ALLOWED_HOSTS 中的 host.docker.internal 一致。
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


def git_clone_remote_for_ssh_pem(canonical_url: str) -> str:
    """使用 SSH 私钥引导克隆时，将常见 ``https://`` 远程转为 ``git@host:path.git``。"""
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


def extract_git_repo_urls(task_detail: dict) -> list[str]:
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

    _collect_repo_list(task_detail.get("project_repos"))
    _collect_repo_list(task_detail.get("git_repos"))

    task_obj = task_detail.get("task")
    if isinstance(task_obj, dict):
        _collect_repo_list(task_obj.get("git_repos"))
        params = task_obj.get("parameters")
        if isinstance(params, dict):
            for key in ("git_repos", "project_urls", "project_repos", "repos", "repositories"):
                _collect_repo_list(params.get(key))
    return out
