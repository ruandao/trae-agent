"""容器启动后周期性将可写层 / 任务层级快照 POST 到 SaaS，经 SSE 推送到任务详情评论区（zTree）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from . import task_api_bootstrap
from .task_api_bootstrap import _post_json

log = logging.getLogger(__name__)


def _access_token_for_push() -> str | None:
    raw = (os.environ.get("ACCESS_TOKEN") or "").strip()
    return raw or None


def _cloud_prefix_for_push() -> str | None:
    try:
        return task_api_bootstrap._task_api_prefix()
    except Exception:
        return None


def _snapshot_fingerprint(payload: dict[str, Any]) -> str:
    """稳定序列化用于去重，避免无变化时重复 POST。"""
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return json.dumps({"err": "non_serializable"}, sort_keys=True)


async def push_layer_graph_snapshot_if_changed(
    *,
    build_snapshot: Any,
    last_sent: list,
) -> None:
    """
    last_sent: 单元素列表 [str|None]，保存上一帧 fingerprint。
    build_snapshot: async callable () -> dict 须含 layers, jobs, layers_root, bootstrap_layer_id
    """
    prefix = _cloud_prefix_for_push()
    token = _access_token_for_push()
    if not prefix or not token:
        return
    try:
        snap = await build_snapshot()
    except Exception:
        log.debug("layer-graph snapshot build failed", exc_info=True)
        return
    if not isinstance(snap, dict):
        return
    fp = _snapshot_fingerprint(snap)
    if last_sent and last_sent[0] == fp:
        return
    last_sent.clear()
    last_sent.append(fp)

    url = f"{prefix.rstrip('/')}/server-container-token/layer-graph-push/"
    body: dict[str, Any] = {
        "access_token": token,
        "layers": snap.get("layers") or [],
        "jobs": snap.get("jobs") or [],
    }
    lr = snap.get("layers_root")
    if isinstance(lr, str) and lr.strip():
        body["layers_root"] = lr.strip()
    bs = snap.get("bootstrap_layer_id")
    if isinstance(bs, str) and bs.strip():
        body["bootstrap_layer_id"] = bs.strip()

    try:
        await asyncio.to_thread(
            _post_json,
            url,
            body,
            step="layer-graph-push",
            timeout=12.0,
        )
    except Exception as e:
        log.warning("layer-graph-push 上报 SaaS 失败: %s", e)


async def run_layer_graph_saas_push_loop(build_snapshot: Any) -> None:
    """后台任务：默认每 4s 尝试推送一次（内容变化才 POST）。"""
    last_sent: list = []
    interval = 4.0
    raw = (os.environ.get("LAYER_GRAPH_SAAS_PUSH_INTERVAL_SEC") or "").strip()
    if raw:
        try:
            interval = max(2.0, float(raw))
        except ValueError:
            pass
    while True:
        await asyncio.sleep(interval)
        await push_layer_graph_snapshot_if_changed(
            build_snapshot=build_snapshot,
            last_sent=last_sent,
        )
