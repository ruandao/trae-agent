"""Overlay 差分层：内核 OverlayFS（Linux）与用户态物化 + 差分（macOS 等）。"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import threading
from pathlib import Path

from .layer_meta import is_overlay_v1_layer, layer_chain_root_to_tip, read_layer_meta
from .layers import layer_path
from .overlay_diff import compute_diff_between_trees, materialize_merged_chain, prune_empty_dirs_under
from .overlay_kernel import overlay_mount, overlay_umount, safe_rmtree_upper_work
from .paths import runtime_dir

log = logging.getLogger(__name__)

BASE = "base"
DIFF = "diff"
UPPER = "upper"
WORK = "work"
MERGED = "merged"

_darwin_parent_merged: dict[str, Path] = {}
_materialize_locks: dict[str, threading.Lock] = {}
_materialize_locks_guard = threading.Lock()


def _materialize_lock_for_layer(layer_id: str) -> threading.Lock:
    with _materialize_locks_guard:
        lock = _materialize_locks.get(layer_id)
        if lock is None:
            lock = threading.Lock()
            _materialize_locks[layer_id] = lock
        return lock


def use_kernel_overlay() -> bool:
    if platform.system() != "Linux":
        return False
    raw = (os.environ.get("TRAU_OVERLAY_KERNEL") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def storage_path_for_layer(layer_id: str) -> Path:
    meta = read_layer_meta(layer_id)
    if not meta:
        return layer_path(layer_id)
    if meta.kind == "clone":
        return layer_path(layer_id) / BASE
    return layer_path(layer_id) / DIFF


def lower_paths_parent_stack(parent_layer_id: str) -> list[Path]:
    chain = layer_chain_root_to_tip(parent_layer_id)
    return [storage_path_for_layer(lid) for lid in chain]


def lower_paths_full_tip(layer_id: str) -> list[Path]:
    chain = layer_chain_root_to_tip(layer_id)
    return [storage_path_for_layer(lid) for lid in chain]


def layer_merged_root_for_api(layer_id: str) -> Path:
    """物化合并视图供 API / git / 文件浏览。"""
    chain = lower_paths_full_tip(layer_id)
    dest = runtime_dir() / "materialized" / layer_id
    with _materialize_lock_for_layer(layer_id):
        materialize_merged_chain(chain, dest)
    return dest


def prepare_job_run(layer_id: str, *, reuse_upper: bool = False) -> Path:
    """任务 cwd = merged；Linux 使用内核 overlay + upper/work。"""
    meta = read_layer_meta(layer_id)
    if not meta or meta.kind != "job" or not meta.parent_layer_id:
        raise ValueError(f"not an overlay job layer: {layer_id}")
    parent_id = meta.parent_layer_id
    lp = layer_path(layer_id)
    merged = lp / MERGED
    upper = lp / UPPER
    work = lp / WORK

    lowers = lower_paths_parent_stack(parent_id)
    if not lowers:
        raise ValueError(f"empty lower stack for parent {parent_id}")
    lowers_top_first = list(reversed(lowers))

    if use_kernel_overlay():
        overlay_umount(merged)
        if not reuse_upper:
            safe_rmtree_upper_work(lp, UPPER, WORK)
            upper.mkdir(parents=True, exist_ok=True)
            work.mkdir(parents=True, exist_ok=True)
        merged.mkdir(parents=True, exist_ok=True)
        overlay_mount(lowers_top_first, upper, work, merged)
        return merged.resolve()

    # 用户态：merged 为完整可写副本；中断「继续」时复用已有 merged
    if reuse_upper and merged.is_dir():
        try:
            next(merged.iterdir())
            _ensure_darwin_parent_cache(layer_id, lowers)
            return merged.resolve()
        except StopIteration:
            pass

    mat = runtime_dir() / "materialized" / f"parent_stack_{layer_id}"
    if mat.exists():
        shutil.rmtree(mat)
    materialize_merged_chain(lowers, mat)
    _darwin_parent_merged[layer_id] = mat
    shutil.rmtree(merged, ignore_errors=True)
    shutil.copytree(mat, merged, symlinks=True)
    return merged.resolve()


def _ensure_darwin_parent_cache(layer_id: str, lowers: list[Path]) -> None:
    if layer_id in _darwin_parent_merged:
        return
    mat = runtime_dir() / "materialized" / f"parent_stack_{layer_id}"
    if mat.exists():
        shutil.rmtree(mat)
    materialize_merged_chain(lowers, mat)
    _darwin_parent_merged[layer_id] = mat


def finalize_job_overlay_finished(layer_id: str) -> None:
    """任务正常结束或失败：提交 upper → diff（Linux）或物化差分（Darwin）。"""
    meta = read_layer_meta(layer_id)
    if not meta or meta.kind != "job":
        return
    lp = layer_path(layer_id)
    merged = lp / MERGED
    upper = lp / UPPER
    work = lp / WORK
    diff = lp / DIFF

    if use_kernel_overlay():
        overlay_umount(merged)
        shutil.rmtree(work, ignore_errors=True)
        if diff.exists():
            shutil.rmtree(diff)
        if upper.exists() and any(upper.iterdir()):
            upper.rename(diff)
            prune_empty_dirs_under(diff)
        elif upper.exists():
            shutil.rmtree(upper, ignore_errors=True)
            diff.mkdir(parents=True, exist_ok=True)
        upper.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
        merged.mkdir(parents=True, exist_ok=True)
        return

    parent_mat = _darwin_parent_merged.pop(layer_id, None)
    if parent_mat is None or not parent_mat.is_dir():
        log.warning("finalize_job_overlay_finished: missing parent cache for %s", layer_id)
        shutil.rmtree(merged, ignore_errors=True)
        return
    if diff.exists():
        shutil.rmtree(diff)
    compute_diff_between_trees(parent_mat, merged, diff)
    shutil.rmtree(merged, ignore_errors=True)
    shutil.rmtree(parent_mat, ignore_errors=True)


def interrupt_job_overlay(layer_id: str) -> None:
    """中断：仅卸载内核 overlay；保留 upper/merged 供继续执行。"""
    if not use_kernel_overlay():
        return
    lp = layer_path(layer_id)
    merged = lp / MERGED
    overlay_umount(merged)
