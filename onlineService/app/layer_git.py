"""Git helpers for writable layers (list branches, checkout, commit)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException

from trae_agent.utils.auto_commit_message import load_latest_trajectory_data

from .git_clone import _validate_branch
from .layer_merge import layer_merged_root_for_api, lower_paths_full_tip
from .layer_meta import is_overlay_v1_layer
from .layers import layer_path
from .overlay_diff import materialize_merged_chain
from .paths import runtime_dir


def layer_git_workspace_root(layer_id: str) -> Path:
    """git 工作区根：Overlay 层为物化合并视图（含 base + diff 链）。"""
    if is_overlay_v1_layer(layer_id):
        return layer_merged_root_for_api(layer_id)
    return layer_path(layer_id)


@contextlib.contextmanager
def _layer_compare_workspace_root(layer_id: str):
    """对比场景下的稳定工作区根。

    Overlay 层使用本次请求独占的物化快照，避免共享 materialized 目录在并发重建时被删改。
    """
    if not is_overlay_v1_layer(layer_id):
        yield layer_path(layer_id).resolve()
        return
    cmp_root = runtime_dir() / "materialized_compare"
    cmp_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"{layer_id}_", dir=str(cmp_root)))
    try:
        materialize_merged_chain(lower_paths_full_tip(layer_id), tmp)
        yield tmp.resolve()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_LAYER_ID_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})_(?P<suf>[0-9a-fA-F]+)$")
_SKIP_PARTS = {".git", "__pycache__", ".DS_Store"}
log = logging.getLogger(__name__)

_runtime_git_identity: dict[str, str] = {}


def _ensure_layer_id(layer_id: str) -> None:
    if not layer_id or not _LAYER_ID_RE.match(layer_id):
        raise HTTPException(status_code=400, detail="invalid layer_id")


def set_runtime_git_identity(name: str, email: str) -> dict[str, str]:
    clean_name = str(name or "").strip()
    clean_email = str(email or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="git identity name is empty")
    if not clean_email:
        raise HTTPException(status_code=400, detail="git identity email is empty")
    if "@" not in clean_email:
        raise HTTPException(status_code=400, detail="git identity email is invalid")
    _runtime_git_identity["name"] = clean_name
    _runtime_git_identity["email"] = clean_email
    return {"name": clean_name, "email": clean_email}


def get_runtime_git_identity() -> dict[str, str]:
    name = str(_runtime_git_identity.get("name") or "").strip()
    email = str(_runtime_git_identity.get("email") or "").strip()
    if not name or not email:
        return {"name": "", "email": ""}
    return {"name": name, "email": email}


def _dir_has_git_metadata(p: Path) -> bool:
    g = p / ".git"
    try:
        return g.exists() and (g.is_dir() or g.is_file())
    except OSError:
        return False


def _discover_git_workspace_roots(layer_id: str) -> list[Path]:
    """定位该层下所有 git 工作区根（层根/base/子目录仓库）。"""
    root = layer_git_workspace_root(layer_id)
    if not root.is_dir():
        return []

    candidates: list[Path] = []
    seen: set[Path] = set()
    for base in (root, root / "base"):
        try:
            base_res = base.resolve()
        except OSError:
            continue
        if base_res in seen or not base_res.is_dir():
            continue
        seen.add(base_res)
        candidates.append(base_res)

    repos: list[Path] = []
    repo_seen: set[Path] = set()

    def add_repo(repo: Path) -> None:
        try:
            rr = repo.resolve()
        except OSError:
            return
        if rr in repo_seen:
            return
        repo_seen.add(rr)
        repos.append(rr)

    for base in candidates:
        if _dir_has_git_metadata(base):
            add_repo(base)

    for base in candidates:
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_dir() or child.name in _SKIP_PARTS:
                    continue
            except OSError:
                continue
            if _dir_has_git_metadata(child):
                add_repo(child)

    if len(repos) > 1:
        log.info(
            "multiple git repos detected under layer %s, count=%s",
            layer_id,
            len(repos),
        )
    return repos


def _resolve_git_workspace_root(layer_id: str) -> Path | None:
    repos = _discover_git_workspace_roots(layer_id)
    if not repos:
        return None
    return repos[0]


def git_worktree_dirty(layer_id: str) -> bool | None:
    """``True``：存在未暂存或未提交变更；``False``：工作区干净；``None``：非 git 或检测失败。"""
    if not layer_id or not _LAYER_ID_RE.match(layer_id):
        return None
    roots = _discover_git_workspace_roots(layer_id)
    if not roots:
        return None
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    any_clean = False
    for root in roots:
        try:
            r = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
            if r.returncode != 0:
                continue
            any_clean = True
            if (r.stdout or "").strip():
                return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    if any_clean:
        return False
    return None


async def list_branches(layer_id: str) -> dict:
    """Return local + remote branch short names and current branch if any."""
    _ensure_layer_id(layer_id)
    base_root = layer_git_workspace_root(layer_id)
    if not base_root.is_dir():
        raise HTTPException(status_code=404, detail="layer not found")
    root = _resolve_git_workspace_root(layer_id)
    if root is None:
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


def _repo_label_for_message(repo_root: Path, layer_root: Path) -> str:
    try:
        rel = repo_root.resolve().relative_to(layer_root.resolve()).as_posix()
        return rel if rel and rel != "." else "layer-root"
    except ValueError:
        return repo_root.name or "repo"


def _format_multi_repo_commit_message(
    *,
    overall_goal: str,
    repo_label: str,
    shortstat: str,
    files: list[str],
) -> str:
    goal = (overall_goal or "").strip() or "完成当前任务目标"
    goal_line = goal.replace("\r\n", "\n").replace("\r", "\n")
    if len(goal_line) > 240:
        goal_line = goal_line[:239] + "…"
    subject = f"chore({repo_label}): {goal_line.splitlines()[0]}"
    if len(subject) > 72:
        subject = subject[:71] + "…"
    lines = [
        subject,
        "",
        "【总体目标】",
        goal,
        "",
        "【当前仓库完成内容】",
        f"- 仓库: {repo_label}",
        f"- 变更统计: {(shortstat or '—').strip()}",
    ]
    if files:
        lines.append("- 关键文件:")
        for p in files[:30]:
            lines.append(f"  - {p}")
        if len(files) > 30:
            lines.append(f"  - … 另有 {len(files) - 30} 个文件")
    msg = "\n".join(lines)
    if len(msg) > _MAX_COMMIT_MSG_LEN:
        msg = msg[: _MAX_COMMIT_MSG_LEN - 12].rstrip() + "\n…（已截断）"
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
    * ``message`` 为空：根据 ``command_hint``、最新轨迹 JSON（若存在）与暂存区统计自动生成说明。
    """
    _ensure_layer_id(layer_id)
    base_root = layer_git_workspace_root(layer_id)
    if not base_root.is_dir():
        raise HTTPException(status_code=404, detail="layer not found")
    roots = _discover_git_workspace_roots(layer_id)
    if not roots:
        raise HTTPException(status_code=400, detail="layer has no git repo")

    explicit = (message or "").strip()

    runtime_identity = get_runtime_git_identity()
    author_name = (
        runtime_identity.get("name")
        or os.environ.get("TRAE_GIT_COMMITTER_NAME")
        or "trae-online-service"
    ).strip() or "trae-online-service"
    author_email = (
        runtime_identity.get("email")
        or os.environ.get("TRAE_GIT_COMMITTER_EMAIL")
        or "trae-online@local"
    ).strip() or "trae-online@local"

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    cenv = {
        **env,
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    }
    trajectory = load_latest_trajectory_data(base_root)
    traj_task = ""
    if trajectory:
        raw_task = trajectory.get("task")
        if isinstance(raw_task, str):
            traj_task = raw_task.strip()
    overall_goal = (
        _strip_explicit_commit_message(explicit)
        if explicit
        else (str(command_hint or "").strip() or traj_task or "完成当前任务目标")
    )

    results: list[dict[str, Any]] = []
    all_outputs: list[str] = []
    for root in roots:
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
            continue

        stat_text, code_s = await _git_diff_cached_text(root, env, "--stat")
        if code_s != 0:
            raise HTTPException(
                status_code=400, detail=f"git diff --cached --stat failed:\n{stat_text.strip()}"
            )
        shortstat, code_ss = await _git_diff_cached_text(root, env, "--shortstat")
        if code_ss != 0:
            shortstat = ""

        repo_label = _repo_label_for_message(root, base_root)
        msg = _format_multi_repo_commit_message(
            overall_goal=overall_goal,
            repo_label=repo_label,
            shortstat=shortstat,
            files=files,
        )

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
                continue
            raise HTTPException(status_code=400, detail=f"git commit failed:\n{out_commit.strip()}")
        all_outputs.append(out_commit.strip())
        results.append(
            {
                "repo": repo_label,
                "files": files,
                "shortstat": (shortstat or "").strip(),
                "commit_message": msg,
                "output": out_commit.strip(),
            }
        )

    if not results:
        return {
            "layer_id": layer_id,
            "status": "noop",
            "commit_message": None,
            "detail": "所有仓库均无变更，未创建提交",
            "output": "",
            "commit_results": [],
        }

    return {
        "layer_id": layer_id,
        "status": "ok",
        "commit_message": overall_goal,
        "output": "\n\n".join(x for x in all_outputs if x),
        "commit_results": results,
    }


