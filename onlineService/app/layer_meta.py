"""可写层元数据（Overlay 差分层与父指针）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .paths import layers_root

META_NAME = "layer_meta.json"
_LAYER_ID_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})_(?P<suf>[0-9a-fA-F]+)$")


def _layer_root(layer_id: str) -> Path:
    return layers_root() / layer_id


@dataclass(frozen=True)
class LayerMeta:
    version: int
    kind: Literal["clone", "job"]
    parent_layer_id: str | None


def meta_path(layer_id: str) -> Path:
    return _layer_root(layer_id) / META_NAME


def read_layer_meta(layer_id: str) -> LayerMeta | None:
    p = meta_path(layer_id)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        v = int(raw.get("version", 1))
        kind = raw.get("kind")
        if kind not in ("clone", "job"):
            return None
        pl = raw.get("parent_layer_id")
        parent = str(pl).strip() if pl else None
        if parent and not _LAYER_ID_RE.match(parent):
            parent = None
        return LayerMeta(version=v, kind=kind, parent_layer_id=parent)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def write_layer_meta(
    layer_id: str, *, kind: Literal["clone", "job"], parent_layer_id: str | None
) -> None:
    root = _layer_root(layer_id)
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "kind": kind,
        "parent_layer_id": parent_layer_id,
    }
    meta_path(layer_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def layer_chain_root_to_tip(layer_id: str) -> list[str]:
    """自 tip 沿父指针走到 clone，返回 [clone_id, …, tip_id]。"""
    chain_tip_to_root: list[str] = []
    cur: str | None = layer_id
    guard = 0
    while cur and _LAYER_ID_RE.match(cur) and guard < 10_000:
        guard += 1
        meta = read_layer_meta(cur)
        if meta is None:
            break
        chain_tip_to_root.append(cur)
        if meta.kind == "clone":
            break
        cur = meta.parent_layer_id
    return list(reversed(chain_tip_to_root))


def is_overlay_v1_layer(layer_id: str) -> bool:
    return read_layer_meta(layer_id) is not None
