"""OverlayFS 上层（diff）语义：whiteout、opaque 与用户态合并 / 差分。"""

from __future__ import annotations

import contextlib
import filecmp
import os
import shutil
import stat
from pathlib import Path

WHITEOUT_MAJOR = 0
WHITEOUT_MINOR = 0
OPAQUE_XATTR = "trusted.overlay.opaque"


def is_whiteout(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISCHR(st.st_mode)
        and os.major(st.st_rdev) == WHITEOUT_MAJOR
        and os.minor(st.st_rdev) == WHITEOUT_MINOR
    )


def is_opaque_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        return False
    try:
        val = getxattr(str(path), OPAQUE_XATTR)
        return val == b"y"
    except OSError:
        return False


def prune_empty_dirs_under(diff_root: Path) -> None:
    """删除 diff 树内无内容的空目录（自底向上），不删除 diff_root 本身。

    联合视图里存在的空目录不必写入 diff；内核 Overlay 的 upper 也可能留下占位空目录。
    """
    if not diff_root.is_dir():
        return
    for child in list(diff_root.iterdir()):
        if child.is_dir() and not child.is_symlink():
            prune_empty_dirs_under(child)
            with contextlib.suppress(OSError):
                child.rmdir()


def create_whiteout_file(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.exists() or target.is_symlink():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
    except OSError:
        pass
    os.mknod(str(target), stat.S_IFCHR | 0o600, os.makedev(WHITEOUT_MAJOR, WHITEOUT_MINOR))


def materialize_merged_chain(lower_paths_root_to_tip: list[Path], dest: Path) -> None:
    """按序合并 lower（clone base → … → tip diff）到 dest。"""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if not lower_paths_root_to_tip:
        return
    base = lower_paths_root_to_tip[0]
    if base.is_dir():
        shutil.copytree(base, dest, symlinks=True, dirs_exist_ok=True)
    for diff_layer in lower_paths_root_to_tip[1:]:
        if diff_layer.is_dir():
            _apply_diff_layer(diff_layer, dest)


def _apply_diff_layer(diff_root: Path, merged: Path) -> None:
    paths = sorted(diff_root.rglob("*"), key=lambda p: (len(p.parts), str(p)))
    for path in paths:
        if path == diff_root:
            continue
        rel = path.relative_to(diff_root)
        target = merged / rel
        if path.is_dir() and not path.is_symlink():
            if is_opaque_dir(path):
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(path, target, symlinks=True)
            continue
        if is_whiteout(path):
            if target.exists() or target.is_symlink():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            continue
        if path.is_file() or path.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_dir():
                shutil.rmtree(target)
            shutil.copy2(path, target, follow_symlinks=False)


def compute_diff_between_trees(parent_merged: Path, child_merged: Path, diff_out: Path) -> None:
    """两棵物化树之间的差异写入 diff_out（含 whiteout）。

    仅空目录的变更不写入 diff；有文件/符号链接时由父路径按需创建。末尾会修剪残余空目录。
    """
    if diff_out.exists():
        shutil.rmtree(diff_out)
    diff_out.mkdir(parents=True, exist_ok=True)
    if not parent_merged.is_dir() or not child_merged.is_dir():
        return
    for path in sorted(child_merged.rglob("*")):
        rel = path.relative_to(child_merged)
        if str(rel) == ".":
            continue
        p = parent_merged / rel
        dest = diff_out / rel
        if path.is_dir() and not path.is_symlink():
            # 不在 diff 中存放「仅空目录」：有文件/符号链接时由下方分支写入并 mkdir 父路径。
            continue
        if path.is_file() or path.is_symlink():
            dest.parent.mkdir(parents=True, exist_ok=True)
            if (
                not p.exists()
                or p.is_dir()
                or path.is_symlink()
                or p.is_symlink()
                or p.is_file()
                and not filecmp.cmp(path, p, shallow=False)
            ):
                shutil.copy2(path, dest, follow_symlinks=False)
    for path in sorted(parent_merged.rglob("*")):
        rel = path.relative_to(parent_merged)
        if str(rel) == ".":
            continue
        c = child_merged / rel
        if path.is_file() and not c.exists() and not (diff_out / rel).exists():
            create_whiteout_file(diff_out / rel)
    prune_empty_dirs_under(diff_out)
