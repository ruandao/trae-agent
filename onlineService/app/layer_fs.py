"""Writable layer filesystem browser helpers.

Provides:
- list existing writable layers under ``state_root()/layers``（或环境变量 ``ONLINE_PROJECT_LAYERS``）
- list files under a layer (recursive, safe)
- read file content from a layer (text or base64 for binary)
"""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import HTTPException

from .layer_merge import layer_merged_root_for_api
from .layer_meta import is_overlay_v1_layer, read_layer_meta
from .layers import layer_path
from .paths import layers_root

_LAYER_ID_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})_(?P<suf>[0-9a-fA-F]+)$")
_SKIP_PARTS = {".git", "__pycache__", ".DS_Store"}


def _layer_browser_root(layer_id: str) -> Path:
    """文件树 API 根：Overlay 层为物化合并视图。"""
    if is_overlay_v1_layer(layer_id):
        return layer_merged_root_for_api(layer_id)
    return layer_path(layer_id)


_MAX_BYTES_DEFAULT = int(os.environ.get("LAYER_FILE_MAX_BYTES", "2000000"))  # 2MB
_MAX_BYTES_CAP = int(os.environ.get("LAYER_FILE_MAX_BYTES_CAP", "20000000"))  # 20MB
_MAX_TEXT_CHARS_DEFAULT = int(os.environ.get("LAYER_FILE_MAX_TEXT_CHARS", "200000"))
_MAX_TEXT_CHARS_CAP = int(os.environ.get("LAYER_FILE_MAX_TEXT_CHARS_CAP", "5000000"))


def _clamp_int(v: int, lo: int, hi: int) -> int:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _layer_created_at(layer_id: str) -> str | None:
    m = _LAYER_ID_RE.match(layer_id)
    if not m:
        return None
    ts = m.group("ts")
    # YYYYMMDD_HHMMSS
    dt = datetime.strptime(ts.replace("_", ""), "%Y%m%d%H%M%S")
    return dt.isoformat(timespec="seconds")


def list_layers() -> list[dict[str, Any]]:
    root = layers_root().resolve()
    if not root.is_dir():
        return []

    layers: list[dict[str, Any]] = []
    for p in root.iterdir():
        if not p.is_dir():
            continue
        layer_id = p.name
        created_at = _layer_created_at(layer_id)
        layers.append({"layer_id": layer_id, "created_at": created_at})

    # Prefer "newest first" when we can parse timestamp; otherwise fallback to name sort.
    def sort_key(x: dict[str, Any]) -> str:
        return x.get("created_at") or ""

    layers.sort(key=sort_key, reverse=True)
    return layers


def _dir_has_git_metadata(p: Path) -> bool:
    """路径下是否存在 git 元数据（``.git`` 为目录或 worktree 用的文件）。"""
    g = p / ".git"
    try:
        return g.exists() and (g.is_dir() or g.is_file())
    except OSError:
        return False


def layer_root_or_child_has_git_repo(layer_dir: Path) -> bool:
    """层目录根部或根下**直接子目录**内是否有 git 仓库。

    - UI ``POST /api/repos/clone`` 使用 ``git clone … .``，``.git`` 在层根。
    - Overlay v1 克隆层：``.git`` 在 ``<layer>/base``。
    - 容器 bootstrap（``task_api_bootstrap``）把多个仓库克隆到同一层的子目录中，
      ``.git`` 在 ``<layer>/<repo_name>/.git``，层根没有 ``.git``。
    """
    try:
        for base in (layer_dir, layer_dir / "base"):
            if not base.is_dir():
                continue
            if _dir_has_git_metadata(base):
                return True
            for child in base.iterdir():
                try:
                    if not child.is_dir() or child.name in _SKIP_PARTS:
                        continue
                except OSError:
                    continue
                if _dir_has_git_metadata(child):
                    return True
    except OSError:
        return False
    return False


def any_layer_has_git_repo() -> bool:
    """是否存在至少一个可写层，表示已成功克隆过仓库（根或子目录含 ``.git``）。"""
    for item in list_layers():
        lid = item.get("layer_id")
        if not lid:
            continue
        try:
            if layer_root_or_child_has_git_repo(layer_path(lid)):
                return True
        except OSError:
            continue
    return False


