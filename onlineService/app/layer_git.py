"""Git helpers for writable layers (list branches, checkout)."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from fastapi import HTTPException

from .git_clone import _validate_branch
from .layers import layer_path

_LAYER_ID_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})_(?P<suf>[0-9a-fA-F]+)$")


def _ensure_layer_id(layer_id: str) -> None:
    if not layer_id or not _LAYER_ID_RE.match(layer_id):
        raise HTTPException(status_code=400, detail="invalid layer_id")


async def list_branches(layer_id: str) -> dict:
    """Return local + remote branch short names and current branch if any."""
    _ensure_layer_id(layer_id)
    root = layer_path(layer_id)
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="layer not found")
    git_dir = root / ".git"
    if not git_dir.exists():
        return {"branches": [], "current": None, "is_git": False}

    proc = await asyncio.create_subprocess_exec(
        "git",
        "for-each-ref",
        "--sort=refname",
        "--format=%(refname:short)",
        "refs/heads",
        "refs/remotes",
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    out_b, err_b = await proc.communicate()
    if proc.returncode != 0:
        err = err_b.decode(errors="replace").strip()
        raise HTTPException(
            status_code=400,
            detail=f"git for-each-ref failed: {err or proc.returncode}",
        )

    raw = out_b.decode(errors="replace").splitlines()
    seen: set[str] = set()
    branches: list[str] = []
    for line in raw:
        name = line.strip()
        if not name or name in seen:
            continue
        # 跳过纯 remote 顶层如 origin（若出现）
        if name == "origin" or name.endswith("/HEAD"):
            continue
        seen.add(name)
        branches.append(name)

    cur_proc = await asyncio.create_subprocess_exec(
        "git",
        "branch",
        "--show-current",
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    cur_out, _ = await cur_proc.communicate()
    current = cur_out.decode(errors="replace").strip() if cur_proc.returncode == 0 else None
    if not current:
        current = None

    return {"branches": branches, "current": current, "is_git": True}


async def git_checkout(workdir: Path, branch: str) -> tuple[str, int]:
    """Run ``git checkout`` in workdir. Returns (combined output, exit code)."""
    b = _validate_branch(branch)
    if not b:
        return ("invalid branch name\n", -1)
    proc = await asyncio.create_subprocess_exec(
        "git",
        "checkout",
        b,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    assert proc.stdout is not None
    out_b = await proc.stdout.read()
    code = await proc.wait()
    return out_b.decode(errors="replace"), code
