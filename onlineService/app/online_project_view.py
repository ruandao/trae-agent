"""将仓库根下 ``onlineProject`` 指向当前分支 tip 的可写层目录。

可写层位于 ``state_root()/layers``（见 ``layers_root``）；overlay 物化视图在 ``state_root()/runtime/materialized``。同一分支上经 ``create_stacked_layer``
逐层叠加后，任意 tip 层目录即为该分支完整工作区；用相对路径符号链接把 ``onlineProject`` 指到该层，
在宿主侧即得到与「联合视图」一致的目录树（等价于 Docker 镜像层叠加后的 merged 结果）。

可通过环境变量 ``TRAU_ONLINE_PROJECT_TIP=0`` 关闭写链接（仅调试）。"""

from __future__ import annotations

import errno
import logging
import os
import shutil
from pathlib import Path

from .layer_meta import is_overlay_v1_layer
from .layer_merge import layer_merged_root_for_api
from .layers import _LAYER_ID_RE
from .paths import layers_root, online_project_root, repo_root, runtime_dir

log = logging.getLogger(__name__)


def _tip_disabled() -> bool:
    raw = (os.environ.get("TRAU_ONLINE_PROJECT_TIP") or "").strip().lower()
    return raw in {"0", "false", "no", "off"}


def set_online_project_tip(layer_id: str) -> None:
    """使 ``onlineProject`` → 该 tip 层目录或物化目录（相对符号链接，目标在 ``REPO_ROOT`` 下）。"""
    if _tip_disabled():
        return
    lid = (layer_id or "").strip()
    if not lid:
        raise ValueError("empty layer_id")
    if is_overlay_v1_layer(lid):
        tip = layer_merged_root_for_api(lid)
    else:
        tip = layers_root() / lid
    if not tip.is_dir():
        raise ValueError(f"layer directory not found: {tip}")

    op = online_project_root()
    repo = repo_root().resolve()
    tip_res = tip.resolve()

    try:
        tip_res.relative_to(repo)
    except ValueError as e:
        raise ValueError(f"layer path must be under REPO_ROOT: {tip_res}") from e

    rel = os.path.relpath(tip_res, start=op.parent.resolve())

    op.parent.mkdir(parents=True, exist_ok=True)
    try:
        if op.is_symlink() or op.is_file():
            op.unlink()
        elif op.is_dir():
            shutil.rmtree(op)
    except OSError as e:
        raise RuntimeError(f"cannot replace onlineProject path: {op}") from e

    try:
        op.symlink_to(rel, target_is_directory=True)
    except OSError as e:
        if getattr(e, "errno", None) == errno.EEXIST:
            try:
                op.unlink()
            except OSError:
                pass
            op.symlink_to(rel, target_is_directory=True)
        else:
            raise


def clear_online_project_tip() -> None:
    """重置任务/层后移除 ``onlineProject`` 链接并建空目录，避免指向已删除的层。"""
    if _tip_disabled():
        return
    op = online_project_root()
    try:
        if op.is_symlink() or op.is_file():
            op.unlink()
        elif op.is_dir():
            shutil.rmtree(op)
    except OSError:
        log.debug("clear_online_project_tip: could not remove %s", op, exc_info=True)
    try:
        op.parent.mkdir(parents=True, exist_ok=True)
        op.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("clear_online_project_tip: could not mkdir %s", op, exc_info=True)


def get_online_project_active_info() -> dict[str, str | bool | None]:
    """返回 ``onlineProject`` 当前解析结果（供 API / 调试）。"""
    op = online_project_root()
    out: dict[str, str | bool | None] = {
        "online_project_path": str(op),
        "is_symlink": op.is_symlink(),
        "resolved_path": None,
        "symlink_target": None,
        "active_tip_layer_id": None,
    }
    try:
        if op.is_symlink():
            out["symlink_target"] = str(os.readlink(op))
        if op.exists() or op.is_symlink():
            resolved = op.resolve()
            out["resolved_path"] = str(resolved)
            try:
                lr = layers_root().resolve()
                rel = resolved.relative_to(lr)
                name = rel.parts[0] if rel.parts else ""
                if _LAYER_ID_RE.match(name):
                    out["active_tip_layer_id"] = name
            except ValueError:
                pass
            rt = (runtime_dir() / "materialized").resolve()
            try:
                relm = resolved.relative_to(rt)
                name_m = relm.parts[0] if relm.parts else ""
                if _LAYER_ID_RE.match(name_m):
                    out["active_tip_layer_id"] = name_m
            except ValueError:
                pass
    except OSError:
        pass
    return out
