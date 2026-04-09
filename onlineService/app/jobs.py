"""Job registry, persistence, and trae-cli subprocess execution."""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import sys
import shutil
import signal
import subprocess
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .hub import hub
from .layer_fs import any_layer_has_git_repo
from .layer_meta import is_overlay_v1_layer, read_layer_meta
from .layer_merge import (
    finalize_job_overlay_finished,
    interrupt_job_overlay,
    prepare_job_run,
)
from .online_project_view import clear_online_project_tip, set_online_project_tip
from .layer_git import git_checkout
from .layers import (
    _LAYER_ID_RE,
    cleanup_layers,
    create_job_layer,
    create_root_layer,
    create_stacked_layer,
    layer_path,
    new_layer_id,
)
from .paths import (
    commands_log_path,
    config_file_path,
    jobs_state_path,
    layers_root,
    repo_root,
    venv_activate_path,
)

log = logging.getLogger(__name__)

JobStatus = Literal["pending", "running", "completed", "failed", "interrupted"]
CommandKind = Literal["trae", "shell", "clone"]

_GIT_LOCK_MSG = (
    "该任务可写层在运行开始后已有 git 提交（HEAD 已变化），已禁止中断、重新执行与删除。"
)


def _git_rev_parse_head_sync(workdir: Path) -> str | None:
    if not (workdir / ".git").exists():
        return None
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if r.returncode != 0:
            return None
        out = (r.stdout or "").strip()
        return out or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_workdir_for_lock_probe(rec: JobRecord) -> Path:
    """与 ``_run_job`` 记录 ``git_head_at_run_start`` 时使用同一 git 工作区。

    Overlay v1 任务层的工作区在 ``<layer>/merged``，层根目录通常没有 ``.git``；
    克隆层工作区在 ``<layer>/base``（``.git`` 在 base 内）。
    """
    root = Path(rec.layer_path)
    if not root.is_dir():
        return root
    if not is_overlay_v1_layer(rec.layer_id):
        return root
    meta = read_layer_meta(rec.layer_id)
    if meta and meta.kind == "clone":
        base = root / "base"
        if base.is_dir() and (base / ".git").exists():
            return base
        return root
    if meta is None or meta.kind != "job":
        return root
    merged = root / "merged"
    if merged.is_dir() and (merged / ".git").exists():
        return merged
    return root


# Rich 需要有限列宽，无法用「无限」；用极大值近似不折行，避免长路径/JSON 在管道下被 80 列切碎。
# TRAE_JOB_COLUMNS=0 / unlimited / max 等均表示使用该默认值；也可设具体正整数（上限见实现）。
_DEFAULT_JOB_WIDE_COLUMNS = 999_999
_MAX_JOB_COLUMNS = 9_999_999

# 按块读取 stdout，减少「一行一条 SSE」的风暴（仍保持 UTF-8 边界正确）。


def _stdout_chunk_bytes() -> int:
    raw = os.environ.get("TRAE_JOB_STDOUT_CHUNK_BYTES", "16384")
    try:
        # 下限避免过小导致频繁 await；测试可用 64 验证多分块
        return max(int(raw), 64)
    except ValueError:
        return 16384


