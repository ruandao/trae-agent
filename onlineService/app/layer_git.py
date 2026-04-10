"""Git helpers for writable layers (list branches, checkout, commit)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import HTTPException

from trae_agent.utils.auto_commit_message import (
    build_auto_commit_message,
    load_latest_trajectory_data,
)

from .git_clone import _validate_branch
from .layer_merge import layer_merged_root_for_api
from .layer_meta import is_overlay_v1_layer
from .layers import layer_path


def layer_git_workspace_root(layer_id: str) -> Path:
    """git 工作区根：Overlay 层为物化合并视图（含 base + diff 链）。"""
    if is_overlay_v1_layer(layer_id):
        return layer_merged_root_for_api(layer_id)
    return layer_path(layer_id)


_LAYER_ID_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})_(?P<suf>[0-9a-fA-F]+)$")


def _ensure_layer_id(layer_id: str) -> None:
    if not layer_id or not _LAYER_ID_RE.match(layer_id):
        raise HTTPException(status_code=400, detail="invalid layer_id")


def git_worktree_dirty(layer_id: str) -> bool | None:
    """``True``：存在未暂存或未提交变更；``False``：工作区干净；``None``：非 git 或检测失败。"""
    if not layer_id or not _LAYER_ID_RE.match(layer_id):
        return None
    root = layer_git_workspace_root(layer_id)
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
    root = layer_git_workspace_root(layer_id)
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
    * ``message`` 为空：根据 ``command_hint``、最新轨迹 JSON（若存在）与暂存区统计自动生成说明。
    """
    _ensure_layer_id(layer_id)
    root = layer_git_workspace_root(layer_id)
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="layer not found")
    if not (root / ".git").exists():
        raise HTTPException(status_code=400, detail="layer has no .git")

    explicit = (message or "").strip()

    author_name = (
        os.environ.get("TRAE_GIT_COMMITTER_NAME") or "trae-online-service"
    ).strip() or "trae-online-service"
    author_email = (
        os.environ.get("TRAE_GIT_COMMITTER_EMAIL") or "trae-online@local"
    ).strip() or "trae-online@local"

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
        raise HTTPException(
            status_code=400, detail=f"git diff --cached failed:\n{names_out.strip()}"
        )
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
        raise HTTPException(
            status_code=400, detail=f"git diff --cached --stat failed:\n{stat_text.strip()}"
        )

    shortstat, code_ss = await _git_diff_cached_text(root, env, "--shortstat")
    if code_ss != 0:
        shortstat = ""

    if explicit:
        msg = _strip_explicit_commit_message(explicit)
    else:
        traj = load_latest_trajectory_data(root)
        msg = build_auto_commit_message(
            command_hint=command_hint,
            stat_text=stat_text,
            shortstat=shortstat,
            files=files,
            trajectory=traj,
            max_total_len=_MAX_COMMIT_MSG_LEN,
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
        with contextlib.suppress(OSError):
            os.unlink(path)

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
    root = layer_git_workspace_root(layer_id)
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
_MAX_CHANGE_LIST_LINES = 2500
_MAX_SINGLE_PATH_DIFF_CHARS = 350_000

_FILES_DIFF_RQ_RE = re.compile(r"^Files (.+) and (.+) differ\s*$")
_ONLY_IN_RQ_RE = re.compile(r"^Only in (.+): (.+)\s*$")


def _parse_diff_rq_output(output: str, parent_root: Path, child_root: Path) -> list[dict[str, str]]:
    """Parse ``diff -rq`` lines into ``{path, kind}`` where kind is ``modified`` | ``added`` | ``removed``."""
    parent_r = parent_root.resolve()
    child_r = child_root.resolve()
    items: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    def add_one(rel: str, kind: str) -> None:
        rel = rel.strip().lstrip("/")
        if not rel or rel == ".":
            return
        if ".." in PurePosixPath(rel).parts:
            return
        if rel in seen_paths:
            return
        seen_paths.add(rel)
        items.append({"path": rel, "kind": kind})

    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _FILES_DIFF_RQ_RE.match(line)
        if m:
            try:
                ar = Path(m.group(1)).resolve()
                br = Path(m.group(2)).resolve()
                rel_a = ar.relative_to(parent_r).as_posix()
                rel_b = br.relative_to(child_r).as_posix()
            except ValueError:
                continue
            rel = rel_a if rel_a == rel_b else rel_b
            add_one(rel, "modified")
            continue
        m2 = _ONLY_IN_RQ_RE.match(line)
        if m2:
            try:
                dir_abs = Path(m2.group(1)).resolve()
                name = m2.group(2).strip()
                if not name:
                    continue
                full = (dir_abs / name).resolve()
                try:
                    rel = full.relative_to(child_r).as_posix()
                    add_one(rel, "added")
                except ValueError:
                    rel = full.relative_to(parent_r).as_posix()
                    add_one(rel, "removed")
            except ValueError:
                continue

    items.sort(key=lambda x: x["path"])
    return items


def list_layer_changes_vs_parent(parent_layer_id: str, layer_id: str) -> dict[str, Any]:
    """列出子层相对父层工作区（排除 ``.git``）的变动路径摘要，基于 ``diff -rq``。"""
    _ensure_layer_id(layer_id)
    _ensure_layer_id(parent_layer_id)
    if parent_layer_id == layer_id:
        raise HTTPException(status_code=400, detail="invalid parent/child pair")

    parent_root = layer_git_workspace_root(parent_layer_id).resolve()
    child_root = layer_git_workspace_root(layer_id).resolve()
    if not parent_root.is_dir():
        raise HTTPException(status_code=404, detail="parent layer not found")
    if not child_root.is_dir():
        raise HTTPException(status_code=404, detail="layer not found")

    try:
        proc = subprocess.run(
            ["diff", "-rq", "-x", ".git", str(parent_root), str(child_root)],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "LANG": os.environ.get("LANG", "C.UTF-8")},
        )
    except subprocess.TimeoutExpired as e:
        raise HTTPException(status_code=504, detail=f"diff timed out: {e}") from e

    if proc.returncode not in (0, 1):
        err = (proc.stderr or proc.stdout or "").strip()
        raise HTTPException(
            status_code=400,
            detail=f"diff failed (code {proc.returncode}): {err or 'unknown'}",
        )

    out = proc.stdout or ""
    items = _parse_diff_rq_output(out, parent_root, child_root)
    truncated_list = False
    if len(items) > _MAX_CHANGE_LIST_LINES:
        items = items[:_MAX_CHANGE_LIST_LINES]
        truncated_list = True

    return {
        "layer_id": layer_id,
        "parent_layer_id": parent_layer_id,
        "same": proc.returncode == 0,
        "changes": items,
        "truncated": truncated_list,
    }