def git_ahead_of_upstream(layer_id: str) -> dict[str, Any]:
    """当前层所有 git 仓库相对上游的领先提交信息。"""
    _ensure_layer_id(layer_id)
    roots = _discover_git_workspace_roots(layer_id)
    if not roots:
        return {
            "is_git": False,
            "ahead": None,
            "branch": None,
            "upstream": None,
            "no_upstream": None,
            "repos": [],
        }

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    repos: list[dict[str, Any]] = []
    for root in roots:
        repo_label = _repo_label_for_message(root, layer_git_workspace_root(layer_id))
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
            repos.append(
                {
                    "repo": repo_label,
                    "branch": branch,
                    "upstream": None,
                    "ahead": None,
                    "no_upstream": True,
                }
            )
            continue

        upstream = r_u.stdout.strip()
        r_cnt = subprocess.run(
            ["git", "rev-list", "--count", f"{upstream}..HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        ahead: int | None = None
        if r_cnt.returncode == 0:
            try:
                ahead = int((r_cnt.stdout or "").strip())
            except ValueError:
                ahead = None
        repos.append(
            {
                "repo": repo_label,
                "branch": branch,
                "upstream": upstream,
                "ahead": ahead,
                "no_upstream": False,
            }
        )

    ahead_values = [int(x["ahead"]) for x in repos if isinstance(x.get("ahead"), int)]
    ahead = max(ahead_values) if ahead_values else None
    first = repos[0] if repos else {}
    return {
        "is_git": True,
        "ahead": ahead,
        "branch": first.get("branch"),
        "upstream": first.get("upstream"),
        "no_upstream": any(bool(x.get("no_upstream")) for x in repos),
        "repos": repos,
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

    with (
        _layer_compare_workspace_root(parent_layer_id) as parent_root,
        _layer_compare_workspace_root(layer_id) as child_root,
    ):
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

    with (
        _layer_compare_workspace_root(parent_layer_id) as parent_root,
        _layer_compare_workspace_root(layer_id) as child_root,
    ):
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

    with (
        _layer_compare_workspace_root(parent_layer_id) as parent_root,
        _layer_compare_workspace_root(layer_id) as child_root,
    ):
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


_GITHUB_REMOTE_RE = re.compile(
    r"^(?:https://(?:[^/@]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def _normalize_github_auth(github_auth: list[dict[str, str]] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not github_auth:
        return out
    for item in github_auth:
        if not isinstance(item, dict):
            continue
        repo = str(item.get("repo") or "").strip().lower()
        token = str(item.get("token") or "").strip()
        if repo and token:
            out[repo] = token
    return out


def _remote_origin_url(root: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _parse_github_repo_slug(remote_url: str) -> str | None:
    raw = str(remote_url or "").strip()
    if not raw:
        return None
    m = _GITHUB_REMOTE_RE.match(raw)
    if not m:
        return None
    owner = str(m.group("owner") or "").strip()
    repo = str(m.group("repo") or "").strip()
    if not owner or not repo:
        return None
    return f"{owner}/{repo}".lower()


def _build_github_push_url(repo_slug: str, token: str) -> str:
    owner, repo = repo_slug.split("/", 1)
    token_q = quote(token, safe="")
    return f"https://x-access-token:{token_q}@github.com/{owner}/{repo}.git"


def _redact_tokens(text: str, secrets: list[str]) -> str:
    out = str(text or "")
    for token in secrets:
        if token:
            out = out.replace(token, "***")
    return out


async def push_layer_worktree(
    layer_id: str,
    target_branch: str | None = None,
    github_auth: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """将当前层下所有 git 仓库推送到上游；可指定 ``target_branch``。"""
    _ensure_layer_id(layer_id)
    base_root = layer_git_workspace_root(layer_id)
    if not base_root.is_dir():
        raise HTTPException(status_code=404, detail="layer not found")
    roots = _discover_git_workspace_roots(layer_id)
    if not roots:
        raise HTTPException(status_code=400, detail="layer has no git repo")

    branch = str(target_branch or "").strip()
    if branch and any(ch.isspace() for ch in branch):
        raise HTTPException(status_code=400, detail="invalid target_branch")

    github_auth_map = _normalize_github_auth(github_auth)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    results: list[dict[str, Any]] = []
    outputs: list[str] = []
    for root in roots:
        repo_label = _repo_label_for_message(root, base_root)
        remote_origin = _remote_origin_url(root)
        github_repo = _parse_github_repo_slug(remote_origin)
        push_remote = "origin"
        redaction_secrets: list[str] = []
        if github_repo:
            token = github_auth_map.get(github_repo)
            if token:
                push_remote = _build_github_push_url(github_repo, token)
                redaction_secrets.append(token)
        cmd = ["git", "push"]
        if branch:
            cmd.extend([push_remote, f"HEAD:{branch}"])
        elif push_remote != "origin":
            cmd.append(push_remote)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        assert proc.stdout is not None
        out_b = await proc.stdout.read()
        code = await proc.wait()
        out = _redact_tokens(out_b.decode(errors="replace").strip(), redaction_secrets)
        if code != 0:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "git push failed",
                    "repo": repo_label,
                    "exit_code": code,
                    "output": out,
                },
            )
        outputs.append(out)
        results.append({"repo": repo_label, "output": out})

    return {
        "layer_id": layer_id,
        "status": "ok",
        "target_branch": branch or None,
        "output": "\n\n".join(x for x in outputs if x),
        "push_results": results,
    }
