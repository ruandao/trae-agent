"""任务容器启动时：用预埋 AccessToken 换 RefreshToken，再换新 Access，并拉取 feature-params YAML。"""

from __future__ import annotations

import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from .paths import config_file_path

log = logging.getLogger(__name__)


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


def _post_json(url: str, body: dict, timeout: float = 120.0) -> dict:
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
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"请求失败 HTTP {e.code} {url}: {err_body}") from e
    except URLError as e:
        raise RuntimeError(f"请求失败 {url}: {e}") from e
    return json.loads(raw) if raw else {}


def bootstrap_container_config() -> None:
    """与 machine_container.md 一致：exchange-refresh → refresh-access → feature-params-yaml。"""
    prefix = _task_api_prefix()
    if not prefix:
        return

    initial = os.environ.get("ACCESS_TOKEN", "").strip()
    if not initial:
        raise RuntimeError(
            "已配置任务云 API 路径但 ACCESS_TOKEN 为空，无法进行 exchange-refresh"
        )

    ex = _post_json(f"{prefix}/server-container-token/exchange-refresh/", {"access_token": initial})
    refresh_token = ex.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(f"exchange-refresh 响应缺少 refresh_token: {ex!r}")

    ref = _post_json(f"{prefix}/server-container-token/refresh-access/", {"refresh_token": refresh_token})
    new_access = ref.get("access_token")
    if not new_access:
        raise RuntimeError(f"refresh-access 响应缺少 access_token: {ref!r}")

    os.environ["ACCESS_TOKEN"] = new_access

    y = _post_json(
        f"{prefix}/server-container-token/feature-params-yaml/",
        {"access_token": new_access},
    )
    yaml_text = y.get("yaml")
    if yaml_text is None:
        raise RuntimeError(f"feature-params-yaml 响应缺少 yaml 字段: {y!r}")

    yaml.safe_load(yaml_text)

    dest = config_file_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(yaml_text, encoding="utf-8")
    log.info("已从任务云拉取功能参数 YAML 并写入 %s", dest)