def diff_layer_one_path_vs_parent(
    parent_layer_id: str,
    layer_id: str,
    file_rel_posix: str,
) -> dict[str, Any]:
    """单路径相对父层的 unified diff（文件用 ``diff -uN``；目录用 ``diff -ruN -x .git``）。"""
    from .layer_fs import _validate_safe_rel_posix

    _ensure_layer_id(layer_id)
    _ensure_layer_id(parent_layer_id)
    if parent_layer_id == layer_id:
        raise HTTPException(status_code=400, detail="invalid parent/child pair")

    norm = (file_rel_posix or "").replace("\\", "/").lstrip("/")
    rel = _validate_safe_rel_posix(norm)
    rel_s = rel.as_posix()

    parent_root = layer_git_workspace_root(parent_layer_id).resolve()
    child_root = layer_git_workspace_root(layer_id).resolve()
    if not parent_root.is_dir():
        raise HTTPException(status_code=404, detail="parent layer not found")
    if not child_root.is_dir():
        raise HTTPException(status_code=404, detail="layer not found")

    p_path = parent_root / Path(*rel.parts)
    c_path = child_root / Path(*rel.parts)
    try:
        p_res = p_path.resolve()
        c_res = c_path.resolve()
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"invalid path: {e}") from e
    if not p_res.is_relative_to(parent_root) or not c_res.is_relative_to(child_root):
        raise HTTPException(status_code=400, detail="path escapes layer root")

    p_exists = p_res.exists()
    c_exists = c_res.exists()
    if not p_exists and not c_exists:
        raise HTTPException(status_code=404, detail="path not found in parent or child layer")

    p_is_dir = p_exists and p_res.is_dir()
    c_is_dir = c_exists and c_res.is_dir()
    p_is_file = p_exists and p_res.is_file()
    c_is_file = c_exists and c_res.is_file()

    def run_diff(argv: list[str]) -> tuple[str, int]:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=90,
            env={**os.environ, "LANG": os.environ.get("LANG", "C.UTF-8")},
        )
        if proc.returncode not in (0, 1):
            err = (proc.stderr or proc.stdout or "").strip()
            raise HTTPException(
                status_code=400,
                detail=f"diff failed (code {proc.returncode}): {err or 'unknown'}",
            )
        out_l = proc.stdout or ""
        if proc.stderr:
            out_l = (out_l + "\n" + proc.stderr).strip()
        return out_l, proc.returncode

    truncated = False
    if p_is_file and c_is_file:
        kind = "file"
        diff_text, code = run_diff(["diff", "-uN", str(p_res), str(c_res)])
    elif p_is_file or c_is_file:
        if p_is_dir or c_is_dir:
            raise HTTPException(
                status_code=400,
                detail="路径在一侧为文件、另一侧为目录，无法生成 diff",
            )
        left = str(p_res) if p_exists else "/dev/null"
        right = str(c_res) if c_exists else "/dev/null"
        kind = "file"
        diff_text, code = run_diff(["diff", "-uN", left, right])
    else:
        kind = "dir"
        diff_text, code = run_diff(["diff", "-ruN", "-x", ".git", str(p_res), str(c_res)])

    if len(diff_text) > _MAX_SINGLE_PATH_DIFF_CHARS:
        diff_text = (
            diff_text[:_MAX_SINGLE_PATH_DIFF_CHARS]
            + "\n\n…（此路径 diff 已截断；完整内容可用 GET /api/layers/{id}/diff/parent）"
        )
        truncated = True

    return {
        "layer_id": layer_id,
        "parent_layer_id": parent_layer_id,
        "path": rel_s,
        "path_kind": kind,
        "same": code == 0,
        "diff": diff_text if code != 0 else "",
        "truncated": truncated,
    }


def diff_layer_worktree_vs_parent(parent_layer_id: str, layer_id: str) -> dict[str, Any]:
    """对比子层工作区目录与父层目录（排除 ``.git``），使用系统 ``diff -ruN``。"""
    _ensure_layer_id(layer_id)
    _ensure_layer_id(parent_layer_id)
    if parent_layer_id == layer_id:
        raise HTTPException(status_code=400, detail="invalid parent/child pair")

    parent_root = layer_git_workspace_root(parent_layer_id).resolve()
    child_root = layer_git_workspace_root(layer_id).resolve()
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
        out = (
            out[:_MAX_PARENT_DIFF_CHARS]
            + "\n\n…（输出已截断，可在本地对两层目录执行 diff -ruN -x .git）"
        )
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
    root = layer_git_workspace_root(layer_id)
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