def infer_layer_parent_from_workspace(layer_id: str) -> str | None:
    """优先读 ``layer_meta.json``；否则若 ``.git`` 为指向兄弟层的符号链接则解析父层 *layer_id*。"""
    m = read_layer_meta(layer_id)
    if m and m.parent_layer_id and _LAYER_ID_RE.match(m.parent_layer_id):
        return m.parent_layer_id
    if not _LAYER_ID_RE.match(layer_id):
        return None
    root = layers_root().resolve()
    lp = (root / layer_id).resolve()
    try:
        if not lp.is_dir():
            return None
        lp.relative_to(root)
    except (OSError, ValueError):
        return None
    g = lp / ".git"
    if not g.is_symlink():
        return None
    try:
        resolved = (lp / g.readlink()).resolve()
    except OSError:
        return None
    if resolved.name != ".git":
        return None
    parent_dir = resolved.parent
    try:
        parent_dir.relative_to(root)
    except ValueError:
        return None
    parent_id = parent_dir.name
    if parent_id == layer_id or not _LAYER_ID_RE.match(parent_id):
        return None
    if not (root / parent_id).is_dir():
        return None
    return parent_id


def _validate_safe_rel_posix(rel_posix: str) -> PurePosixPath:
    # Empty is not allowed (must point to something within the layer).
    if not rel_posix:
        raise HTTPException(status_code=400, detail="Empty path")
    rel = PurePosixPath(rel_posix)
    if rel.is_absolute():
        raise HTTPException(status_code=400, detail="Absolute path is not allowed")
    if ".." in rel.parts:
        raise HTTPException(status_code=400, detail="Path traversal is not allowed")
    for part in rel.parts:
        if part in _SKIP_PARTS:
            raise HTTPException(status_code=400, detail=f"Path contains forbidden part: {part}")
    return rel


def list_layer_files(layer_id: str, prefix: str | None, max_files: int) -> dict[str, Any]:
    layer_dir = _layer_browser_root(layer_id).resolve()
    if not layer_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Layer not found: {layer_id}")

    prefix_norm: str | None = None
    if prefix:
        prefix_norm = prefix.replace("\\", "/").lstrip("/")
        if not prefix_norm:
            prefix_norm = None
        else:
            rel = _validate_safe_rel_posix(prefix_norm)
            prefix_norm = rel.as_posix()

    files: list[dict[str, Any]] = []

    try:
        it = layer_dir.rglob("*")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to iterate layer: {e}") from e

    # Note: `rglob` order is platform-dependent; we don't need a stable order for the UI.
    for p in it:
        try:
            if p.is_symlink():
                continue
            if not p.is_file():
                continue
            rel = p.relative_to(layer_dir).as_posix()
            parts = PurePosixPath(rel).parts
            if any(part in _SKIP_PARTS for part in parts):
                continue
            if prefix_norm and not rel.startswith(prefix_norm):
                continue
            st = p.stat()
            files.append(
                {
                    "path": rel,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                }
            )
            if len(files) >= max_files:
                return {"layer_id": layer_id, "files": files, "truncated": True}
        except OSError:
            continue

    return {"layer_id": layer_id, "files": files, "truncated": False}


