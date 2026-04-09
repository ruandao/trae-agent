"""Writable workspace layers.

可写层根目录默认在 ``onlineProject_state/layers/``（见 ``paths.layers_root``），不在 ``onlineProject`` 下。

**Overlay v1（默认新任务）**：克隆层仅含 ``base/`` + ``layer_meta.json``；任务层仅 ``diff/`` + 元数据，
运行时在内核 OverlayFS（Linux）或物化目录（macOS）上执行，结束后提交为 whiteout 差分层。
**旧版** ``create_stacked_layer`` 仍用于无 ``layer_meta`` 的遗留目录。
"""

import platform
import re
import secrets
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .layer_meta import write_layer_meta
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


def create_clone_layer(layer_id: str) -> Path:
    """Overlay v1：克隆层仅含 ``base/`` + 元数据（完整树在 base）。"""
    p = layer_path(layer_id)
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True, exist_ok=True)
    (p / "base").mkdir(exist_ok=True)
    write_layer_meta(layer_id, kind="clone", parent_layer_id=None)
    return p.resolve()


def create_job_layer(layer_id: str, parent_layer_id: str) -> Path:
    """Overlay v1：任务层仅元数据 + 空 ``diff/``；运行时在 merged 上写入，结束后提交为 diff。"""
    p = layer_path(layer_id)
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True, exist_ok=True)
    (p / "diff").mkdir(exist_ok=True)
    write_layer_meta(layer_id, kind="job", parent_layer_id=parent_layer_id)
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
