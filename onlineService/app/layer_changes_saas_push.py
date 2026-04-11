"""容器启动后周期性将层文件变动摘要 POST 到 SaaS，经 SSE 推送到任务详情执行日志。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any

from . import task_api_bootstrap
from .layer_git import list_layer_changes_vs_parent
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


def _fingerprint(obj: dict[str, Any]) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return json.dumps({"err": "non_serializable"}, sort_keys=True)


def _normalize_change_row(one: Any) -> dict[str, str] | None:
    if not isinstance(one, dict):
        return None
    path = str(one.get("path") or "").strip()
    if not path:
        return None
    kind = str(one.get("kind") or "").strip() or "modified"
    return {"path": path, "kind": kind}


async def _build_layer_changes_payloads(build_snapshot: Any) -> list[dict[str, Any]]:
    snap = await build_snapshot()
    if not isinstance(snap, dict):
        return []
    layers = snap.get("layers")
    jobs = snap.get("jobs")
    if not isinstance(layers, list):
        return []
    jobs_list = jobs if isinstance(jobs, list) else []
    rows: list[dict[str, Any]] = []
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        layer_id = str(layer.get("layer_id") or "").strip()
        parent_layer_id = str(layer.get("parent_layer_id") or "").strip()
        if not layer_id or not parent_layer_id:
            continue
        try:
            diff_body = await asyncio.to_thread(
                list_layer_changes_vs_parent, parent_layer_id, layer_id
            )
        except Exception:
            log.debug("layer-changes snapshot build failed layer_id=%s", layer_id, exc_info=True)
            continue
        changes_raw = diff_body.get("changes") if isinstance(diff_body, dict) else []
        normalized_changes = []
        if isinstance(changes_raw, list):
            for one in changes_raw:
                row = _normalize_change_row(one)
                if row is not None:
                    normalized_changes.append(row)
        related_job_ids = []
        for j in jobs_list:
            if not isinstance(j, dict):
                continue
            if str(j.get("layer_id") or "").strip() != layer_id:
                continue
            jid = str(j.get("id") or "").strip()
            if jid:
                related_job_ids.append(jid)
        rows.append(
            {
                "layer_id": layer_id,
                "parent_layer_id": parent_layer_id,
                "same": bool(diff_body.get("same")) if isinstance(diff_body, dict) else None,
                "changes": normalized_changes,
                "change_count": len(normalized_changes),
                "truncated": bool(diff_body.get("truncated"))
                if isinstance(diff_body, dict)
                else False,
                "job_ids": related_job_ids,
            }
        )
    return rows


async def push_layer_changes_if_changed(*, build_snapshot: Any, last_sent: dict[str, str]) -> None:
    prefix = _cloud_prefix_for_push()
    token = _access_token_for_push()
    if not prefix or not token:
        return

    payloads = await _build_layer_changes_payloads(build_snapshot)
    active_layer_ids = {str(one.get("layer_id")) for one in payloads if one.get("layer_id")}
    for stale_layer_id in [lid for lid in list(last_sent.keys()) if lid not in active_layer_ids]:
        last_sent.pop(stale_layer_id, None)

    for one in payloads:
        layer_id = str(one.get("layer_id") or "").strip()
        if not layer_id:
            continue
        fp = _fingerprint(one)
        if last_sent.get(layer_id) == fp:
            continue
        last_sent[layer_id] = fp
        url = f"{prefix.rstrip('/')}/server-container-token/layer-changes-push/"
        body = {
            "access_token": token,
            "layer_id": layer_id,
            "parent_layer_id": one.get("parent_layer_id"),
            "same": one.get("same"),
            "changes": one.get("changes") or [],
            "change_count": int(one.get("change_count") or 0),
            "truncated": bool(one.get("truncated")),
            "job_ids": one.get("job_ids") or [],
        }
        try:
            await asyncio.to_thread(
                _post_json,
                url,
                body,
                step="layer-changes-push",
                timeout=12.0,
            )
        except Exception as e:
            log.warning("layer-changes-push 上报 SaaS 失败 layer_id=%s: %s", layer_id, e)


async def run_layer_changes_saas_push_loop(build_snapshot: Any) -> None:
    """后台任务：默认每 4s 扫描层变动并按层去重后上报。"""
    last_sent: dict[str, str] = {}
    interval = 4.0
    raw = (os.environ.get("LAYER_CHANGES_SAAS_PUSH_INTERVAL_SEC") or "").strip()
    if raw:
        with contextlib.suppress(ValueError):
            interval = max(2.0, float(raw))
    while True:
        await asyncio.sleep(interval)
        await push_layer_changes_if_changed(
            build_snapshot=build_snapshot,
            last_sent=last_sent,
        )