def list_layer_children(
    *,
    layer_id: str,
    dir_rel_posix: str,
    prefix: str | None,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    layer_dir = _layer_browser_root(layer_id)
    if not layer_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Layer not found: {layer_id}")

    # Allow '' for root; otherwise validate directory path parts.
    dir_norm = (dir_rel_posix or "").replace("\\", "/").strip()
    dir_rel = PurePosixPath(".") if dir_norm == "" else _validate_safe_rel_posix(dir_norm)

    dir_fs = layer_dir.resolve()
    target = dir_fs / Path(*dir_rel.parts) if str(dir_rel) != "." else dir_fs
    try:
        target_res = target.resolve()
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Invalid dir: {e}") from e
    if not target_res.is_relative_to(dir_fs):
        raise HTTPException(status_code=400, detail="Path traversal is not allowed")
    if not target_res.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    prefix_norm: str | None = None
    if prefix:
        p = prefix.replace("\\", "/").lstrip("/").strip()
        if p:
            prefix_norm = p

    offset = _clamp_int(offset, 0, 1_000_000_000)
    limit = _clamp_int(limit, 1, 5000)

    entries: list[dict[str, Any]] = []
    try:
        for child in target_res.iterdir():
            name = child.name
            if name in _SKIP_PARTS:
                continue
            if child.is_symlink():
                continue

            rel_parent = "" if dir_norm in ("", ".") else dir_norm.strip("/")
            full_rel = name if not rel_parent else f"{rel_parent}/{name}"
            # Prefix filtering: keep ancestors of the prefix.
            # Show entry if prefix is inside this entry, or this entry is inside prefix.
            if prefix_norm and not (
                full_rel.startswith(prefix_norm)
                or prefix_norm.startswith(full_rel.rstrip("/") + "/")
            ):
                continue

            st = child.stat()
            entries.append(
                {
                    "type": "dir" if child.is_dir() else "file",
                    "name": name,
                    "path": full_rel,
                    "size": st.st_size if child.is_file() else 0,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                }
            )
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to list directory: {e}") from e

    # Deterministic order for pagination.
    entries.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["name"]))

    total = len(entries)
    sliced = entries[offset : offset + limit]
    next_offset = offset + len(sliced)

    return {
        "layer_id": layer_id,
        "dir": "" if dir_norm in ("", ".") else dir_norm.strip("/"),
        "entries": sliced,
        "offset": offset,
        "limit": limit,
        "total": total,
        "truncated": next_offset < total,
        "next_offset": next_offset,
    }


def read_layer_file(
    layer_id: str,
    file_rel_posix: str,
    *,
    max_bytes: int | None = None,
    max_text_chars: int | None = None,
) -> dict[str, Any]:
    layer_dir = _layer_browser_root(layer_id)
    if not layer_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Layer not found: {layer_id}")

    rel = _validate_safe_rel_posix(file_rel_posix)
    layer_res = layer_dir.resolve()
    target = layer_res / Path(*rel.parts)

    try:
        target_res = target.resolve()
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}") from e

    if not target_res.is_relative_to(layer_res):
        raise HTTPException(status_code=400, detail="Path traversal is not allowed")
    if not target_res.exists() or not target_res.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if target_res.is_symlink():
        raise HTTPException(status_code=400, detail="Symlinks are not readable via this endpoint")

    try:
        st = target_res.stat()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to stat file: {e}") from e

    req_max_bytes = _MAX_BYTES_DEFAULT if max_bytes is None else int(max_bytes)
    req_max_bytes = _clamp_int(req_max_bytes, 1, _MAX_BYTES_CAP)
    req_max_text_chars = _MAX_TEXT_CHARS_DEFAULT if max_text_chars is None else int(max_text_chars)
    req_max_text_chars = _clamp_int(req_max_text_chars, 1, _MAX_TEXT_CHARS_CAP)

    original_size = st.st_size
    read_limit = min(original_size, req_max_bytes)

    try:
        with target_res.open("rb") as f:
            data = f.read(read_limit)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {e}") from e

    head = data[:1024]
    if b"\0" in head:
        truncated = original_size > req_max_bytes
        return {
            "layer_id": layer_id,
            "path": rel.as_posix(),
            "kind": "binary",
            "size": original_size,
            "read_bytes": len(data),
            "truncated": truncated,
            "base64": base64.b64encode(data).decode("ascii"),
        }

    # Decode only the head bytes; if it isn't valid UTF-8, show a binary preview.
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return {
            "layer_id": layer_id,
            "path": rel.as_posix(),
            "kind": "binary",
            "size": original_size,
            "read_bytes": len(data),
            "truncated": original_size > req_max_bytes,
            "base64": base64.b64encode(data).decode("ascii"),
        }

    truncated = original_size > req_max_bytes
    if len(text) > req_max_text_chars:
        text = text[:req_max_text_chars]
        truncated = True

    return {
        "layer_id": layer_id,
        "path": rel.as_posix(),
        "kind": "text",
        "size": original_size,
        "read_bytes": len(data),
        "truncated": truncated,
        "text": text,
    }
