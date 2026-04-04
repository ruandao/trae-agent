"""Job registry, persistence, and trae-cli subprocess execution."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .hub import hub
from .layer_fs import any_layer_has_git_repo
from .layer_git import git_checkout
from .layers import cleanup_layers, create_root_layer, create_stacked_layer, layer_path, new_layer_id
from .paths import config_file_path, jobs_state_path, venv_activate_path


JobStatus = Literal["pending", "running", "completed", "failed", "interrupted"]


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

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()
        self._running: dict[str, asyncio.subprocess.Process] = {}
        self._runner_tasks: dict[str, asyncio.Task[None]] = {}
        self._load()

    def _load(self) -> None:
        p = jobs_state_path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for row in data.get("jobs", []):
                row.setdefault("git_branch", None)
                row.setdefault("repo_layer_id", None)
                rec = JobRecord(**row)
                if rec.status == "running":
                    rec.status = "interrupted"
                self._jobs[rec.id] = rec
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    def _save_sync(self) -> None:
        p = jobs_state_path()
        payload = {"jobs": [j.to_dict() for j in self._jobs.values()]}
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
    ) -> JobRecord:
        from datetime import datetime

        cfg = config_file_path()
        if not cfg.is_file():
            raise FileNotFoundError(
                f"Config missing: {cfg}. Push config via POST /api/config first."
            )
        act = venv_activate_path()
        if not act.is_file():
            raise FileNotFoundError(f"venv activate script not found: {act}")

        if not any_layer_has_git_repo():
            raise ValueError("请先完成「克隆仓库」后再创建任务。")

        if parent_job_id and repo_layer_id:
            raise ValueError("repo_layer_id is only valid when parent_job_id is empty")
        if git_branch and not parent_job_id and not repo_layer_id:
            raise ValueError("git_branch requires parent_job_id or repo_layer_id")

        layer_id = new_layer_id()
        if parent_job_id:
            parent_rec = self._jobs.get(parent_job_id)
            if not parent_rec:
                raise ValueError(f"parent_job_id not found: {parent_job_id}")
            lp = create_stacked_layer(layer_id, Path(parent_rec.layer_path))
        elif repo_layer_id:
            src = layer_path(repo_layer_id)
            if not src.is_dir():
                raise ValueError(f"repo_layer_id not found: {repo_layer_id}")
            lp = create_stacked_layer(layer_id, src.resolve())
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
        )
        async with self._lock:
            self._jobs[jid] = rec
        await self._save()
        await hub.publish({"type": "job_created", "job": rec.to_dict()})
        task = asyncio.create_task(self._run_job(jid))
        self._runner_tasks[jid] = task
        task.add_done_callback(lambda _t, job_id=jid: self._runner_tasks.pop(job_id, None))
        return rec

    async def _run_job(self, job_id: str) -> None:
        rec = self._jobs.get(job_id)
        if not rec:
            return
        cfg = config_file_path()
        act = venv_activate_path()
        work = rec.layer_path
        script = (
            f"source {shlex.quote(str(act))} && "
            f"trae-cli run {shlex.quote(rec.command)} "
            f"--config-file={shlex.quote(str(cfg))} "
            f"--working-dir={shlex.quote(work)}"
        )
        cmd = ["bash", "-lc", script]

        rec.status = "running"
        rec.output = ""
        await self._save()
        await hub.publish({"type": "job_started", "job_id": job_id})

        if rec.git_branch:
            co_out, co_code = await git_checkout(Path(rec.layer_path), rec.git_branch)
            banner = f"[git checkout {rec.git_branch}]\n{co_out}"
            rec.output += banner
            await hub.publish(
                {
                    "type": "job_output",
                    "job_id": job_id,
                    "chunk": banner,
                }
            )
            if co_code != 0:
                rec.status = "failed"
                rec.exit_code = co_code
                await self._save()
                await hub.publish({"type": "job_finished", "job_id": job_id, "job": rec.to_dict()})
                return

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=work,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                start_new_session=True,
            )
        except Exception as e:
            rec.status = "failed"
            rec.output = f"spawn error: {e}\n"
            rec.exit_code = -1
            await self._save()
            await hub.publish({"type": "job_finished", "job_id": job_id, "job": rec.to_dict()})
            return

        self._running[job_id] = proc
        assert proc.stdout is not None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace")
                rec.output += text
                await hub.publish(
                    {
                        "type": "job_output",
                        "job_id": job_id,
                        "chunk": text,
                    }
                )
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
            await self._save()
            await hub.publish({"type": "job_finished", "job_id": job_id, "job": rec.to_dict()})

    async def interrupt(self, job_id: str) -> bool:
        rec = self._jobs.get(job_id)
        if not rec or rec.status != "running":
            return False
        proc = self._running.get(job_id)
        if not proc:
            return False
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            proc.terminate()
        await hub.publish({"type": "job_interrupt_requested", "job_id": job_id})
        return True

    async def redo_job(self, job_id: str) -> JobRecord:
        """删除该任务可写层并从创建时的来源重新复制，再执行同一指令。"""
        async with self._lock:
            rec = self._jobs.get(job_id)
            if not rec:
                raise ValueError("Job not found")
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
                    raise ValueError(f"parent_job_id not found: {rec.parent_job_id}")
                lp = create_stacked_layer(new_lid, Path(parent.layer_path))
            elif rec.repo_layer_id:
                src = layer_path(rec.repo_layer_id)
                if not src.is_dir():
                    raise ValueError(f"repo_layer_id not found: {rec.repo_layer_id}")
                lp = create_stacked_layer(new_lid, src.resolve())
            else:
                lp = create_root_layer(new_lid)

            rec.layer_id = new_lid
            rec.layer_path = str(lp)
            rec.status = "pending"
            rec.output = ""
            rec.exit_code = None

        await self._save()
        await hub.publish({"type": "job_redone", "job_id": job_id, "job": rec.to_dict()})

        task = asyncio.create_task(self._run_job(job_id))
        self._runner_tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._runner_tasks.pop(jid, None))
        return rec

    async def reset_all(self) -> dict[str, Any]:
        """中断所有任务并清空任务列表。"""
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
        await hub.publish({"type": "jobs_reset"})
        return {
            "jobs_cleared": len(all_job_ids),
            "running_interrupted": len(running_items),
            "runner_tasks_cancelled": len(runner_items),
            "layers_removed": layers_stat.get("removed", 0),
        }


store = JobStore()
