"""Git helpers for writable layers (list branches, checkout, commit)."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .git_clone import _validate_branch
from .layers import layer_path

_LAYER_ID_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})_(?P<suf>[0-9a-fA-F]+)$")


def _ensure_layer_id(layer_id: str) -> None:
    if not layer_id or not _LAYER_ID_RE.match(layer_id):
        raise HTTPException(status_code=400, detail="invalid layer_id")


def git_worktree_dirty(layer_id: str) -> bool | None:
    """``True``：存在未暂存或未提交变更；``False``：工作区干净；``None``：非 git 或检测失败。"""
    if not layer_id or not _LAYER_ID_RE.match(layer_id):
        return None
    root = layer_path(layer_id)
    if not root.is_dir() or not (root / ".git").exists():
        return None
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if r.returncode != 0:
            return None
        return bool((r.stdout or "").strip())
    except (OSError, subprocess.TimeoutExpired):
        return None


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


_MAX_COMMIT_MSG_LEN = 4096


def _strip_explicit_commit_message(message: str | None) -> str:
    raw = (message or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="commit message is empty")
    if len(raw) > _MAX_COMMIT_MSG_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"commit message exceeds {_MAX_COMMIT_MSG_LEN} characters",
        )
    return raw


def _build_auto_commit_message(
    *,
    command_hint: str | None,
    stat_text: str,
    shortstat: str,
    files: list[str],
) -> str:
    """根据任务指令与暂存区内容撰写多行提交说明。"""
    cmd = (command_hint or "").strip()
    if cmd:
        first = cmd.replace("\r", "").split("\n", 1)[0].strip()
        if len(first) > 72:
            first = first[:69] + "..."
        title = f"chore(online-layer): {first}"
    else:
        n = len(files)
        title = f"chore(online-layer): 更新 {n} 个文件" if n else "chore(online-layer): 工作区变更"

    lines: list[str] = [title, ""]
    lines.append("【任务指令】")
    lines.append(cmd if cmd else "（无关联任务记录，可能为克隆层或未通过本服务创建的任务）")
    lines.append("")
    lines.append("【变更统计】")
    lines.append(shortstat.strip() or stat_text.strip() or "—")
    lines.append("")
    lines.append("【git diff --stat】")
    st = stat_text.strip()
    lines.append(st if st else "（空）")
    lines.append("")
    lines.append("【涉及文件】")
    if files:
        for name in files[:200]:
            lines.append(f"  {name}")
        if len(files) > 200:
            lines.append(f"  … 共 {len(files)} 个文件，以上仅列出前 200 个")
    else:
        lines.append("  （无法列出文件）")

    msg = "\n".join(lines)
    if len(msg) > _MAX_COMMIT_MSG_LEN:
        msg = msg[: _MAX_COMMIT_MSG_LEN - 24] + "\n…(说明已截断)"
    return msg


async def _git_diff_cached_text(
    root: Path,
    env: dict[str, str],
    *args: str,
) -> tuple[str, int]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--cached",
        *args,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    out_b, _ = await proc.communicate()
    return out_b.decode(errors="replace"), proc.returncode or 0


async def commit_layer_worktree(
    layer_id: str,
    message: str | None,
    *,
    command_hint: str | None = None,
) -> dict[str, Any]:
    """暂存全部变更（``git add -A``）并提交到该层工作区所在仓库。

    * ``message`` 非空：作为完整提交说明（手动覆盖）。
    * ``message`` 为空：根据 ``command_hint`` 与暂存区 ``git diff`` 自动生成说明。
    """
    _ensure_layer_id(layer_id)
    root = layer_path(layer_id)
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="layer not found")
    if not (root / ".git").exists():
        raise HTTPException(status_code=400, detail="layer has no .git")

    explicit = (message or "").strip()

    author_name = (os.environ.get("TRAE_GIT_COMMITTER_NAME") or "trae-online-service").strip() or "trae-online-service"
    author_email = (os.environ.get("TRAE_GIT_COMMITTER_EMAIL") or "trae-online@local").strip() or "trae-online@local"

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    proc_add = await asyncio.create_subprocess_exec(
        "git",
        "add",
        "-A",
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    out_add_b, _ = await proc_add.communicate()
    out_add = out_add_b.decode(errors="replace")
    if proc_add.returncode != 0:
        raise HTTPException(status_code=400, detail=f"git add failed:\n{out_add.strip()}")

    names_out, code_n = await _git_diff_cached_text(root, env, "--name-only", "-z")
    if code_n != 0:
        raise HTTPException(status_code=400, detail=f"git diff --cached failed:\n{names_out.strip()}")
    files = [p for p in names_out.split("\0") if p.strip()]
    if not files:
        return {
            "layer_id": layer_id,
            "status": "noop",
            "commit_message": None,
            "detail": "工作区无变更，未创建提交",
            "output": "",
        }

    stat_text, code_s = await _git_diff_cached_text(root, env, "--stat")
    if code_s != 0:
        raise HTTPException(status_code=400, detail=f"git diff --cached --stat failed:\n{stat_text.strip()}")

    shortstat, code_ss = await _git_diff_cached_text(root, env, "--shortstat")
    if code_ss != 0:
        shortstat = ""

    if explicit:
        msg = _strip_explicit_commit_message(explicit)
    else:
        msg = _build_auto_commit_message(
            command_hint=command_hint,
            stat_text=stat_text,
            shortstat=shortstat,
            files=files,
        )

    cenv = {
        **env,
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    }

    fd, path = tempfile.mkstemp(prefix="gitmsg-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(msg)
        proc_commit = await asyncio.create_subprocess_exec(
            "git",
            "commit",
            "-F",
            path,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=cenv,
        )
        out_commit_b, _ = await proc_commit.communicate()
        out_commit = out_commit_b.decode(errors="replace")
        code = proc_commit.returncode or 0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    low = out_commit.lower()
    if code != 0:
        if "nothing to commit" in low or "nothing added to commit" in low:
            return {
                "layer_id": layer_id,
                "status": "noop",
                "commit_message": None,
                "detail": "工作区无变更，未创建提交",
                "output": out_commit.strip(),
            }
        raise HTTPException(status_code=400, detail=f"git commit failed:\n{out_commit.strip()}")

    return {
        "layer_id": layer_id,
        "status": "ok",
        "commit_message": msg,
        "output": out_commit.strip(),
    }


def git_ahead_of_upstream(layer_id: str) -> dict[str, Any]:
    """当前分支相对上游的领先提交数（共享 ``.git`` 的层结果一致）。"""
    _ensure_layer_id(layer_id)
    root = layer_path(layer_id)
    if not root.is_dir() or not (root / ".git").exists():
        return {
            "is_git": False,
            "ahead": None,
            "branch": None,
            "upstream": None,
            "no_upstream": None,
        }

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    r_head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    branch = r_head.stdout.strip() if r_head.returncode == 0 else None

    r_u = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "@{u}"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if r_u.returncode != 0:
        return {
            "is_git": True,
            "ahead": None,
            "branch": branch,
            "upstream": None,
            "no_upstream": True,
        }

    upstream = r_u.stdout.strip()
    r_cnt = subprocess.run(
        ["git", "rev-list", "--count", f"{upstream}..HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    if r_cnt.returncode != 0:
        return {
            "is_git": True,
            "ahead": None,
            "branch": branch,
            "upstream": upstream,
            "no_upstream": False,
        }
    try:
        ahead = int((r_cnt.stdout or "").strip())
    except ValueError:
        ahead = None
    return {
        "is_git": True,
        "ahead": ahead,
        "branch": branch,
        "upstream": upstream,
        "no_upstream": False,
    }


_MAX_PARENT_DIFF_CHARS = 400_000


def diff_layer_worktree_vs_parent(parent_layer_id: str, layer_id: str) -> dict[str, Any]:
    """对比子层工作区目录与父层目录（排除 ``.git``），使用系统 ``diff -ruN``。"""
    _ensure_layer_id(layer_id)
    _ensure_layer_id(parent_layer_id)
    if parent_layer_id == layer_id:
        raise HTTPException(status_code=400, detail="invalid parent/child pair")

    parent_root = layer_path(parent_layer_id).resolve()
    child_root = layer_path(layer_id).resolve()
    if not parent_root.is_dir():
        raise HTTPException(status_code=404, detail="parent layer not found")
    if not child_root.is_dir():
        raise HTTPException(status_code=404, detail="layer not found")

    try:
        proc = subprocess.run(
            ["diff", "-ruN", "-x", ".git", str(parent_root), str(child_root)],
            capture_output=True,
            text=True,
            timeout=180,
            env={**os.environ, "LANG": os.environ.get("LANG", "C.UTF-8")},
        )
    except subprocess.TimeoutExpired as e:
        raise HTTPException(status_code=504, detail=f"diff timed out: {e}") from e

    # diff 返回 1 表示有差异，0 表示相同，2 为错误
    if proc.returncode not in (0, 1):
        err = (proc.stderr or proc.stdout or "").strip()
        raise HTTPException(
            status_code=400,
            detail=f"diff failed (code {proc.returncode}): {err or 'unknown'}",
        )

    out = proc.stdout or ""
    if proc.stderr:
        out = (out + "\n" + proc.stderr).strip()

    truncated = False
    if len(out) > _MAX_PARENT_DIFF_CHARS:
        out = out[:_MAX_PARENT_DIFF_CHARS] + "\n\n…（输出已截断，可在本地对两层目录执行 diff -ruN -x .git）"
        truncated = True

    return {
        "layer_id": layer_id,
        "parent_layer_id": parent_layer_id,
        "same": proc.returncode == 0,
        "diff": out if proc.returncode != 0 else "",
        "truncated": truncated,
    }


async def push_layer_worktree(layer_id: str) -> dict[str, Any]:
    """将当前分支推送到已配置的上游（``git push``）。"""
    _ensure_layer_id(layer_id)
    root = layer_path(layer_id)
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="layer not found")
    if not (root / ".git").exists():
        raise HTTPException(status_code=400, detail="layer has no .git")

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = await asyncio.create_subprocess_exec(
        "git",
        "push",
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    assert proc.stdout is not None
    out_b = await proc.stdout.read()
    code = await proc.wait()
    out = out_b.decode(errors="replace").strip()

    if code != 0:
        raise HTTPException(
            status_code=400,
            detail={"message": "git push failed", "exit_code": code, "output": out},
        )

    return {"layer_id": layer_id, "status": "ok", "output": out}
