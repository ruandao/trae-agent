"""Linux 内核 OverlayFS 挂载（需 CAP_SYS_ADMIN / 通常容器内 root）。"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def overlay_mount(lower_paths_top_to_bottom: list[Path], upper: Path, work: Path, merged: Path) -> None:
    """lower_paths: 自顶向下（最先出现的目录在 overlay 中最优先）。"""
    if os.name != "posix":
        raise OSError("overlay mount requires POSIX")
    lowers = [p.resolve() for p in lower_paths_top_to_bottom]
    for p in lowers:
        if not p.exists():
            raise FileNotFoundError(f"overlay lower missing: {p}")
    upper.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    merged.mkdir(parents=True, exist_ok=True)
    lower = ":".join(str(p) for p in lowers)
    cmd = [
        "mount",
        "-t",
        "overlay",
        "overlay",
        "-o",
        f"lowerdir={lower},upperdir={upper},workdir={work}",
        str(merged),
    ]
    log.debug("overlay mount: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def overlay_umount(merged: Path) -> None:
    try:
        subprocess.run(["umount", "-l", str(merged)], check=False, capture_output=True)
    except OSError as e:
        log.debug("umount %s: %s", merged, e)


def is_merged_mountpoint(merged: Path) -> bool:
    try:
        r = subprocess.run(
            ["mount"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            return False
        return str(merged.resolve()) in (r.stdout or "")
    except (OSError, subprocess.TimeoutExpired):
        return False


def safe_rmtree_upper_work(lp: Path, upper_name: str, work_name: str) -> None:
    u = lp / upper_name
    w = lp / work_name
    shutil.rmtree(u, ignore_errors=True)
    shutil.rmtree(w, ignore_errors=True)
