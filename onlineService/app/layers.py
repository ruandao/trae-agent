"""Writable workspace layers (stacked copies for portability)."""

import secrets
import shutil
import re
from datetime import datetime
from pathlib import Path

from .paths import layers_root

_SKIP_NAMES = {".git", "__pycache__", ".DS_Store"}
_LAYER_ID_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})_(?P<suf>[0-9a-fA-F]+)$")


def new_layer_id() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suf = secrets.token_hex(3)
    return f"{ts}_{suf}"


def layer_path(layer_id: str) -> Path:
    return layers_root() / layer_id


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
            if item.is_symlink() or item.is_file():
                shutil.copy2(item, dest, follow_symlinks=False)
            elif item.is_dir():
                shutil.copytree(item, dest, symlinks=True, ignore_dangling_symlinks=True)
        except OSError:
            continue
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