async def _iter_stdout_text(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """Decode subprocess stdout in fixed-size binary chunks (not line-based)."""
    chunk_sz = _stdout_chunk_bytes()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        block = await stream.read(chunk_sz)
        if not block:
            tail = decoder.decode(b"", final=True)
            if tail:
                yield tail
            break
        text = decoder.decode(block)
        if text:
            yield text


def _job_subprocess_columns() -> str:
    raw = (os.environ.get("TRAE_JOB_COLUMNS") or "").strip().lower()
    if not raw or raw in ("0", "unlimited", "none", "max", "inf"):
        return str(_DEFAULT_JOB_WIDE_COLUMNS)
    if raw.isdigit():
        n = int(raw)
        if n <= 0:
            return str(_DEFAULT_JOB_WIDE_COLUMNS)
        return str(min(n, _MAX_JOB_COLUMNS))
    return str(_DEFAULT_JOB_WIDE_COLUMNS)


def _job_subprocess_env() -> dict[str, str]:
    base = {**os.environ, "PYTHONUNBUFFERED": "1"}
    base["COLUMNS"] = _job_subprocess_columns()
    if not base.get("PYTHONPATH"):
        # 允许在未安装 console script 时回退到 `python -m trae_agent.cli`
        base["PYTHONPATH"] = str(repo_root())
    return base


def _venv_python_path() -> Path:
    activate = venv_activate_path()
    return activate.parent / "python"


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _build_shell_cmd(script: str) -> list[str]:
    """在工作区目录下以 login shell 执行用户给出的整条命令行文本。"""
    return ["bash", "-lc", script]


def _build_trae_run_cmd(cfg: Path, work: str, cmd_text: str) -> list[str]:
    activate = venv_activate_path()
    trae_bin = activate.parent / "trae-cli"
    py = _venv_python_path()
    py3 = activate.parent / "python3"
    if _is_executable_file(py):
        base = [str(py), "-m", "trae_agent.cli"]
    elif _is_executable_file(py3):
        base = [str(py3), "-m", "trae_agent.cli"]
    elif _is_executable_file(trae_bin):
        # 最后兜底：某些环境仅有 entrypoint 脚本
        base = [str(trae_bin)]
    else:
        base = [sys.executable, "-m", "trae_agent.cli"]
    return [
        *base,
        "run",
        cmd_text,
        f"--config-file={str(cfg)}",
        f"--working-dir={work}",
    ]


@dataclass
class JobRecord:
    id: str
    layer_id: str
    layer_path: str
    command: str
    parent_job_id: str | None
    status: JobStatus
    created_at: str
    exit_code: int | None = None
    output: str = ""
    git_branch: str | None = None
    repo_layer_id: str | None = None  # 无父任务时从该层复制工作区
    git_head_at_run_start: str | None = None  # 本次 trae-cli 启动前 HEAD（用于检测是否已有新提交）
    command_kind: CommandKind = "trae"  # shell：原样 bash -lc，不经 trae-cli

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _job_command_head(rec: JobRecord | None, max_len: int = 56) -> str:
    if rec is None:
        return ""
    cmd = (rec.command or "").strip()
    if len(cmd) <= max_len:
        return cmd
    return cmd[: max_len - 1] + "…"


def sse_job_event(
    event_type: str,
    rec: JobRecord | None,
    job_id: str,
    *,
    extra_title: str | None = None,
) -> dict[str, Any]:
    """轻量 SSE：仅 job_id + 标题，正文由前端按 ID 拉 REST。"""
    jid = job_id if rec is None else rec.id
    head = _job_command_head(rec)
    titles = {
        "job_created": "新任务已创建",
        "job_started": "任务已开始",
        "job_output": "控制台输出更新",
        "job_finished": "任务已结束",
        "job_interrupt_requested": "已请求中断",
        "job_redone": "已重新执行（层已重建）",
        "job_continued": "已继续执行",
    }
    base = titles.get(event_type, event_type)
    if extra_title:
        title = extra_title
    elif head and event_type == "job_created":
        title = f"{base} · {head}"
    elif event_type == "job_finished" and rec is not None:
        title = f"{base} · {rec.status}" + (f" · {head}" if head else "")
    else:
        title = base
    return {"type": event_type, "job_id": jid, "title": title}


def job_layer_git_destructive_locked(rec: JobRecord) -> bool:
    """相对任务开始执行时所记录 HEAD，若当前 HEAD 不同则视为已有新提交，锁定破坏性操作。

    无 baseline 时不再兼容旧状态文件：除仍处于 pending、或已 failed（如 checkout 失败未记下 HEAD）外，一律锁定。
    """
    if rec.status == "pending":
        return False
    baseline = (rec.git_head_at_run_start or "").strip()
    if not baseline:
        return rec.status != "failed"
    work = _git_workdir_for_lock_probe(rec)
    if not work.is_dir():
        return False
    cur = _git_rev_parse_head_sync(work)
    if not cur:
        return True
    return cur != baseline


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._layer_queues: dict[str, list[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._creation_lock = asyncio.Lock()
        self._running: dict[str, asyncio.subprocess.Process] = {}
        self._runner_tasks: dict[str, asyncio.Task[None]] = {}
        self._load()

    async def _wait_until_layer_idle(self, layer_id: str) -> None:
        """在叠层复制前等待：该 layer 上无排队/执行中的任务（叠加指令串行化）。"""
        while True:
            async with self._lock:
                if not self._active_job_for_layer(layer_id):
                    return
            await asyncio.sleep(0.08)

    async def _force_remove_job(self, job_id: str) -> None:
        """放弃分支时强制移除任务与其可写层（不校验 git 锁/子任务顺序由调用方保证）。"""
        async with self._lock:
            rec = self._jobs.get(job_id)
            if not rec:
                return
            runner_task = self._runner_tasks.get(job_id)

        if rec.status == "running" and runner_task and not runner_task.done():
            proc = self._running.get(job_id)
            if proc:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    proc.terminate()
        elif rec.status == "pending" and runner_task and not runner_task.done():
            runner_task.cancel()
            try:
                await runner_task
            except asyncio.CancelledError:
                pass
        elif runner_task and not runner_task.done():
            try:
                await asyncio.wait_for(runner_task, timeout=120.0)
            except TimeoutError:
                runner_task.cancel()
                try:
                    await runner_task
                except asyncio.CancelledError:
                    pass

        layer_path_obj = Path(rec.layer_path)
        async with self._lock:
            self._jobs.pop(job_id, None)
            self._running.pop(job_id, None)
            self._runner_tasks.pop(job_id, None)

        if self._is_managed_layer_dir(layer_path_obj) and layer_path_obj.is_dir():
            await asyncio.to_thread(shutil.rmtree, layer_path_obj, True)

    def _active_job_for_layer(self, layer_id: str) -> JobRecord | None:
        """若有任务正在该 layer_id 对应可写目录上排队或执行，返回该任务。"""
        for j in self._jobs.values():
            if j.layer_id == layer_id and j.status in ("pending", "running"):
                return j
        return None

    def _job_for_layer(self, layer_id: str) -> JobRecord | None:
        """每个可写层至多一条任务记录（layer_id 唯一）。"""
        for j in self._jobs.values():
            if j.layer_id == layer_id:
                return j
        return None

    def layer_queue_depth(self, layer_id: str) -> int:
        return len(self._layer_queues.get(layer_id, []))

    def _post_order_job_ids(self, job_id: str) -> list[str]:
        out: list[str] = []
        for cid in self._child_job_ids(job_id):
            out.extend(self._post_order_job_ids(cid))
        out.append(job_id)
        return out

    def _load(self) -> None:
        p = jobs_state_path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for row in data.get("jobs", []):
                row = {**row}
                row.setdefault("command_kind", "trae")
                if row.get("command_kind") not in ("trae", "shell", "clone"):
                    row["command_kind"] = "trae"
                rec = JobRecord(**row)
                if rec.status == "running":
                    rec.status = "interrupted"
                self._jobs[rec.id] = rec
            raw_q = data.get("layer_queues") or {}
            self._layer_queues = {}
            if isinstance(raw_q, dict):
                for k, v in raw_q.items():
                    if not isinstance(k, str) or not isinstance(v, list):
                        continue
                    norm: list[dict[str, Any]] = []
                    for item in v:
                        if isinstance(item, dict) and item.get("command"):
                            ck = item.get("command_kind", "trae")
                            if ck not in ("trae", "shell"):
                                ck = "trae"
                            norm.append(
                                {"command": str(item["command"]), "command_kind": ck}
                            )
                        elif isinstance(item, str) and item.strip():
                            norm.append({"command": item.strip(), "command_kind": "trae"})
                    if norm:
                        self._layer_queues[k] = norm
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    def _save_sync(self) -> None:
        p = jobs_state_path()
        payload: dict[str, Any] = {
            "jobs": [j.to_dict() for j in self._jobs.values()],
            "layer_queues": {k: v for k, v in self._layer_queues.items() if v},
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _save(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_sync)

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[JobRecord]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    async def create_job(
        self,
        command: str,
        parent_job_id: str | None,
        *,
        repo_layer_id: str | None = None,
        git_branch: str | None = None,
        command_kind: CommandKind = "trae",
    ) -> JobRecord:
        from datetime import datetime

        cfg = config_file_path()
        act = venv_activate_path()
        if command_kind == "trae":
            if not cfg.is_file():
                raise FileNotFoundError(
                    f"Config missing: {cfg}. Push config via POST /api/config first."
                )
            if not act.is_file():
                raise FileNotFoundError(f"venv activate script not found: {act}")
        elif command_kind != "shell":
            raise ValueError(f"invalid command_kind: {command_kind!r}")

        if not any_layer_has_git_repo():
            raise ValueError("请先完成「克隆仓库」后再创建任务。")

        if parent_job_id and repo_layer_id:
            raise ValueError("repo_layer_id is only valid when parent_job_id is empty")
        if git_branch and not parent_job_id and not repo_layer_id:
            raise ValueError("git_branch requires parent_job_id or repo_layer_id")

        async with self._creation_lock:
            stack_parent: str | None = None

            async with self._lock:
                if parent_job_id:
                    parent_rec = self._jobs.get(parent_job_id)
                    if not parent_rec:
                        raise ValueError(f"parent_job_id not found: {parent_job_id}")
                    stack_parent = str(parent_rec.layer_id)
                elif repo_layer_id:
                    src = layer_path(repo_layer_id)
                    if not src.is_dir():
                        raise ValueError(f"repo_layer_id not found: {repo_layer_id}")
                    stack_parent = str(repo_layer_id)
                else:
                    stack_parent = None

            if stack_parent:
                await self._wait_until_layer_idle(stack_parent)

            layer_id = new_layer_id()
            if parent_job_id:
                if stack_parent is None:
                    raise ValueError("内部错误：未解析父层 layer_id")
                lp = create_job_layer(layer_id, stack_parent)
            elif repo_layer_id:
                if stack_parent is None:
                    raise ValueError("内部错误：未解析父层 layer_id")
                lp = create_job_layer(layer_id, stack_parent)
            else:
                lp = create_root_layer(layer_id)

            jid = str(uuid4())
            rec = JobRecord(
                id=jid,
                layer_id=layer_id,
                layer_path=str(lp),
                command=command,
                parent_job_id=parent_job_id,
                status="pending",
                created_at=datetime.now().isoformat(timespec="seconds"),
                git_branch=git_branch,
                repo_layer_id=repo_layer_id if not parent_job_id else None,
                command_kind=command_kind,
            )
            async with self._lock:
                self._jobs[jid] = rec
            await self._save()
            await hub.publish(sse_job_event("job_created", rec, rec.id))
            task = asyncio.create_task(self._run_job(jid))
            self._runner_tasks[jid] = task
            task.add_done_callback(
                lambda _t, job_id=jid: self._runner_tasks.pop(job_id, None)
            )
            return rec

    async def register_clone_layer_job(
        self,
        layer_id: str,
        *,
        command: str,
        output: str = "",
    ) -> JobRecord | None:
        """克隆层就绪后登记一条已完成任务（不启动子进程）；每层至多一条。"""
        from datetime import datetime

        lid = (layer_id or "").strip()
        if not lid:
            raise ValueError("empty layer_id")

        lp = layer_path(lid)
        if not lp.is_dir():
            raise ValueError(f"layer not found: {lid!r}")

        async with self._creation_lock:
            async with self._lock:
                if self._job_for_layer(lid):
                    return self._job_for_layer(lid)

            probe_rec = JobRecord(
                id="",
                layer_id=lid,
                layer_path=str(lp.resolve()),
                command=command,
                parent_job_id=None,
                status="completed",
                created_at="",
                command_kind="clone",
            )
            git_head = await asyncio.to_thread(
                _git_rev_parse_head_sync,
                _git_workdir_for_lock_probe(probe_rec),
            )

            async with self._lock:
                if self._job_for_layer(lid):
                    return self._job_for_layer(lid)
                jid = str(uuid4())
                rec = JobRecord(
                    id=jid,
                    layer_id=lid,
                    layer_path=str(lp.resolve()),
                    command=command,
                    parent_job_id=None,
                    status="completed",
                    created_at=datetime.now().isoformat(timespec="seconds"),
                    exit_code=0,
                    output=output or "",
                    repo_layer_id=None,
                    command_kind="clone",
                    git_head_at_run_start=git_head,
                )
                self._jobs[jid] = rec
            await self._save()

        await hub.publish(sse_job_event("job_created", rec, rec.id))
        return rec

    async def _run_job(
        self,
        job_id: str,
        *,
        run_command: str | None = None,
        preserve_output: bool = False,
    ) -> None:
        rec = self._jobs.get(job_id)
        if not rec:
            return
        cfg = config_file_path()
        cmd_text = run_command if run_command is not None else rec.command

        overlay_prepared = False

        async def _finalize_overlay_if_needed() -> None:
            if not overlay_prepared:
                return
            if rec.status == "interrupted":
                await asyncio.to_thread(interrupt_job_overlay, rec.layer_id)
            else:
                await asyncio.to_thread(finalize_job_overlay_finished, rec.layer_id)

        work_dir = Path(rec.layer_path)
        if is_overlay_v1_layer(rec.layer_id):
            try:
                work_dir = await asyncio.to_thread(
                    prepare_job_run,
                    rec.layer_id,
                    reuse_upper=preserve_output,
                )
                overlay_prepared = True
            except Exception as e:
                rec.status = "failed"
                rec.output = f"overlay prepare failed: {e}\n"
                rec.exit_code = -1
                async with self._lock:
                    self._layer_queues.pop(rec.layer_id, None)
                await self._save()
                await hub.publish(sse_job_event("job_finished", rec, job_id))
                return

        work = str(work_dir)
        if rec.command_kind == "shell":
            cmd = _build_shell_cmd(cmd_text)
        else:
            cmd = _build_trae_run_cmd(cfg, work, cmd_text)

        if rec.git_branch and not preserve_output:
            co_out, co_code = await git_checkout(work_dir, rec.git_branch)
            banner = f"[git checkout {rec.git_branch}]\n{co_out}"
            rec.output = banner
            await self._save()
            await hub.publish(sse_job_event("job_output", rec, job_id))
            if co_code != 0:
                rec.status = "failed"
                rec.exit_code = co_code
                async with self._lock:
                    self._layer_queues.pop(rec.layer_id, None)
                await self._save()
                await hub.publish(sse_job_event("job_finished", rec, job_id))
                await _finalize_overlay_if_needed()
                try:
                    await asyncio.to_thread(set_online_project_tip, rec.layer_id)
                except Exception:
                    log.exception("set_online_project_tip after checkout failure")
                return

        head0 = await asyncio.to_thread(_git_rev_parse_head_sync, work_dir)
        rec.git_head_at_run_start = head0
        await self._save()

        rec.status = "running"
        if preserve_output:
            rec.output += "\n\n--- 继续执行（指令：继续）---\n"
        elif not (rec.git_branch and rec.output.strip()):
            rec.output = ""
        await self._save()
        await hub.publish(sse_job_event("job_started", rec, job_id))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(work_dir),
                env=_job_subprocess_env(),
                start_new_session=True,
            )
        except Exception as e:
            rec.status = "failed"
            err_line = f"spawn error: {e} (cmd={cmd[0]!r}, cwd={work!r})\n"
            rec.output = err_line if not preserve_output else rec.output + err_line
            rec.exit_code = -1
            async with self._lock:
                self._layer_queues.pop(rec.layer_id, None)
            await self._save()
            await hub.publish(sse_job_event("job_finished", rec, job_id))
            await _finalize_overlay_if_needed()
            try:
                await asyncio.to_thread(set_online_project_tip, rec.layer_id)
            except Exception:
                log.exception("set_online_project_tip after spawn failure")
            return

        self._running[job_id] = proc
        assert proc.stdout is not None
        try:
            async for text in _iter_stdout_text(proc.stdout):
                rec.output += text
                await hub.publish(sse_job_event("job_output", rec, job_id))
            code = await proc.wait()
            rec.exit_code = code
            if job_id in self._running:
                del self._running[job_id]
            if rec.status == "running":
                if code is not None and code < 0:
                    rec.status = "interrupted"
                elif code == 0:
                    rec.status = "completed"
                else:
                    rec.status = "failed"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            rec.status = "failed"
            rec.output += f"\n[runner error] {e}\n"
            rec.exit_code = rec.exit_code if rec.exit_code is not None else -1
        finally:
            if job_id in self._running:
                del self._running[job_id]
            if rec.status in ("failed", "interrupted") and rec.layer_id:
                async with self._lock:
                    self._layer_queues.pop(rec.layer_id, None)
            await self._save()
            await hub.publish(sse_job_event("job_finished", rec, job_id))
            await _finalize_overlay_if_needed()
            try:
                await asyncio.to_thread(set_online_project_tip, rec.layer_id)
            except Exception:
                log.exception("set_online_project_tip after job finished")

        rec_done = self._jobs.get(job_id)
        if rec_done and rec_done.status == "completed":
            asyncio.create_task(self._maybe_drain_layer_queue(job_id))

    async def _maybe_drain_layer_queue(self, finished_job_id: str) -> None:
        """当前层任务成功完成后：从该层待执行队列取第一条，基于本任务新建子层执行；其余队列挂到新层。"""
        try:
            async with self._lock:
                fin = self._jobs.get(finished_job_id)
                if not fin or fin.status != "completed":
                    return
                layer_id = fin.layer_id
                q = self._layer_queues.get(layer_id)
                if not q:
                    return
                first = q[0]
                rest = q[1:]
                saved_snapshot = list(q)
                self._layer_queues.pop(layer_id, None)
            cmd = str(first.get("command", "")).strip()
            kind: CommandKind = (
                first.get("command_kind", "trae")
                if first.get("command_kind") in ("trae", "shell")
                else "trae"
            )
            if not cmd:
                async with self._lock:
                    self._layer_queues[layer_id] = saved_snapshot
                await self._save()
                return
            try:
                new_rec = await self.create_job(
                    cmd,
                    finished_job_id,
                    command_kind=kind,
                )
            except Exception:
                async with self._lock:
                    self._layer_queues[layer_id] = saved_snapshot
                await self._save()
                raise
            async with self._lock:
                if rest:
                    self._layer_queues[new_rec.layer_id] = rest
            await self._save()
        except Exception:
            log.exception("drain layer queue after job %s", finished_job_id)

    async def enqueue_layer_command(
        self,
        layer_id: str,
        command: str,
        *,
        command_kind: CommandKind = "trae",
    ) -> dict[str, Any]:
        command = command.strip()
        if not command:
            raise ValueError("empty command")
        if command_kind not in ("trae", "shell"):
            raise ValueError("invalid command_kind")
        async with self._lock:
            j = self._active_job_for_layer(layer_id)
            if not j:
                raise ValueError("该层无排队或运行中的任务，无法加入待执行队列")
            self._layer_queues.setdefault(layer_id, []).append(
                {"command": command, "command_kind": command_kind}
            )
            depth = len(self._layer_queues[layer_id])
        await self._save()
        return {"layer_id": layer_id, "queue_depth": depth}

    async def delete_layer_by_layer_id(self, layer_id: str) -> dict[str, Any]:
        """删除可写层：运行中则先中断；含子任务时自底向上删除；纯克隆层则先删依赖任务再删目录。"""
        if not layer_id or not _LAYER_ID_RE.match(layer_id):
            raise ValueError("invalid layer_id")

        async with self._lock:
            self._layer_queues.pop(layer_id, None)

        job = self._job_for_layer(layer_id)
        if not job:
            roots = [j for j in self._jobs.values() if j.repo_layer_id == layer_id]
            for r in roots:
                for jid in self._post_order_job_ids(r.id):
                    await self.delete_job(jid)
            lp = layer_path(layer_id)
            if self._is_managed_layer_dir(lp) and lp.is_dir():
                await asyncio.to_thread(shutil.rmtree, lp, True)
            await self._save()
            await hub.publish(
                {
                    "type": "layer_deleted",
                    "layer_id": layer_id,
                    "title": "可写层已删除",
                }
            )
            return {"layer_id": layer_id, "status": "deleted"}

        for jid in self._post_order_job_ids(job.id):
            await self.delete_job(jid)
        await self._save()
        await hub.publish(
            {"type": "layer_deleted", "layer_id": layer_id, "title": "可写层已删除"}
        )
        return {"layer_id": layer_id, "job_id": job.id, "status": "deleted"}

    async def interrupt(self, job_id: str) -> bool:
        rec = self._jobs.get(job_id)
        if not rec or rec.status != "running":
            return False
        # 中断仅终止子进程，不依赖「层与基线 HEAD 一致」；HEAD 在长任务中可能因正常 git 操作变化，
        # 若与 redo/删除 共用 git 锁会导致「中断」按钮永久不可用。
        proc = self._running.get(job_id)
        if not proc:
            return False
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            proc.terminate()
        await hub.publish(sse_job_event("job_interrupt_requested", rec, job_id))
        return True

    async def redo_job(self, job_id: str) -> JobRecord:
        """删除该任务可写层并从创建时的来源重新复制，再执行同一指令。"""
        async with self._creation_lock:
            async with self._lock:
                rec = self._jobs.get(job_id)
                if not rec:
                    raise ValueError("Job not found")
                runner_task = self._runner_tasks.get(job_id)

            if rec.command_kind == "clone":
                raise ValueError(
                    "克隆层任务不支持重新执行，请使用「克隆仓库」新建可写层。"
                )

            if job_layer_git_destructive_locked(rec):
                raise ValueError(_GIT_LOCK_MSG)

            if rec.status == "running" and runner_task and not runner_task.done():
                _ = await self.interrupt(job_id)
            elif rec.status == "pending" and runner_task and not runner_task.done():
                runner_task.cancel()
                try:
                    await runner_task
                except asyncio.CancelledError:
                    pass
            elif runner_task and not runner_task.done():
                try:
                    await asyncio.wait_for(runner_task, timeout=120.0)
                except TimeoutError:
                    runner_task.cancel()
                    try:
                        await runner_task
                    except asyncio.CancelledError:
                        pass

            async with self._lock:
                rec = self._jobs.get(job_id)
                if not rec:
                    raise ValueError("Job not found")

                old_path = Path(rec.layer_path)
                if old_path.is_dir():
                    await asyncio.to_thread(shutil.rmtree, old_path, True)

                new_lid = new_layer_id()
                if rec.parent_job_id:
                    parent = self._jobs.get(rec.parent_job_id)
                    if not parent:
                        raise ValueError(
                            f"parent_job_id not found: {rec.parent_job_id}"
                        )
                    lp = create_job_layer(new_lid, str(parent.layer_id))
                elif rec.repo_layer_id:
                    src = layer_path(rec.repo_layer_id)
                    if not src.is_dir():
                        raise ValueError(
                            f"repo_layer_id not found: {rec.repo_layer_id}"
                        )
                    lp = create_job_layer(new_lid, str(rec.repo_layer_id))
                else:
                    lp = create_root_layer(new_lid)

                rec.layer_id = new_lid
                rec.layer_path = str(lp)
                rec.status = "pending"
                rec.output = ""
                rec.exit_code = None
                rec.git_head_at_run_start = None

            await self._save()
            await hub.publish(sse_job_event("job_redone", rec, job_id))

            task = asyncio.create_task(self._run_job(job_id))
            self._runner_tasks[job_id] = task
            task.add_done_callback(
                lambda _t, jid=job_id: self._runner_tasks.pop(jid, None)
            )
            return rec

    async def continue_job(self, job_id: str) -> JobRecord:
        """在中断状态下保留当前可写层，以指令「继续」重新执行 trae-cli。"""
        async with self._creation_lock:
            async with self._lock:
                rec = self._jobs.get(job_id)
                if not rec:
                    raise ValueError("Job not found")
                if rec.command_kind == "clone":
                    raise ValueError("克隆任务不支持「继续」。")
                if rec.command_kind == "shell":
                    raise ValueError("Shell 任务不支持「继续」，请使用重新执行。")
                if rec.status != "interrupted":
                    raise ValueError("Only interrupted jobs can be continued")
                runner_task = self._runner_tasks.get(job_id)
                if runner_task and not runner_task.done():
                    raise ValueError("Job runner is still active")
                rec.status = "pending"
                rec.exit_code = None

            await self._save()
            await hub.publish(sse_job_event("job_continued", rec, job_id))

            task = asyncio.create_task(
                self._run_job(job_id, run_command="继续", preserve_output=True)
            )
            self._runner_tasks[job_id] = task
            task.add_done_callback(
                lambda _t, jid=job_id: self._runner_tasks.pop(jid, None)
            )
            return rec

    async def reset_all(self) -> dict[str, Any]:
        """中断所有任务并清空任务列表。"""
        async with self._creation_lock:
            async with self._lock:
                all_job_ids = list(self._jobs.keys())
                running_items = list(self._running.items())
                runner_items = list(self._runner_tasks.items())

                # 让后续 job_finished 事件能反映“中断”
                for jid in all_job_ids:
                    rec = self._jobs.get(jid)
                    if rec and rec.status in ("pending", "running"):
                        rec.status = "interrupted"

                # 清空内存状态，确保 /api/jobs 立刻变空
                self._jobs.clear()
                self._layer_queues.clear()
                self._running.clear()
                self._runner_tasks.clear()

            # 取消所有 runner task（阻止未生成进程的任务启动）
            for _jid, task in runner_items:
                task.cancel()

            # 终止所有运行中的进程组
            for _jid, proc in running_items:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    proc.terminate()

            # 清理磁盘上的可写层（防止 reset 后 layers 仍残留）
            layers_stat = cleanup_layers()

            await self._save()

            def _unlink_commands_log() -> None:
                try:
                    commands_log_path().unlink(missing_ok=True)
                except OSError:
                    pass

            await asyncio.to_thread(_unlink_commands_log)
            await asyncio.to_thread(clear_online_project_tip)

            await hub.publish({"type": "jobs_reset", "title": "任务与可写层已重置"})
            return {
                "jobs_cleared": len(all_job_ids),
                "running_interrupted": len(running_items),
                "runner_tasks_cancelled": len(runner_items),
                "layers_removed": layers_stat.get("removed", 0),
            }

    def _child_job_ids(self, job_id: str) -> list[str]:
        return [j.id for j in self._jobs.values() if j.parent_job_id == job_id]

    def _is_managed_layer_dir(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
            root = layers_root().resolve()
            if resolved.parent != root:
                return False
        except OSError:
            return False
        return bool(_LAYER_ID_RE.match(resolved.name))

    async def delete_job(self, job_id: str) -> dict[str, Any]:
        """从列表中移除任务；若存在则删除其可写层目录。存在子任务时拒绝删除。"""
        async with self._lock:
            rec = self._jobs.get(job_id)
            if not rec:
                raise ValueError("Job not found")
            if self._child_job_ids(job_id):
                raise ValueError("存在子任务，请先删除子任务后再删除该任务")
            if job_layer_git_destructive_locked(rec):
                raise ValueError(_GIT_LOCK_MSG)
            runner_task = self._runner_tasks.get(job_id)

        if rec.status == "running" and runner_task and not runner_task.done():
            _ = await self.interrupt(job_id)
        elif rec.status == "pending" and runner_task and not runner_task.done():
            runner_task.cancel()
            try:
                await runner_task
            except asyncio.CancelledError:
                pass
        elif runner_task and not runner_task.done():
            try:
                await asyncio.wait_for(runner_task, timeout=120.0)
            except TimeoutError:
                runner_task.cancel()
                try:
                    await runner_task
                except asyncio.CancelledError:
                    pass

        layer_path_obj = Path(rec.layer_path)
        async with self._lock:
            if job_id not in self._jobs:
                raise ValueError("Job not found")
            if self._child_job_ids(job_id):
                raise ValueError("存在子任务，请先删除子任务后再删除该任务")
            self._layer_queues.pop(rec.layer_id, None)
            del self._jobs[job_id]
            self._running.pop(job_id, None)
            self._runner_tasks.pop(job_id, None)

        if self._is_managed_layer_dir(layer_path_obj) and layer_path_obj.is_dir():
            await asyncio.to_thread(shutil.rmtree, layer_path_obj, True)

        await self._save()
        await hub.publish(
            {"type": "job_deleted", "job_id": job_id, "title": "任务已删除"}
        )
        return {"job_id": job_id, "status": "deleted"}


store = JobStore()
