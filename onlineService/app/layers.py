"""Writable workspace layers (stacked copies for portability).

基于父层「新建层」时，优先使用文件系统 COW/reflink（与父层共享数据块、写入时再分裂），
在 APFS / btrfs / xfs 等环境下可避免每次全量逐字节复制；不支持时退回 shutil。
"""

import platform
import re
import secrets
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .paths import layers_root

# 不复制 .git：在 create_stacked_layer 末尾用符号链接指向父层 .git，各可写层共享同一仓库元数据。
_SKIP_NAMES = {"__pycache__", ".DS_Store", ".git"}
_LAYER_ID_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})_(?P<suf>[0-9a-fA-F]+)$")


def new_layer_id() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suf = secrets.token_hex(3)
    return f"{ts}_{suf}"


def layer_path(layer_id: str) -> Path:
    return layers_root() / layer_id


def _link_shared_git(child: Path, parent: Path) -> None:
    """若父层存在 .git，则在子层建立相对符号链接 child/.git -> ../<parent_id>/.git。"""
    git_src = parent / ".git"
    if not git_src.exists():
        return
    dest = child / ".git"
    try:
        if dest.exists() or dest.is_symlink():
            dest.unlink()
    except OSError:
        return
    try:
        rel_target = Path("..") / parent.name / ".git"
        dest.symlink_to(rel_target, target_is_directory=git_src.is_dir())
    except OSError:
        pass


def _copy_entry_reflink_or_shutil(src: Path, dest: Path) -> None:
    """将父层一项复制到子层：先试 COW/reflink，失败则用 shutil（与旧行为一致）。"""
    cp = shutil.which("cp")
    if cp and not dest.exists():
        try:
            if platform.system() == "Darwin":
                r = subprocess.run(
                    [cp, "-cR", str(src), str(dest)],
                    capture_output=True,
                    timeout=7200,
                )
            else:
                r = subprocess.run(
                    [cp, "-a", "--reflink=auto", str(src), str(dest)],
                    capture_output=True,
                    timeout=7200,
                )
            if r.returncode == 0:
                try:
                    if dest.exists():
                        return
                except OSError:
                    pass
        except (OSError, subprocess.TimeoutExpired):
            pass

    try:
        if src.is_symlink() or src.is_file():
            shutil.copy2(src, dest, follow_symlinks=False)
        elif src.is_dir():
            shutil.copytree(src, dest, symlinks=True, ignore_dangling_symlinks=True)
    except OSError:
        raise


def create_root_layer(layer_id: str) -> Path:
    p = layer_path(layer_id)
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def create_stacked_layer(layer_id: str, parent_layer_path: Path) -> Path:
    child = layer_path(layer_id)
    if child.exists():
        shutil.rmtree(child)
    child.mkdir(parents=True, exist_ok=True)
    parent = parent_layer_path.resolve()
    if not parent.is_dir():
        return child.resolve()
    for item in parent.iterdir():
        if item.name in _SKIP_NAMES:
            continue
        dest = child / item.name
        try:
            _copy_entry_reflink_or_shutil(item, dest)
        except OSError:
            continue
    _link_shared_git(child, parent)
    return child.resolve()


def cleanup_layers() -> dict[str, int]:
    """Delete writable layer directories created for jobs.

    Safety: only delete directories whose name matches `layer_id` format.
    """
    root = layers_root()
    removed = 0
    skipped = 0

    if not root.is_dir():
        return {"removed": 0, "skipped": 0}

    for p in root.iterdir():
        try:
            if not p.is_dir():
                continue
            if not _LAYER_ID_RE.match(p.name):
                skipped += 1
                continue
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
        except OSError:
            skipped += 1

    return {"removed": removed, "skipped": skipped}
